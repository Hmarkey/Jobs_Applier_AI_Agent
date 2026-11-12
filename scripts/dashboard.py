"""A dashboard of your applications, on this computer: `uv run python scripts/dashboard.py`, and the page opens in the browser.

It reads AIHawk's store, data/jobs.db, and never writes to it. What you track yourself (how each application is going,
the next step and its date, your notes) is kept in a file of its own beside it, data/tracking.db, so AIHawk's own file
stays as AIHawk made it. The server answers on 127.0.0.1 only: nothing leaves this computer.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from aihawk.apply import why_not_sent
from aihawk.config import default_home
from aihawk.screens import readable
from aihawk.store import SENT_WORDS, SUBMITTED
from aihawk.telemetry import vendor_of

PAGE = Path(__file__).with_name("dashboard.html")
PORT = 8765

# How an application is going, as you track it. A job AIHawk sent starts at waiting, one it did not send at not_sent.
STAGES = {
    "waiting": "等待回复",
    "interviewing": "面试中",
    "offer": "已获 Offer",
    "rejected": "被拒",
    "withdrawn": "已放弃",
    "no_reply": "无回音",
    "not_sent": "未发出",
}
# The four groups the page counts and colours by.
GROUPS = {
    "interviewing": "active",
    "offer": "active",
    "waiting": "waiting",
    "rejected": "ended",
    "withdrawn": "ended",
    "no_reply": "ended",
    "not_sent": "not_sent",
}
# A stage that means an interview was reached: once reached, it counts for the interview rate even after a rejection.
INTERVIEWED = {"interviewing", "offer"}

RESULTS = {
    "submitted": "已发送并确认",
    "unconfirmed": "已发送，未确认",
    "refused": "被表单拒绝",
    "blocked": "未发出",
    "failed": "失败",
}
# Why AIHawk did not send a job, in Chinese; a reason not here falls back to AIHawk's own English words.
REASONS = {
    "captcha": "验证码拦截",
    "login": "网站要求登录",
    "closed": "职位已关闭",
    "broken_page": "页面无法使用",
    "missing_info": "缺少必填信息",
    "stuck": "原地打转",
    "no_code": "邮件验证码没收到",
    "mailbox_signed_out": "邮箱未登录",
    "never_clicked": "提交按钮没有响应",
    "form_not_loaded": "表单没有加载",
    "browser_did_not_open": "浏览器没有打开",
    "browser_not_ready": "浏览器启动太慢",
    "refused_by_form": "表单拒绝了提交",
    "dry_run": "试运行，未提交",
    "other": "无法填写表单",
    "no_credit": "OrcaRouter 余额不足",
    "free_calls_used": "今天的免费调用已用完",
    "key_limit": "这个 key 的额度已用完",
    "spend_limit": "这个 key 达到了消费上限",
    "budget": "本月 OrcaRouter 预算已用完",
    "model_not_allowed": "这个 key 不能使用所需的模型",
    "refused_other": "OrcaRouter 拒绝了这个 key",
    "no_browser_here": "浏览器没有适合这台电脑的版本",
    "no_display": "浏览器需要桌面环境",
    "browser_libraries": "缺少浏览器依赖库",
}


def reason_words(reason: str) -> str:
    """Why a job was not sent, in Chinese: the known reasons by name, an error by what kind it was."""
    if not reason or reason in REASONS:
        return REASONS.get(reason, "")
    return "AI 服务出错" if why_not_sent(reason) == "the AI service gave an error" else "出现错误"


TRACKING_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracking (
    job_id TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    next_date TEXT NOT NULL DEFAULT '',
    next_event TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    interviewed INTEGER NOT NULL DEFAULT 0,
    updated TEXT NOT NULL
)
"""
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LIMITS = {"next_event": 200, "notes": 10_000}
TRIED = tuple(SENT_WORDS)


