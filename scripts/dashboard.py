"""A dashboard of your applications, on this computer: `uv run python scripts/dashboard.py`, and the page opens in the browser.

The page has two views. The applications: every job AIHawk tried, read from AIHawk's store, data/jobs.db, which the page
itself never writes to. What you track yourself (how each application is going, the next step and its date, your notes)
is kept in a file of its own beside it, data/tracking.db. The jobs found: the ones the search found and AIHawk has not
tried, best first, each with its posting, to select and send. Sending goes through AIHawk's own Session, as AIHawk's screen
does, and it is AIHawk's code that records how each one went. The server answers on 127.0.0.1 only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
import threading
import webbrowser
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from aihawk import errors
from aihawk.apply import missing_for_the_browser, no_browser_here, why_not_sent
from aihawk.config import default_home, dry_run, load_config
from aihawk.library import Library
from aihawk.llm import ACCOUNT_CAUSES, refused
from aihawk.screens import readable
from aihawk.session import Session
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
    "stopped": "被你终止",
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
# Where a job the search found stands before it is tried.
FOUND = {
    "scored": "已打分",
    "new": "等待打分",
    "shortlisted": "等待打分",
    "passed_over": "AI 未给出判断",
}
# The most applications one batch from the page sends: each takes minutes, and a long batch is better started again.
BATCH = 50


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

    # The jobs found and not tried yet.

    def found(self) -> list[dict[str, Any]]:
        """Every job the search found that AIHawk has not tried: the scored first, by their score, which is what the page shows,
        and the title's fit among equal scores; then the rest by the title's fit."""
        marks = ",".join("?" * len(TRIED))
        with self._store() as db:
            rows = db.execute(
                "SELECT id, company, title, location, url, apply_url, status, score, lexical, length(description) > 0 AS described"
                f" FROM jobs WHERE status NOT IN ({marks})",
                TRIED,
            ).fetchall()
        scored = sorted((row for row in rows if row["score"] is not None), key=lambda row: (-row["score"], -row["lexical"], row["id"]))
        rest = sorted((row for row in rows if row["score"] is None), key=lambda row: (-row["lexical"], row["id"]))
        return [self._found(row) for row in [*scored, *rest]]

    @staticmethod
    def _found(row: sqlite3.Row) -> dict[str, Any]:
        host = (urlsplit(row["apply_url"] or row["url"]).hostname or "").lower()
        vendor = vendor_of(host)
        return {
            "id": row["id"],
            "company": row["company"],
            "title": row["title"],
            "location": row["location"],
            "url": row["url"],
            "site": host if vendor == "other" else vendor,
            "score": row["score"],
            "status": row["status"],
            "status_words": FOUND.get(row["status"], row["status"]),
            "described": bool(row["described"]),
        }

    def found_details(self, job_id: str) -> dict[str, Any] | None:
        """The posting of one job found: its text as AIHawk kept it when scoring, else as the job database on this computer has it."""
        marks = ",".join("?" * len(TRIED))
        with self._store() as db:
            row = db.execute(f"SELECT url, description FROM jobs WHERE id = ? AND status NOT IN ({marks})", (job_id, *TRIED)).fetchone()
        if row is None:
            return None
        text = row["description"] or ""
        if not text:
            try:
                text = Library(self.data_dir / "library").descriptions([row["url"]])[0]
            except Exception:  # noqa: BLE001  The database may be downloading or gone: the page says there is no text.
                text = ""
        return {"description": readable(text)}

    def untried(self, job_ids: list[str]) -> list[dict[str, str]]:
        """These jobs, in this order, each one found and not tried; a LookupError names how many are not."""
        marks = ",".join("?" * len(job_ids))
        tried = ",".join("?" * len(TRIED))
        with self._store() as db:
            rows = {
                row["id"]: row
                for row in db.execute(f"SELECT id, company, title FROM jobs WHERE id IN ({marks}) AND status NOT IN ({tried})", (*job_ids, *TRIED))
            }
        missing = [job_id for job_id in job_ids if job_id not in rows]
        if missing:
            raise LookupError(f"{len(missing)} 个职位已经申请过或不在列表里了，请刷新后重新选择")
        return [{"id": job_id, "company": rows[job_id]["company"], "title": rows[job_id]["title"]} for job_id in job_ids]


class Busy(Exception):
    """A batch is being sent already."""


def _idle() -> dict[str, Any]:
    return {"running": False, "stopping": False, "terminating": False, "total": 0, "number": 0, "current": None, "note": "", "queue": [],
            "results": [], "log": [], "error": "", "started": "", "finished": ""}


# What a job ended by the person says, kept with it. It counts as tried, so the page never sends it again on its own: it may
# have been ended after the click that sends.
STOPPED = "stopped"
STOPPED_NOTE = "你在申请过程中终止了它。如果终止时已经点了提交按钮，申请可能已经发出：请打开职位链接确认。"


class Sender:
    """Sends the applications chosen on the page, one batch at a time, through AIHawk's own Session: in a thread of its own,
    with its own event loop and its own connection to the store, one application after the other, as AIHawk's screen does.
    The page reads how far it is with `state`. `stop` ends the batch once the application under way has ended; `terminate`
    ends that application now, closing its browser, and the batch with it."""

    def __init__(self, home: Path, data: Data, session: Callable[[Path], Any] = Session) -> None:
        self.home = home
        self.data = data
        self._session = session
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._state = _idle()
        # The application under way and the loop it runs on, so another thread can end it.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None

    def state(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def _set(self, **change: Any) -> None:
        with self._lock:
            self._state.update(change)

    def blocked(self) -> str:
        """Why nothing can be sent from this computer now, in plain words; empty when it can."""
        if no_browser_here():
            return "这台电脑上 AIHawk 的浏览器无法运行，只能找职位和打分，不能发送申请。"
        if missing_for_the_browser() is not None:
            return "浏览器需要桌面环境或缺少系统库，现在不能发送申请。"
        config = load_config(self.home)
        if not config.api_key():
            return "还没有 OrcaRouter key：先在 AIHawk 终端里输入 key。"
        if not config.cv.is_file():
            return "还没有简历：先在 AIHawk 终端里读入简历。"
        return ""

    def start(self, job_ids: object) -> dict[str, Any]:
        """The batch started, in the order given; a ValueError says what is wrong with the request, Busy that one is under way."""
        if not isinstance(job_ids, list) or not job_ids or not all(isinstance(job_id, str) and job_id for job_id in job_ids):
            raise ValueError("请至少选择一个职位")
        job_ids = list(dict.fromkeys(job_ids))
        if len(job_ids) > BATCH:
            raise ValueError(f"一次最多发送 {BATCH} 个申请")
        if why := self.blocked():
            raise ValueError(why)
        jobs = self.data.untried(job_ids)
        with self._lock:
            if self._state["running"]:
                raise Busy
            self._stopping.clear()
            self._state = {**_idle(), "running": True, "total": len(jobs), "queue": jobs, "started": _now()}
        threading.Thread(target=self._run, args=(jobs,), name="dashboard-send", daemon=True).start()
        return self.state()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._state["running"]:
                self._stopping.set()
                self._state["stopping"] = True
        return self.state()

    def terminate(self) -> dict[str, Any]:
        """The application under way ended now, and no other started after it."""
        with self._lock:
            if self._state["running"]:
                self._stopping.set()
                self._state.update(stopping=True, terminating=True)
                if self._loop is not None and self._task is not None:
                    self._loop.call_soon_threadsafe(self._task.cancel)
        return self.state()

    def _run(self, jobs: list[dict[str, str]]) -> None:
        try:
            asyncio.run(self._send(jobs))
        except Exception as error:  # noqa: BLE001  Said on the page, with its details in AIHawk's error log.
            if refused(error):
                words = "OrcaRouter 拒绝了这个 key：在 AIHawk 终端里换一个 key 后再试。"
            else:
                errors.record(error)
                words = f"发送中断：{type(error).__name__}，详情见 data/errors.log。"
            self._set(error=words)
        finally:
            self._set(running=False, stopping=False, terminating=False, current=None, note="", finished=_now())

    async def _send(self, jobs: list[dict[str, str]]) -> None:
        session = self._session(self.home)
        loop = asyncio.get_running_loop()
        try:
            for number, job in enumerate(jobs, 1):
                if self._stopping.is_set():
                    break
                item = session.store.get(job["id"])
                if item is None or item.status in TRIED:
                    self._result(job, "skipped", "已申请过或已不在列表里，跳过")
                    continue
                self._set(number=number, current=job, note="")
                task = asyncio.ensure_future(session.apply(item, self._log, self._note))
                with self._lock:
                    self._loop, self._task = loop, task
                    # Asked to end before the task was there to end.
                    if self._state["terminating"]:
                        task.cancel()
                try:
                    outcome = await task
                except asyncio.CancelledError:
                    if not task.cancelled():
                        raise
                    # Ended by the person: the browser is closed by the application's own cleanup. Kept as tried, never sent again
                    # by itself, since the click that sends may have come before the end.
                    session.store.set_status(item.job.id, "blocked", STOPPED_NOTE, STOPPED)
                    self._result(job, "blocked", reason_words(STOPPED), STOPPED)
                    break
                finally:
                    with self._lock:
                        self._loop = self._task = None
                self._result(job, outcome.status, reason_words(outcome.reason), outcome.reason, outcome.proof.evidence)
                if outcome.reason in ACCOUNT_CAUSES:
                    # Every application after this one would fail the same way.
                    self._set(error=f"已停止：{reason_words(outcome.reason)}")
                    break
        finally:
            session.close()

    def _result(self, job: dict[str, str], status: str, words: str, reason: str = "", evidence: str = "") -> None:
        with self._lock:
            self._state["results"].append({
                **job, "status": status, "status_words": RESULTS.get(status, "跳过" if status == "skipped" else status),
                "reason": reason, "reason_words": words, "evidence": evidence, "needs_mail": reason == "mailbox_signed_out",
            })

    def _log(self, line: str) -> None:
        with self._lock:
            self._state["log"] = [*self._state["log"], line][-20:]

    def _note(self, line: str) -> None:
        self._set(note=line)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def handler(data: Data, port: int, sender: Sender) -> type[BaseHTTPRequestHandler]:
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
                if path == "/api/found":
                    return self._json({"items": data.found(), "dry_run": dry_run(), "blocked": sender.blocked(), "batch": BATCH})
                if path == "/api/found/job":
                    found = data.found_details(self._job_id())
                    return self._json(found) if found is not None else self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                if path == "/api/apply":
                    return self._json(sender.state())
            except sqlite3.Error as error:
                return self._json({"error": f"the store cannot be read: {error}"}, HTTPStatus.SERVICE_UNAVAILABLE)
            self._send(HTTPStatus.NOT_FOUND, b"", "text/plain")

        def do_POST(self) -> None:
            # The body is read before any answer, even a refusal: one left unread makes Windows drop the connection, answer and all.
            try:
                raw = self.rfile.read(min(int(self.headers.get("Content-Length", "0")), 64_000))
            except ValueError:
                raw = b""
            # JSON only: a form on another site cannot send it without the browser asking first, and this server never says yes.
            if not self._allowed() or self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self._send(HTTPStatus.FORBIDDEN, b"", "text/plain")
            path = urlsplit(self.path).path
            if path not in ("/api/job", "/api/apply", "/api/apply/stop", "/api/apply/terminate"):
                return self._send(HTTPStatus.NOT_FOUND, b"", "text/plain")
            try:
                change = json.loads(raw or b"{}")
                if not isinstance(change, dict):
                    return self._json({"error": "send an object"}, HTTPStatus.BAD_REQUEST)
                if path == "/api/apply":
                    return self._json(sender.start(change.get("ids")), HTTPStatus.ACCEPTED)
                if path == "/api/apply/stop":
                    return self._json(sender.stop())
                if path == "/api/apply/terminate":
                    return self._json(sender.terminate())
                self._json(data.save(self._job_id(), change))
            except Busy:
                self._json({"error": "已经有一批申请在发送中"}, HTTPStatus.CONFLICT)
            except LookupError as error:
                self._json({"error": str(error) if path == "/api/apply" else "not found"}, HTTPStatus.NOT_FOUND)
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
    home = options.home.resolve()
    data = Data(home)
    server = ThreadingHTTPServer(("127.0.0.1", options.port), handler(data, options.port, Sender(home, data)))
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
