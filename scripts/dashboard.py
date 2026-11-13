"""A dashboard of your applications, on this computer: `uv run python scripts/dashboard.py`, and the page opens in the browser.

The page has two views. The applications: every job AIHawk tried, read from AIHawk's store, data/jobs.db, which the page
itself never writes to. What you track yourself (how each application is going, the next step and its date, your notes)
is kept in a file of its own beside it, data/tracking.db. The jobs found: the ones the search found and AIHawk has not
tried, best first, each with its posting, to send automatically or to apply to yourself. Sending goes through AIHawk's own
Session, as AIHawk's screen does, and it is AIHawk's code that records how each one went; a job you applied to yourself is
marked as sent through AIHawk's own Store, so that AIHawk never sends it again. The server answers on 127.0.0.1 only.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
import os
import re
import sqlite3
import threading
import time
import webbrowser
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from openai import AsyncOpenAI

from aihawk import errors
from aihawk.apply import MAILBOXES, mail_provider, missing_for_the_browser, no_browser_here, why_not_sent
from aihawk.config import DEFAULT_BASE_URL, DEFAULT_DATABASE, DEFAULT_MAX_AGE_DAYS, KEY_ENV, ConfigError, default_home, dry_run, load_config
from aihawk.database import DatabaseError, load_manifest
from aihawk.library import STATE, Library, LibraryError, Stopped
from aihawk.llm import ACCOUNT_CAUSES, account_problem, no_credit, refused
from aihawk.profile import CV_NAME_FILE, save_setting
from aihawk.scoring import NotAnswering
from aihawk.screens import readable
from aihawk.session import BALANCE_PATH, WIDE, Session
from aihawk.store import OWN_ANSWERS, SENT_WORDS, SUBMITTED, Proof, Store, write_applied
from aihawk.telemetry import vendor_of
from aihawk.tui import RETRY_PAUSES

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
    """One person's AIHawk store, read only, and the tracking file beside it, in their workspace."""

    def __init__(self, home: Path, library: Library | None = None) -> None:
        self.data_dir = home / "data"
        # The job database a posting's text is read from when the store has none: the shared one, else the workspace's own.
        self.library = library
        self.jobs = self.data_dir / "jobs.db"
        self.proofs = (self.data_dir / "proofs").resolve()
        self.tracking = self.data_dir / "tracking.db"
        if not self.jobs.is_file():
            # A person just added has no store yet: AIHawk's own makes it, empty, so the page can read it.
            self.data_dir.mkdir(parents=True, exist_ok=True)
            Store(self.jobs).close()
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
            "result_words": "手动申请" if reason == MANUAL else RESULTS.get(row["status"], row["status"]),
            "manual": reason == MANUAL,
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
                text = (self.library or Library(self.data_dir / "library")).descriptions([row["url"]])[0]
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

    # The applications you make yourself.

    def mark_manual(self, job_id: str) -> dict[str, Any]:
        """A job found, applied to by you in your own browser: kept in AIHawk's store as sent, so that it is listed and tracked
        with the others and AIHawk never sends it again. Written through AIHawk's own Store, as an application would be."""
        self.untried([job_id])
        store = Store(self.jobs)
        try:
            store.set_status(job_id, "submitted", MANUAL_NOTE, MANUAL)
            store.set_proof(job_id, Proof(at=_now(), verdict=MANUAL, evidence="你自己在浏览器里完成了申请"))
            self._applied_csv(store)
        finally:
            store.close()
        return next(item for item in self.items() if item["id"] == job_id)

    def undo_manual(self, job_id: str) -> None:
        """A job marked as applied by you by mistake, back among the jobs found as it was: scored if it had a score. Your
        tracking of it goes too. Only a job marked by hand can be put back: one AIHawk sent stays sent."""
        with self._store() as db:
            row = db.execute("SELECT status, reason, score FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None or row["reason"] != MANUAL or row["status"] not in SUBMITTED:
            raise LookupError(job_id)
        store = Store(self.jobs)
        try:
            store.set_status(job_id, "scored" if row["score"] is not None else "new")
            store.set_proof(job_id, Proof())
            self._applied_csv(store)
        finally:
            store.close()
        with self._tracking() as db:
            db.execute("DELETE FROM tracking WHERE job_id = ?", (job_id,))
            db.commit()

    def _applied_csv(self, store: Store) -> None:
        """applied.csv, which AIHawk writes after each application, written again so that it lists what you did yourself too."""
        try:
            write_applied(self.data_dir.parent / "applied.csv", store.sent())
        except OSError:
            pass  # Another program holds it open: AIHawk writes it again after its next application.


class Busy(Exception):
    """Something is under way already: a batch being sent, a search, a sign-in. Its text says what, when it says it."""


class UnknownUser(LookupError):
    """A request names no user, or one the dashboard does not know."""


def _record_error(error: BaseException, folder: Path | None) -> None:
    """An unexpected error into the dashboard's own log: AIHawk's log goes where the last session set it, which may be someone else's folder."""
    if folder is not None:
        errors.use(folder, os.environ.get(KEY_ENV, ""))
    errors.record(error)


def _idle() -> dict[str, Any]:
    return {"running": False, "stopping": False, "terminating": False, "total": 0, "number": 0, "current": None, "note": "", "queue": [],
            "results": [], "log": [], "error": "", "started": "", "finished": ""}


# What a job ended by the person says, kept with it. It counts as tried, so the page never sends it again on its own: it may
# have been ended after the click that sends.
STOPPED = "stopped"
STOPPED_NOTE = "你在申请过程中终止了它。如果终止时已经点了提交按钮，申请可能已经发出：请打开职位链接确认。"
# A job you applied to yourself, in your own browser, and marked here as done.
MANUAL = "manual"
MANUAL_NOTE = "你在自己的浏览器里手动完成了这份申请，然后在看板里标记为已完成。"


class Sender:
    """Sends the applications chosen on the page, one batch at a time, through AIHawk's own Session: in a thread of its own,
    with its own event loop and its own connection to the store, one application after the other, as AIHawk's screen does.
    The page reads how far it is with `state`. `stop` ends the batch once the application under way has ended; `terminate`
    ends that application now, closing its browser, and the batch with it."""

    def __init__(self, home: Path, data: Data, session: Callable[[Path], Any] = Session, log_dir: Path | None = None) -> None:
        self.home = home
        self.data = data
        self._session = session
        self.log_dir = log_dir
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
            return "还没有 OrcaRouter key：先在「设置」里填写。"
        if not config.cv.is_file():
            return "还没有简历：先在「设置」里上传简历。"
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
                raise Busy("已经有一批申请在发送中")
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
                words = "OrcaRouter 不认现在的 key：在「设置」里换一个后再试。"
            else:
                _record_error(error, self.log_dir)
                words = f"发送中断：{type(error).__name__}，详情见 {(self.log_dir or self.home / 'data') / errors.LOG}。"
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


# ---------- several people on one computer ----------

USERS_FILE = "users.json"
SETTINGS_FILE = "settings.json"
KEY_FILE = "key"
NAME_CHARS = 40
RESUME_TYPES = (".pdf", ".docx", ".txt", ".md")
RESUME_BYTES = 10 * 1024 * 1024
# A body this large is read only for a resume, which comes as base64 inside JSON; every other request is small.
RESUME_BODY = RESUME_BYTES * 4 // 3 + 4096
SMALL_BODY = 64_000
# A search scores this many rounds of WIDE postings at first; more are asked for on the page.
FIRST_ROUNDS = 2
# The shared job database, brought up to date this recently, is not asked for again by the next search.
SYNC_AGAIN_S = 30 * 60
ANSWER_CHARS = 2000


def _write_json(path: Path, value: Any) -> None:
    """A JSON file written whole beside itself and renamed over the old one, so it is never half written."""
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    part.replace(path)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else default
    except ValueError:
        return default


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "user"


class Users:
    """Who uses this computer's AIHawk: a name, and the workspace their search lives in, in users.json under the root."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / USERS_FILE
        self._lock = threading.Lock()
        found = _read_json(self.path, [])
        self._list: list[dict[str, str]] = [entry for entry in found if isinstance(entry, dict) and entry.get("id")] if isinstance(found, list) else []

    def all(self) -> list[dict[str, str]]:
        with self._lock:
            return [dict(entry) for entry in self._list]

    def get(self, user_id: str) -> dict[str, str] | None:
        with self._lock:
            return next((dict(entry) for entry in self._list if entry["id"] == user_id), None)

    def _name(self, name: object, keep: str = "") -> str:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("请填写名字")
        name = " ".join(name.split())
        if len(name) > NAME_CHARS:
            raise ValueError(f"名字最多 {NAME_CHARS} 个字")
        if any(entry["name"] == name and entry["id"] != keep for entry in self._list):
            raise ValueError("已经有同名的用户了")
        return name

    def add(self, name: object, workspace: Path | None = None, user_id: str = "") -> dict[str, str]:
        """A person added, with a workspace of their own under the root unless one is named; the entry."""
        with self._lock:
            name = self._name(name)
            base = user_id or _slug(name)
            taken = {entry["id"] for entry in self._list}
            chosen = next(candidate for candidate in (base if n == 1 else f"{base}-{n}" for n in range(1, 10_000)) if candidate not in taken)
            folder = workspace or self.root / "users" / chosen
            folder.mkdir(parents=True, exist_ok=True)
            entry = {"id": chosen, "name": name, "workspace": str(folder), "created": _now()}
            self._list.append(entry)
            _write_json(self.path, self._list)
            return dict(entry)

    def rename(self, user_id: str, name: object) -> dict[str, str]:
        with self._lock:
            entry = next((entry for entry in self._list if entry["id"] == user_id), None)
            if entry is None:
                raise UnknownUser(user_id)
            entry["name"] = self._name(name, keep=user_id)
            _write_json(self.path, self._list)
            return dict(entry)


async def check_key(key: str) -> str:
    """Why OrcaRouter will not take this key, in plain words; empty when it will, or when it cannot be asked: the check never blocks."""
    client = AsyncOpenAI(base_url=DEFAULT_BASE_URL, api_key=key, max_retries=0, timeout=15.0)
    try:
        try:
            await client.get("/models", cast_to=dict)
        except Exception as error:  # noqa: BLE001  Only a refusal is an answer.
            if refused(error):
                return "OrcaRouter 不认这个 key，请检查后重新粘贴。"
        try:
            answer = await client.get(BALANCE_PATH, cast_to=dict)
            left = answer["data"]["spendable_usd"]
        except Exception:  # noqa: BLE001  Not knowing the credit is no reason to refuse the key.
            return ""
        if isinstance(left, int | float) and not isinstance(left, bool) and left <= 0:
            return "这个 key 的余额已经用完：先到 orcarouter.ai 充值，再保存。"
        return ""
    finally:
        await client.close()


def _account_words(error: BaseException) -> str:
    """What the AI service's refusal means for everyone on the page, in plain words; empty when it is not one."""
    if refused(error):
        return "OrcaRouter 不认现在的 key：在「设置」里换一个。"
    if no_credit(error):
        return "OrcaRouter 余额用完了：到 orcarouter.ai 充值后再试。"
    if account_problem(error):
        return "OrcaRouter 拒绝了请求：请检查 orcarouter.ai 上的账户。"
    return ""


class Syncer:
    """The job database everyone shares, brought to this computer in one thread at a time: the newest postings first, each chunk
    searchable the moment it is here. `changes` counts the chunks and deltas that came, so a search knows to look again."""

    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"running": False, "chunks": [0, 0], "bytes": [0, 0], "error": "", "finished": ""}
        self._changes = 0
        self._last = 0.0

    def state(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def changes(self) -> int:
        with self._lock:
            return self._changes

    def ensure(self) -> None:
        """The database brought up to date, unless it is being or just was."""
        with self._lock:
            if self._state["running"]:
                return
            if self._last and time.monotonic() - self._last < SYNC_AGAIN_S and self.hub.library.complete():
                return
            self._state.update(running=True, error="")
        threading.Thread(target=self._run, name="dashboard-library", daemon=True).start()

    def _run(self) -> None:
        library = self.hub.library
        try:
            library.days = self.hub.max_days()
            manifest = load_manifest(self.hub.database)
            library.sync(manifest, self.hub.database, self._progress, self._arrived, self._bytes)
            self._last = time.monotonic()
        except Stopped:
            pass
        except (LibraryError, DatabaseError) as error:
            self._set(error=str(error))
        except Exception as error:  # noqa: BLE001  Said on the page; the details go to the log.
            _record_error(error, self.hub.root)
            self._set(error=f"职位库下载出错：{type(error).__name__}，详情见 {self.hub.root / errors.LOG}。")
        finally:
            self._set(running=False, finished=_now())

    def _set(self, **change: Any) -> None:
        with self._lock:
            self._state.update(change)

    def _progress(self, done: int, size: int) -> None:
        self._set(chunks=[done, size])

    def _bytes(self, done: int, total: int) -> None:
        self._set(bytes=[done, total])

    def _arrived(self) -> None:
        with self._lock:
            self._changes += 1


def _finder_idle() -> dict[str, Any]:
    return {"running": False, "phase": "idle", "note": "", "error": "", "found": 0, "new": 0, "scored": 0, "rounds": 0, "started": "", "finished": ""}


class Finder:
    """One person's search and scoring, in a thread of its own with its own loop, as the terminal does them on its screen:
    a new resume read, the shared database searched as it comes, and rounds of WIDE postings scored by Jev, a round the AI
    service does not answer asked again after a pause."""

    def __init__(self, workspace: Path, hub: Hub) -> None:
        self.workspace = workspace
        self.hub = hub
        self._lock = threading.Lock()
        self._state = _finder_idle()

    def state(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def _set(self, **change: Any) -> None:
        with self._lock:
            self._state.update(change)

    def _add(self, **counts: int) -> None:
        with self._lock:
            for name, value in counts.items():
                self._state[name] += value

    def start(self, resume: Path | None = None, rounds: int = FIRST_ROUNDS, search: bool = True) -> dict[str, Any]:
        with self._lock:
            if self._state["running"]:
                raise Busy("正在搜索或打分，请等它完成")
            self._state = {**_finder_idle(), "running": True, "phase": "starting", "started": _now()}
        threading.Thread(target=self._run, args=(resume, rounds, search), name="dashboard-find", daemon=True).start()
        return self.state()

    def _run(self, resume: Path | None, rounds: int, search: bool) -> None:
        try:
            asyncio.run(self._work(resume, rounds, search))
            self._set(phase="done", note="")
        except (ConfigError, DatabaseError, LibraryError, StillNotAnswering) as error:
            self._set(phase="error", error=str(error))
        except Exception as error:  # noqa: BLE001  Said on the page; an unexpected one goes to the log.
            words = _account_words(error)
            if not words:
                _record_error(error, self.hub.root)
                words = f"出错了：{type(error).__name__}，详情见 {self.hub.root / errors.LOG}。"
            self._set(phase="error", error=words)
        finally:
            if resume is not None:
                resume.unlink(missing_ok=True)
            self._set(running=False, finished=_now())

    async def _work(self, resume: Path | None, rounds: int, search: bool) -> None:
        session = self.hub.session(self.workspace)
        try:
            if resume is not None:
                self._set(phase="resume", note="正在读简历，大约半分钟…")
                await session.load_resume(resume)
                # A new resume is a new search: the jobs of the old one that nobody applied to give way, as in the terminal.
                session.store.forget_untried()
            if session.needs_resume():
                raise ConfigError("还没有简历：先在「设置」里上传简历。")
            seen = self.hub.syncer.changes()
            if search:
                self.hub.syncer.ensure()
                while not self.hub.library.has_postings():
                    sync = self.hub.syncer.state()
                    if not sync["running"]:
                        raise DatabaseError(sync["error"] or "职位库还没有下载下来，请稍后重试。")
                    self._set(phase="waiting", note="正在下载职位库，第一次需要几分钟…")
                    await asyncio.sleep(1)
                seen = await self._find(session)
            for number in range(1, rounds + 1):
                seen = await self._again(session, seen)
                if not session.store.with_status("new"):
                    break
                self._set(phase="scoring", note=f"AI 正在给职位打分：第 {number} / {rounds} 轮，每轮 {WIDE} 个…")
                await self._score(session)
            # While the database still comes, what arrives is searched; scoring it is the page's "再打 200 个".
            while self.hub.syncer.state()["running"]:
                self._set(phase="following", note="职位库还在下载，新到的职位会自动加入列表。")
                await asyncio.sleep(3)
                seen = await self._again(session, seen)
            await self._again(session, seen)
        finally:
            session.close()

    async def _find(self, session: Any) -> int:
        seen = self.hub.syncer.changes()
        self._set(phase="searching", note="正在搜索职位…")
        new = await session.find()
        self._add(new=new)
        self._set(found=len(session.store.all()))
        return seen

    async def _again(self, session: Any, seen: int) -> int:
        """The search again when chunks came since it last ran."""
        return await self._find(session) if self.hub.syncer.changes() != seen else seen

    async def _score(self, session: Any) -> None:
        failures = 0
        while True:
            try:
                scored = await session.read(lambda line: self._set(note=line))
            except NotAnswering:
                if failures == len(RETRY_PAUSES):
                    raise StillNotAnswering("AI 服务一直没有回应。请检查网络，稍后点「再打 200 个」重试。") from None
                self._set(note=f"AI 服务没有回应，{RETRY_PAUSES[failures]} 秒后重试…")
                await asyncio.sleep(RETRY_PAUSES[failures])
                failures += 1
                continue
            self._add(scored=scored, rounds=1)
            return


class StillNotAnswering(Exception):
    """A round of scoring got no answer after every pause."""


class MailSignIn:
    """One person's mailbox opened in their own browser for them to sign in, until they say on the page that they did."""

    def __init__(self, workspace: Path, hub: Hub) -> None:
        self.workspace = workspace
        self.hub = hub
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"running": False, "phase": "idle", "reached": None, "error": ""}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None

    def state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def _set(self, **change: Any) -> None:
        with self._lock:
            self._state.update(change)

    def start(self, provider: str) -> dict[str, Any]:
        if provider not in MAILBOXES:
            raise ValueError("先填写 Gmail 或 Outlook 邮箱")
        with self._lock:
            if self._state["running"]:
                raise Busy("邮箱登录窗口已经打开了")
            self._state = {"running": True, "phase": "opening", "reached": None, "error": ""}
        threading.Thread(target=self._run, args=(provider,), name="dashboard-mail", daemon=True).start()
        return self.state()

    def done(self) -> dict[str, Any]:
        """The person says they signed in, or gave up: the browser is closed and the inbox checked."""
        with self._lock:
            if self._loop is not None and self._event is not None:
                self._loop.call_soon_threadsafe(self._event.set)
        return self.state()

    def _run(self, provider: str) -> None:
        try:
            asyncio.run(self._work(provider))
        except Exception as error:  # noqa: BLE001  Said on the page.
            _record_error(error, self.hub.root)
            self._set(phase="error", error="浏览器没有打开。请关掉其他 AIHawk 窗口后重试。")
        finally:
            with self._lock:
                self._loop = self._event = None
                self._state["running"] = False

    async def _work(self, provider: str) -> None:
        event = asyncio.Event()
        with self._lock:
            self._loop, self._event = asyncio.get_running_loop(), event

        async def wait() -> None:
            self._set(phase="waiting")
            await event.wait()
            self._set(phase="checking")

        session = self.hub.session(self.workspace)
        try:
            reached = await session.sign_in_mail(provider, wait)
        finally:
            session.close()
        self._set(phase="done", reached=reached)


class Person:
    """Everything the page does for one person: their store and tracking, sending, searching, the mailbox sign-in."""

    def __init__(self, hub: Hub, entry: dict[str, str]) -> None:
        self.hub = hub
        self.id = entry["id"]
        self.workspace = Path(entry["workspace"])
        self.data = Data(self.workspace, hub.library)
        self.sender = Sender(self.workspace, self.data, hub.session, log_dir=hub.root)
        self.finder = Finder(self.workspace, hub)
        self.mail = MailSignIn(self.workspace, hub)

    @property
    def name(self) -> str:
        entry = self.hub.users.get(self.id)
        return entry["name"] if entry else self.id

    def busy(self) -> str:
        """What of theirs holds the browser now, in plain words; empty when nothing does."""
        if self.sender.state()["running"]:
            return "正在发送申请"
        if self.mail.state()["running"]:
            return "邮箱登录窗口开着"
        return ""

    def profile(self) -> dict[str, Any]:
        """What the settings show: the resume and what was read from it, the search, the mailbox, and the person's answers."""
        config = load_config(self.workspace)
        store = Store(config.store_file)
        try:
            answers = store.answers()
        finally:
            store.close()
        kept = config.data_dir / "cvs" / CV_NAME_FILE
        return {
            # As Session.needs_resume tells it: the resume's text, and a search read from it.
            "has_resume": config.cv.is_file() and bool(config.filters.include),
            "resume_name": kept.read_text(encoding="utf-8").strip() if kept.is_file() else "",
            "goals": config.goals,
            "titles": [{"id": node.id, "forms": [form.text for form in node.forms][:8]} for node in config.filters.plan.nodes],
            "places": list(config.filters.locations),
            "exclude": list(config.filters.exclude),
            "max_age_days": config.max_age_days,
            "email": answers.get("email", ""),
            "mail_provider": answers.get("mail_provider", ""),
            "answers": {name: value for name, value in answers.items() if name not in OWN_ANSWERS},
        }

    def change(self, settings: dict[str, Any]) -> None:
        """The search's settings as the page sends them: places and words to leave out as lists, the days as a number."""
        if "max_age_days" in settings:
            days = settings["max_age_days"]
            if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 60:
                raise ValueError("天数要在 1 到 60 之间")
        for name in ("places", "exclude"):
            if name in settings and (not isinstance(settings[name], list) or not all(isinstance(word, str) for word in settings[name])):
                raise ValueError(f"{name} 要是一组词")
        for name in ("places", "exclude", "max_age_days"):
            if name in settings:
                value = settings[name]
                save_setting(load_config(self.workspace), name, ", ".join(value) if isinstance(value, list) else str(value))

    def answer(self, name: object, value: object) -> None:
        """One answer the forms are told about the person; an empty value removes it."""
        if not isinstance(name, str) or not isinstance(value, str) or not name.strip():
            raise ValueError("回答要有名称和内容")
        name = re.sub(r"\s+", "_", name.strip().lower())
        if not name or len(name) > 60:
            raise ValueError("请填写回答的名称（最多 60 个字）")
        if name in OWN_ANSWERS:
            raise ValueError("这个名称是 AIHawk 自己用的，换一个")
        if len(value) > ANSWER_CHARS:
            raise ValueError(f"回答最多 {ANSWER_CHARS} 个字")
        store = Store(load_config(self.workspace).store_file)
        try:
            store.set_answer(name, value)
        finally:
            store.close()

    def set_mail(self, email: object, provider: object) -> dict[str, Any]:
        """The address the applications use and where its mailbox is; one AIHawk cannot read is kept for later."""
        if not isinstance(email, str) or "@" not in email or len(email) > 200:
            raise ValueError("请填写邮箱地址")
        email = email.strip()
        chosen = mail_provider(email) or (provider if provider in MAILBOXES else "later")
        store = Store(load_config(self.workspace).store_file)
        try:
            store.set_answer("email", email)
            store.set_answer("mail_provider", chosen)
        finally:
            store.close()
        return self.profile()

    def upload(self, name: object, content: object) -> Path:
        """A resume sent by the page, kept where the search reads it once; its path."""
        if not isinstance(name, str) or not isinstance(content, str) or not content:
            raise ValueError("请选择简历文件")
        suffix = Path(name).suffix.lower()
        if suffix not in RESUME_TYPES:
            raise ValueError("简历要是 PDF、Word（.docx）或文本文件")
        try:
            raw = base64.b64decode(content, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("简历文件没有传完整，请重新选择") from None
        if not raw or len(raw) > RESUME_BYTES:
            raise ValueError("简历文件要小于 10 MB")
        folder = self.workspace / "data" / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        # The person's own name for it: the settings show it. Only its last part, as letters and digits a folder takes.
        safe = re.sub(r"[^\w.\- ]+", "_", Path(name).name)[:80] or f"resume{suffix}"
        path = folder / safe
        path.write_bytes(raw)
        return path

    def counts(self) -> dict[str, int]:
        with self.data._store() as db:
            rows = dict(db.execute("SELECT status, count(*) FROM jobs GROUP BY status").fetchall())
        return {
            "found": sum(count for status, count in rows.items() if status not in TRIED),
            "applied": sum(rows.get(status, 0) for status in SUBMITTED),
            "tried": sum(rows.get(status, 0) for status in TRIED),
        }


class Hub:
    """The dashboard's world: who uses it, the key and job database they share, and each one's Person. One batch is sent at a
    time for everyone: what an application costs is counted by one counter for the whole program (llm.SPEND), which two
    batches at once would mix."""

    def __init__(self, root: Path, legacy: Path | None = None, session: Callable[[Path], Any] | None = None,
                 key_check: Callable[[str], Awaitable[str]] = check_key) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.users = Users(root)
        self.settings_path = root / SETTINGS_FILE
        settings = _read_json(self.settings_path, {})
        settings = settings if isinstance(settings, dict) else {}
        self.key_path = root / KEY_FILE
        if not self.users.all() and legacy is not None and (legacy / "data" / "jobs.db").is_file():
            # The first start: the one person AIHawk served so far is the first user, their folder left where it is, and their
            # job database and key shared from now on, so that nothing is downloaded or typed again.
            self.users.add("我", workspace=legacy, user_id="me")
            if "library" not in settings and (legacy / "data" / "library" / STATE).is_file():
                settings["library"] = str(legacy / "data" / "library")
            if not self.key_path.is_file() and (legacy / KEY_FILE).is_file():
                self._write_key((legacy / KEY_FILE).read_text(encoding="utf-8-sig").strip())
        settings.setdefault("library", str(root / "library"))
        _write_json(self.settings_path, settings)
        self.library = Library(Path(settings["library"]))
        self.database = str(settings.get("database") or DEFAULT_DATABASE)
        self._session = session
        self._key_check = key_check
        if self.key_path.is_file() and not os.environ.get(KEY_ENV, "").strip():
            os.environ[KEY_ENV] = self.key_path.read_text(encoding="utf-8-sig").strip()
        self.syncer = Syncer(self)
        self._lock = threading.Lock()
        self._people: dict[str, Person] = {}

    def session(self, workspace: Path) -> Any:
        """A Session on one person's workspace that reads the shared job database."""
        return self._session(workspace) if self._session is not None else Session(workspace, self.library)

    def person(self, user_id: str) -> Person:
        entry = self.users.get(user_id) if user_id else None
        if entry is None:
            raise UnknownUser(user_id)
        with self._lock:
            if user_id not in self._people:
                self._people[user_id] = Person(self, entry)
            return self._people[user_id]

    def add(self, name: object) -> dict[str, Any]:
        entry = self.users.add(name)
        self.person(entry["id"])
        return self.summary(entry)

    def summary(self, entry: dict[str, str]) -> dict[str, Any]:
        person = self.person(entry["id"])
        try:
            counts = person.counts()
        except sqlite3.Error:
            counts = {"found": 0, "applied": 0, "tried": 0}
        has_resume = (person.workspace / "cv.md").is_file()
        return {"id": entry["id"], "name": entry["name"], "has_resume": has_resume, **counts,
                "sending": person.sender.state()["running"], "searching": person.finder.state()["running"]}

    def everyone(self) -> list[dict[str, Any]]:
        return [self.summary(entry) for entry in self.users.all()]

    def max_days(self) -> int:
        """The days the shared database is downloaded for: the longest anyone searches."""
        days = []
        for entry in self.users.all():
            try:
                days.append(load_config(Path(entry["workspace"])).max_age_days)
            except ConfigError:
                continue
        return max(days, default=DEFAULT_MAX_AGE_DAYS)

    def key_set(self) -> bool:
        return bool(os.environ.get(KEY_ENV, "").strip())

    def save_key(self, key: object) -> None:
        if not isinstance(key, str) or not key.strip() or len(key.strip()) > 400 or any(char.isspace() for char in key.strip()):
            raise ValueError("请粘贴完整的 OrcaRouter key")
        key = key.strip()
        if why := asyncio.run(self._key_check(key)):
            raise ValueError(why)
        self._write_key(key)

    def _write_key(self, key: str) -> None:
        self.key_path.touch(mode=0o600, exist_ok=True)
        self.key_path.write_text(key, encoding="utf-8")
        os.environ[KEY_ENV] = key

    def send(self, person: Person, job_ids: object) -> dict[str, Any]:
        with self._lock:
            others = [other for other in self._people.values() if other is not person and other.sender.state()["running"]]
            if others:
                raise Busy(f"{others[0].name} 正在发送申请。同一时间只能有一个人在发送，请等那批结束。")
            if person.mail.state()["running"]:
                raise Busy("邮箱登录窗口还开着：先在「设置」里点「我已登录」。")
            return person.sender.start(job_ids)

    def search(self, person: Person, resume: Path | None = None, rounds: int = FIRST_ROUNDS, search: bool = True) -> dict[str, Any]:
        state = person.finder.start(resume, rounds, search)
        if search:
            self.syncer.ensure()
        return state


def handler(hub: Hub, port: int) -> type[BaseHTTPRequestHandler]:
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

        def _person(self) -> Person:
            return hub.person(parse_qs(urlsplit(self.path).query).get("user", [""])[0])

        def do_GET(self) -> None:
            if not self._allowed():
                return self._send(HTTPStatus.FORBIDDEN, b"", "text/plain")
            path = urlsplit(self.path).path
            try:
                if path == "/":
                    return self._send(HTTPStatus.OK, PAGE.read_bytes(), "text/html; charset=utf-8")
                # Shared by everyone.
                if path == "/api/users":
                    return self._json({"users": hub.everyone(), "key_set": hub.key_set(), "library": hub.syncer.state()})
                if not path.startswith("/api/"):
                    return self._send(HTTPStatus.NOT_FOUND, b"", "text/plain")
                # One person's.
                person = self._person()
                data, sender = person.data, person.sender
                if path == "/api/profile":
                    return self._json({**person.profile(), "key_set": hub.key_set(), "mail_sign_in": person.mail.state()})
                if path == "/api/search":
                    return self._json({**person.finder.state(), "library": hub.syncer.state()})
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
            except UnknownUser:
                return self._json({"error": "请先选择用户"}, HTTPStatus.BAD_REQUEST)
            except (sqlite3.Error, ConfigError) as error:
                return self._json({"error": f"读取失败：{error}"}, HTTPStatus.SERVICE_UNAVAILABLE)
            self._send(HTTPStatus.NOT_FOUND, b"", "text/plain")

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            # The body is read before any answer, even a refusal: one left unread makes Windows drop the connection, answer and all.
            try:
                raw = self.rfile.read(min(int(self.headers.get("Content-Length", "0")), RESUME_BODY if path == "/api/resume" else SMALL_BODY))
            except ValueError:
                raw = b""
            # JSON only: a form on another site cannot send it without the browser asking first, and this server never says yes.
            if not self._allowed() or self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self._send(HTTPStatus.FORBIDDEN, b"", "text/plain")
            if path not in POSTS:
                return self._send(HTTPStatus.NOT_FOUND, b"", "text/plain")
            uploaded: Path | None = None
            try:
                try:
                    change = json.loads(raw or b"{}")
                except ValueError:
                    return self._json({"error": "请求不完整，请重试"}, HTTPStatus.BAD_REQUEST)
                if not isinstance(change, dict):
                    return self._json({"error": "send an object"}, HTTPStatus.BAD_REQUEST)
                # Shared by everyone.
                if path == "/api/users":
                    return self._json(hub.add(change.get("name")), HTTPStatus.CREATED)
                if path == "/api/key":
                    hub.save_key(change.get("key"))
                    return self._json({"key_set": True})
                # One person's.
                person = self._person()
                data, sender = person.data, person.sender
                if path == "/api/users/rename":
                    return self._json(hub.summary(hub.users.rename(person.id, change.get("name"))))
                if path == "/api/resume":
                    if person.finder.state()["running"]:
                        raise Busy("正在搜索或打分，请等它完成后再换简历")
                    uploaded = person.upload(change.get("name"), change.get("data"))
                    state = hub.search(person, resume=uploaded)
                    uploaded = None  # the search owns it now, and removes it once read
                    return self._json(state, HTTPStatus.ACCEPTED)
                if path == "/api/settings":
                    person.change(change)
                    if person.profile()["has_resume"] and not person.finder.state()["running"]:
                        hub.search(person)
                    return self._json(person.profile())
                if path == "/api/answers":
                    person.answer(change.get("name"), change.get("value"))
                    return self._json(person.profile())
                if path == "/api/mail":
                    return self._json(person.set_mail(change.get("email"), change.get("provider")))
                if path == "/api/mail/signin":
                    if why := person.busy():
                        raise Busy(f"现在不能打开浏览器：{why}")
                    return self._json(person.mail.start(person.profile()["mail_provider"]), HTTPStatus.ACCEPTED)
                if path == "/api/mail/signin/done":
                    return self._json(person.mail.done())
                if path == "/api/search":
                    return self._json(hub.search(person), HTTPStatus.ACCEPTED)
                if path == "/api/score/more":
                    return self._json(hub.search(person, rounds=1, search=False), HTTPStatus.ACCEPTED)
                if path == "/api/apply":
                    return self._json(hub.send(person, change.get("ids")), HTTPStatus.ACCEPTED)
                if path == "/api/apply/stop":
                    return self._json(sender.stop())
                if path == "/api/apply/terminate":
                    return self._json(sender.terminate())
                if path == "/api/manual":
                    if sender.state()["running"] and self._job_id() in {job["id"] for job in sender.state()["queue"]}:
                        return self._json({"error": "这个职位在正在发送的这批里，等这批结束后再标记"}, HTTPStatus.CONFLICT)
                    return self._json(data.mark_manual(self._job_id()))
                if path == "/api/manual/undo":
                    data.undo_manual(self._job_id())
                    return self._json({"ok": True})
                self._json(data.save(self._job_id(), change))
            except UnknownUser:
                self._json({"error": "请先选择用户"}, HTTPStatus.BAD_REQUEST)
            except Busy as error:
                self._json({"error": str(error) or "正在进行中，请稍后"}, HTTPStatus.CONFLICT)
            except LookupError as error:
                words = {"/api/apply": str(error), "/api/manual": "这个职位已经申请过或不在列表里了，请刷新", "/api/manual/undo": "只有手动标记的申请可以撤销"}
                self._json({"error": words.get(path, "not found")}, HTTPStatus.NOT_FOUND)
            except (ValueError, ConfigError) as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            except sqlite3.Error as error:
                self._json({"error": f"没有保存：{error}"}, HTTPStatus.SERVICE_UNAVAILABLE)
            finally:
                if uploaded is not None:
                    uploaded.unlink(missing_ok=True)

    return Handler


POSTS = (
    "/api/users", "/api/users/rename", "/api/key", "/api/resume", "/api/settings", "/api/answers", "/api/mail", "/api/mail/signin",
    "/api/mail/signin/done", "/api/search", "/api/score/more", "/api/job", "/api/apply", "/api/apply/stop", "/api/apply/terminate",
    "/api/manual", "/api/manual/undo",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="A dashboard of AIHawk for everyone on this computer, in your browser.")
    parser.add_argument("--root", type=Path, default=default_home().resolve().parent / "aihawk-users",
                        help="where the people, their shared key and job database are kept (default: aihawk-users beside AIHawk's folder)")
    parser.add_argument("--home", type=Path, default=default_home(), help="AIHawk's own folder, brought in as the first person on the first start")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-open", action="store_true", help="do not open the browser")
    options = parser.parse_args()
    hub = Hub(options.root.resolve(), legacy=options.home.resolve() if options.home.exists() else None)
    server = ThreadingHTTPServer(("127.0.0.1", options.port), handler(hub, options.port))
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