class Data:
    """AIHawk's store, read only, and the tracking file beside it."""

    def __init__(self, home: Path) -> None:
        self.data_dir = home / "data"
        self.jobs = self.data_dir / "jobs.db"
        self.proofs = (self.data_dir / "proofs").resolve()
        self.tracking = self.data_dir / "tracking.db"
        with self._tracking() as db:
            db.execute(TRACKING_SCHEMA)
            db.commit()

    @contextmanager
    def _store(self) -> Iterator[sqlite3.Connection]:
        # Read only: AIHawk may be writing to it at the same moment, and this page never changes it.
        db = sqlite3.connect(f"{self.jobs.as_uri()}?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _tracking(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.tracking, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def _tracked(self) -> dict[str, sqlite3.Row]:
        with self._tracking() as db:
            return {row["job_id"]: row for row in db.execute("SELECT * FROM tracking")}

    def items(self) -> list[dict[str, Any]]:
        """Every job AIHawk tried, newest first, with how you track it."""
        marks = ",".join("?" * len(TRIED))
        with self._store() as db:
            rows = db.execute(
                "SELECT id, company, title, location, url, apply_url, status, score, sent_at, updated, final_url, verdict_evidence,"
                f" screenshot, cost, reason FROM jobs WHERE status IN ({marks}) ORDER BY COALESCE(sent_at, updated) DESC",
                TRIED,
            ).fetchall()
        tracked = self._tracked()
        return [self._item(row, tracked.get(row["id"])) for row in rows]

    def _item(self, row: sqlite3.Row, mine: sqlite3.Row | None) -> dict[str, Any]:
        sent = row["status"] in SUBMITTED
        stage = mine["stage"] if mine else ("waiting" if sent else "not_sent")
        reason = row["reason"] or ""
        host = (urlsplit(row["apply_url"] or row["url"]).hostname or "").lower()
        vendor = vendor_of(host)
        return {
            "id": row["id"],
            "company": row["company"],
            "title": row["title"],
            "location": row["location"],
            "url": row["url"],
            "final_url": row["final_url"] or "",
            "site": host if vendor == "other" else vendor,
            "when": row["sent_at"] or row["updated"],
            "result": row["status"],
            "result_words": RESULTS.get(row["status"], row["status"]),
            "reason": reason,
            "reason_words": reason_words(reason),
            "evidence": row["verdict_evidence"] or "",
            "score": row["score"],
            "cost": row["cost"],
            "has_screenshot": self._proof(row["screenshot"]) is not None,
            "stage": stage,
            "group": GROUPS.get(stage, "waiting"),
            "next_date": mine["next_date"] if mine else "",
            "next_event": mine["next_event"] if mine else "",
            "notes": mine["notes"] if mine else "",
            "interviewed": bool(mine["interviewed"]) if mine else False,
        }

    def details(self, job_id: str) -> dict[str, Any] | None:
        """The long texts of one job: the posting, what the form was filled with, and what the page said after."""
        marks = ",".join("?" * len(TRIED))
        with self._store() as db:
            row = db.execute(f"SELECT description, note, page_text FROM jobs WHERE id = ? AND status IN ({marks})", (job_id, *TRIED)).fetchone()
        if row is None:
            return None
        return {"description": readable(row["description"] or ""), "filled": row["note"] or "", "page_text": row["page_text"] or ""}

    def screenshot(self, job_id: str) -> Path | None:
        """The picture of the page after the send, only from AIHawk's proofs folder."""
        with self._store() as db:
            row = db.execute("SELECT screenshot FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._proof(row["screenshot"]) if row else None

    def _proof(self, kept: str | None) -> Path | None:
        if not kept:
            return None
        path = Path(kept).resolve()
        return path if path.is_relative_to(self.proofs) and path.is_file() and path.suffix.lower() == ".png" else None

    def save(self, job_id: str, change: dict[str, Any]) -> dict[str, Any]:
        """Your tracking of one job, checked; the job as the page shows it after."""
        current = next((item for item in self.items() if item["id"] == job_id), None)
        if current is None:
            raise LookupError(job_id)
        stage = str(change.get("stage", current["stage"]))
        if stage not in STAGES:
            raise ValueError(f"unknown stage: {stage}")
        next_date = str(change.get("next_date", current["next_date"])).strip()
        if next_date and not DATE.match(next_date):
            raise ValueError("next_date must be YYYY-MM-DD")
        texts = {name: str(change.get(name, current[name])).strip()[:limit] for name, limit in LIMITS.items()}
        interviewed = current["interviewed"] or stage in INTERVIEWED
        with self._tracking() as db:
            db.execute(
                "INSERT INTO tracking (job_id, stage, next_date, next_event, notes, interviewed, updated) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(job_id) DO UPDATE SET stage = excluded.stage, next_date = excluded.next_date, next_event = excluded.next_event,"
                " notes = excluded.notes, interviewed = excluded.interviewed, updated = excluded.updated",
                (job_id, stage, next_date, texts["next_event"], texts["notes"], int(interviewed), datetime.now(UTC).isoformat(timespec="seconds")),
            )
            db.commit()
        return next(item for item in self.items() if item["id"] == job_id)


def handler(data: Data, port: int) -> type[BaseHTTPRequestHandler]:
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            # Quiet: the terminal shows the address and nothing else.
            pass

        def _allowed(self) -> bool:
            # A page on another site, or another name pointed at this address, gets nothing here.
            if self.headers.get("Host", "") not in hosts:
                return False
            origin = self.headers.get("Origin")
            return origin is None or urlsplit(origin).netloc in hosts

        def _send(self, status: int, body: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _json(self, value: Any, status: int = HTTPStatus.OK) -> None:
            self._send(status, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _job_id(self) -> str:
            return parse_qs(urlsplit(self.path).query).get("id", [""])[0]

        def do_GET(self) -> None:
            if not self._allowed():
                return self._send(HTTPStatus.FORBIDDEN, b"", "text/plain")
            path = urlsplit(self.path).path
            try:
                if path == "/":
                    return self._send(HTTPStatus.OK, PAGE.read_bytes(), "text/html; charset=utf-8")
                if path == "/api/data":
                    return self._json({"items": data.items(), "stages": STAGES, "groups": GROUPS, "now": datetime.now(UTC).isoformat()})
                if path == "/api/job":
                    found = data.details(self._job_id())
                    return self._json(found) if found is not None else self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                if path == "/api/proof":
                    picture = data.screenshot(self._job_id())
                    return self._send(HTTPStatus.OK, picture.read_bytes(), "image/png") if picture else self._send(HTTPStatus.NOT_FOUND, b"", "text/plain")
            except sqlite3.Error as error:
                return self._json({"error": f"the store cannot be read: {error}"}, HTTPStatus.SERVICE_UNAVAILABLE)
            self._send(HTTPStatus.NOT_FOUND, b"", "text/plain")

        def do_POST(self) -> None:
            # JSON only: a form on another site cannot send it without the browser asking first, and this server never says yes.
            if not self._allowed() or self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self._send(HTTPStatus.FORBIDDEN, b"", "text/plain")
            if urlsplit(self.path).path != "/api/job":
                return self._send(HTTPStatus.NOT_FOUND, b"", "text/plain")
            try:
                size = min(int(self.headers.get("Content-Length", "0")), 64_000)
                change = json.loads(self.rfile.read(size) or b"{}")
                if not isinstance(change, dict):
                    return self._json({"error": "send an object"}, HTTPStatus.BAD_REQUEST)
                self._json(data.save(self._job_id(), change))
            except LookupError:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            except sqlite3.Error as error:
                self._json({"error": f"not saved: {error}"}, HTTPStatus.SERVICE_UNAVAILABLE)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="A dashboard of your AIHawk applications, in your browser.")
    parser.add_argument("--home", type=Path, default=default_home(), help="AIHawk's folder (default: ~/.aihawk)")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-open", action="store_true", help="do not open the browser")
    options = parser.parse_args()
    if not (options.home / "data" / "jobs.db").is_file():
        sys.exit(f"No AIHawk store at {options.home / 'data' / 'jobs.db'}: run AIHawk first.")
    data = Data(options.home.resolve())
    server = ThreadingHTTPServer(("127.0.0.1", options.port), handler(data, options.port))
    address = f"http://127.0.0.1:{options.port}/"
    print(f"Dashboard: {address}  (Ctrl+C stops it)")
    if not options.no_open:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
