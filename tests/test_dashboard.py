"""The dashboard's jobs found: the list, a posting, and sending the ones chosen through AIHawk's own Session."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from aihawk.apply import SUBMITTING, Outcome
from aihawk.database import Job
from aihawk.store import Store
from fakes import load_script

dashboard = load_script("scripts", "dashboard")


def job(name: str, lexical: float = 0.5, description: str = "") -> Job:
    url = f"https://jobs.example.com/{name}"
    return Job(url, f"{name} Inc", f"{name} engineer", "Remote", url, url, description, lexical)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A folder as AIHawk leaves it: a key, a resume, and jobs found, scored, set aside and tried."""
    monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-test")
    (tmp_path / "cv.md").write_text("A CV", encoding="utf-8")
    store = Store(tmp_path / "data" / "jobs.db")
    store.add_all([job("low", 0.9), job("high", 0.1, "Build models."), job("unscored", 0.8), job("other", 0.2), job("sent"), job("aside", 0.3)])
    store.set_score(job("low").id, 2.0)
    store.set_score(job("high").id, 4.0)
    store.set_score(job("other").id, 2.0)
    store.set_status(job("sent").id, "submitted")
    store.set_statuses([job("aside").id], "passed_over")
    store.close()
    return tmp_path


def wait_until_done(sender: Any) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while (state := sender.state())["running"]:
        assert time.monotonic() < deadline, "the batch did not end"
        time.sleep(0.02)
    return state


class FakeSession:
    """AIHawk's Session as the sender uses it: the store to look a job up, apply, close. Each application ends as told."""

    made: ClassVar[list[FakeSession]] = []

    def __init__(self, home: Path, outcomes: dict[str, Outcome] | None = None, gate: threading.Event | None = None, clicks: bool = False) -> None:
        self.clicks = clicks
        self.store = Store(home / "data" / "jobs.db")
        self.outcomes = outcomes or {}
        self.gate = gate
        self.applied: list[str] = []
        self.closed = False
        FakeSession.made.append(self)

    async def apply(self, item: Any, log: Any, note: Any = None) -> Outcome:
        if self.clicks and (submitting := SUBMITTING.get()) is not None:
            # As the real one does just before it clicks to send.
            submitting["clicked"] = True
        if self.gate is not None:
            while not self.gate.is_set():
                await asyncio.sleep(0.01)
        self.applied.append(item.job.id)
        note("The AI service is not answering: waiting 20 seconds…")
        outcome = self.outcomes.get(item.job.id, Outcome("submitted", "filled", reason=""))
        self.store.set_status(item.job.id, outcome.status, outcome.summary, outcome.reason)
        return outcome

    def close(self) -> None:
        self.closed = True
        self.store.close()


def test_the_jobs_found_are_the_untried_ones_best_first(home: Path) -> None:
    found = dashboard.Data(home).found()
    names = [item["company"] for item in found]
    # Scored by score, the title's fit among equal ones, then the unscored by title fit; what was tried is not among them.
    assert names == ["high Inc", "low Inc", "other Inc", "unscored Inc", "aside Inc"]
    assert found[0]["status_words"] == "已打分" and found[0]["score"] == 4.0
    assert found[3]["score"] is None and found[3]["status_words"] == "等待打分"
    assert found[4]["status_words"] == "AI 未给出判断"


def test_a_posting_is_read_from_the_store_and_a_tried_job_is_not_one_found(home: Path) -> None:
    data = dashboard.Data(home)
    assert data.found_details(job("high").id) == {"description": "Build models."}
    assert data.found_details(job("unscored").id) == {"description": ""}
    assert data.found_details(job("sent").id) is None


@pytest.mark.parametrize("ids", [None, [], "x", [1], [""]])
def test_a_batch_needs_a_list_of_jobs(home: Path, ids: Any) -> None:
    with pytest.raises(ValueError):
        dashboard.Sender(home, dashboard.Data(home), FakeSession).start(ids)


def test_a_batch_has_a_limit(home: Path) -> None:
    with pytest.raises(ValueError, match="最多"):
        dashboard.Sender(home, dashboard.Data(home), FakeSession).start([f"id{n}" for n in range(dashboard.BATCH + 1)])


def test_a_job_already_sent_is_not_sent_again(home: Path) -> None:
    with pytest.raises(LookupError):
        dashboard.Sender(home, dashboard.Data(home), FakeSession).start([job("high").id, job("sent").id])


def test_nothing_is_sent_without_a_key(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ORCAROUTER_API_KEY")
    sender = dashboard.Sender(home, dashboard.Data(home), FakeSession)
    assert "key" in sender.blocked()
    with pytest.raises(ValueError, match="key"):
        sender.start([job("high").id])


def test_the_chosen_jobs_are_sent_in_order_and_each_result_is_said(home: Path) -> None:
    refused = Outcome("blocked", "a captcha", reason="captcha")
    sender = dashboard.Sender(home, dashboard.Data(home), lambda path: FakeSession(path, {job("low").id: refused}))
    started = sender.start([job("high").id, job("low").id, job("high").id])
    assert started["running"] and started["total"] == 2
    state = wait_until_done(sender)
    session = FakeSession.made[-1]
    assert session.applied == [job("high").id, job("low").id] and session.closed
    assert [(r["company"], r["status"], r["reason_words"]) for r in state["results"]] == [
        ("high Inc", "submitted", ""), ("low Inc", "blocked", "验证码拦截")]
    assert state["error"] == "" and state["finished"]
    # What AIHawk recorded is what the page lists: the two are tried now, and no longer among the jobs found.
    assert {item["company"] for item in dashboard.Data(home).found()} == {"other Inc", "unscored Inc", "aside Inc"}


def test_a_finished_credit_stops_the_batch(home: Path) -> None:
    broke = Outcome("failed", "no credit", reason="no_credit")
    sender = dashboard.Sender(home, dashboard.Data(home), lambda path: FakeSession(path, {job("high").id: broke}))
    sender.start([job("high").id, job("low").id])
    state = wait_until_done(sender)
    assert [r["company"] for r in state["results"]] == ["high Inc"]
    assert "余额" in state["error"]


def test_stop_ends_the_batch_after_the_application_under_way(home: Path) -> None:
    gate = threading.Event()
    sender = dashboard.Sender(home, dashboard.Data(home), lambda path: FakeSession(path, gate=gate))
    sender.start([job("high").id, job("low").id, job("other").id])
    with pytest.raises(dashboard.Busy):
        sender.start([job("unscored").id])
    deadline = time.monotonic() + 10
    while sender.state()["number"] != 1:
        assert time.monotonic() < deadline, "the first application did not start"
        time.sleep(0.01)
    assert sender.stop()["stopping"]
    gate.set()
    state = wait_until_done(sender)
    assert [r["company"] for r in state["results"]] == ["high Inc"]
    assert not state["stopping"]


def _terminated(home: Path, clicks: bool) -> dict[str, Any]:
    never = threading.Event()
    sender = dashboard.Sender(home, dashboard.Data(home), lambda path: FakeSession(path, gate=never, clicks=clicks))
    sender.start([job("high").id, job("low").id])
    deadline = time.monotonic() + 10
    while sender.state()["number"] != 1:
        assert time.monotonic() < deadline, "the first application did not start"
        time.sleep(0.01)
    assert sender.terminate()["terminating"]
    state = wait_until_done(sender)
    assert FakeSession.made[-1].applied == [] and FakeSession.made[-1].closed
    assert not state["terminating"] and state["error"] == ""
    return state


def test_an_application_ended_before_the_send_goes_back_among_the_jobs_found(home: Path) -> None:
    state = _terminated(home, clicks=False)
    assert [(r["company"], r["status"], r["status_words"]) for r in state["results"]] == [("high Inc", "back", "已终止")]
    data = dashboard.Data(home)
    found = next(item for item in data.found() if item["id"] == job("high").id)
    # As it was: scored, with its score, to choose again; and no record of it among the applications.
    assert (found["status"], found["score"]) == ("scored", 4.0)
    assert job("high").id not in {item["id"] for item in data.items()}
    # The job after it was never started, and is still there to choose.
    assert job("low").id in {item["id"] for item in data.found()}


def test_an_application_ended_after_the_send_was_clicked_is_kept_as_maybe_sent(home: Path) -> None:
    state = _terminated(home, clicks=True)
    assert [(r["company"], r["status"]) for r in state["results"]] == [("high Inc", "unconfirmed")]
    data = dashboard.Data(home)
    assert job("high").id not in {item["id"] for item in data.found()}
    kept = next(item for item in data.items() if item["id"] == job("high").id)
    assert kept["result"] == "unconfirmed" and kept["reason_words"] == "被你终止（可能已发出）"
    assert "已经点了提交按钮" in data.details(job("high").id)["filled"]


def test_terminate_when_nothing_is_sent_changes_nothing(home: Path) -> None:
    state = dashboard.Sender(home, dashboard.Data(home), FakeSession).terminate()
    assert not state["running"] and not state["terminating"]


def test_a_job_applied_to_by_hand_is_listed_and_tracked_as_sent(home: Path) -> None:
    data = dashboard.Data(home)
    item = data.mark_manual(job("low").id)
    assert item["result"] == "submitted" and item["manual"] and item["result_words"] == "手动申请"
    assert item["stage"] == "waiting" and item["group"] == "waiting" and item["when"]
    assert job("low").id not in {found["id"] for found in data.found()}
    assert "手动完成" in data.details(job("low").id)["filled"]
    # AIHawk sees it as sent: it never sends it again, and applied.csv lists it.
    store = Store(home / "data" / "jobs.db")
    assert store.get(job("low").id).status == "submitted"
    store.close()
    assert "low Inc" in (home / "applied.csv").read_text(encoding="utf-8-sig")
    # It is tracked as any other: a stage, a date, notes.
    assert data.save(job("low").id, {"stage": "interviewing", "notes": "phone screen"})["group"] == "active"


def test_a_deleted_record_leaves_the_page_and_is_never_found_or_sent_again(home: Path) -> None:
    data = dashboard.Data(home)
    data.mark_manual(job("low").id)
    data.save(job("low").id, {"notes": "to go"})
    assert {item["company"] for item in data.items()} == {"sent Inc", "low Inc"}
    assert data.delete([job("low").id, job("sent").id, "not one"]) == 2
    assert data.items() == []
    # AIHawk's store keeps both as tried: no search brings them back to send.
    assert job("low").id not in {item["id"] for item in data.found()}
    store = Store(home / "data" / "jobs.db")
    assert store.get(job("low").id).status == "submitted"
    # Tried again by AIHawk afterwards, a job is a record again, with nothing of the deleted tracking.
    time.sleep(1.1)
    store.set_status(job("sent").id, "submitted", "sent again")
    store.close()
    assert [(item["company"], item["notes"]) for item in data.items()] == [("sent Inc", "")]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node checks the page's script")
def test_the_page_s_script_parses(tmp_path: Path) -> None:
    page = (Path(__file__).resolve().parents[1] / "scripts" / "dashboard.html").read_text(encoding="utf-8")
    script = tmp_path / "page.js"
    script.write_text(page[page.index("<script>") + len("<script>") : page.rindex("</script>")], encoding="utf-8")
    checked = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True, encoding="utf-8", check=False)
    assert checked.returncode == 0, checked.stderr


def test_nothing_to_delete_is_said(home: Path) -> None:
    data = dashboard.Data(home)
    for wrong in (None, [], "x", [1]):
        with pytest.raises(ValueError):
            data.delete(wrong)
    with pytest.raises(LookupError):
        data.delete(["not one"])


def test_the_page_deletes_records(server: Any) -> None:
    status, body = call(server.at("/api/job/delete"), {"ids": [job("sent").id]})
    assert status == 200 and body == {"deleted": 1}
    assert call(server.at("/api/data"))[1]["items"] == []
    status, body = call(server.at("/api/job/delete"), {"ids": [job("sent").id]})
    assert status == 404 and "刷新" in body["error"]


def test_a_job_already_tried_cannot_be_marked_by_hand(home: Path) -> None:
    with pytest.raises(LookupError):
        dashboard.Data(home).mark_manual(job("sent").id)


def test_a_mark_by_hand_can_be_undone_and_the_job_is_found_again_as_it_was(home: Path) -> None:
    data = dashboard.Data(home)
    data.mark_manual(job("high").id)
    data.save(job("high").id, {"notes": "a mistake"})
    data.undo_manual(job("high").id)
    found = next(item for item in data.found() if item["id"] == job("high").id)
    assert found["status"] == "scored" and found["score"] == 4.0
    assert job("high").id not in {item["id"] for item in data.items()}
    # Tracked again from nothing, if it is marked again.
    assert data.mark_manual(job("high").id)["notes"] == ""
    data.mark_manual(job("unscored").id)
    data.undo_manual(job("unscored").id)
    assert next(item for item in data.found() if item["id"] == job("unscored").id)["status"] == "new"


def test_only_a_mark_by_hand_can_be_undone(home: Path) -> None:
    with pytest.raises(LookupError):
        dashboard.Data(home).undo_manual(job("sent").id)


def test_the_page_marks_and_unmarks_a_job_by_hand(server: Any) -> None:
    status, body = call(server.at(f"/api/manual?id={job('other').id}"), {})
    assert status == 200 and body["manual"]
    status, body = call(server.at(f"/api/manual?id={job('other').id}"), {})
    assert status == 404 and "申请过" in body["error"]
    status, _ = call(server.at(f"/api/manual/undo?id={job('other').id}"), {})
    assert status == 200
    status, body = call(server.at(f"/api/manual/undo?id={job('sent').id}"), {})
    assert status == 404 and "手动" in body["error"]


def test_a_failure_of_the_whole_batch_is_said_in_a_line(home: Path) -> None:
    def broken(path: Path) -> Any:
        raise RuntimeError("the settings cannot be read")

    sender = dashboard.Sender(home, dashboard.Data(home), broken)
    sender.start([job("high").id])
    state = wait_until_done(sender)
    assert "RuntimeError" in state["error"] and not state["running"]


async def accept_any_key(key: str) -> str:
    return ""


@pytest.fixture
def server(home: Path) -> Any:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), lambda *args: None)  # type: ignore[arg-type]
    port = httpd.server_address[1]
    hub = dashboard.Hub(home.parent / "everyone", legacy=home, session=FakeSession, key_check=accept_any_key)
    httpd.RequestHandlerClass = dashboard.handler(hub, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    yield SimpleNamespace(base=base, hub=hub, at=lambda path, user="me": f"{base}{path}{'&' if '?' in path else '?'}user={user}")
    httpd.shutdown()
    httpd.server_close()


def call(url: str, body: Any = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})}, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        text = error.read()
        return error.code, json.loads(text) if text else None


def test_the_page_lists_the_jobs_found_and_a_posting(server: Any) -> None:
    status, body = call(server.at("/api/found"))
    assert status == 200 and len(body["items"]) == 5 and body["blocked"] == "" and body["batch"] == dashboard.BATCH
    status, body = call(server.at(f"/api/found/job?id={job('high').id}"))
    assert status == 200 and body["description"] == "Build models."


def test_the_page_starts_a_batch_and_reads_how_it_goes(server: Any) -> None:
    status, body = call(server.at("/api/apply"), {"ids": [job("high").id]})
    assert status == 202 and body["total"] == 1
    deadline = time.monotonic() + 10
    while (state := call(server.at("/api/apply"))[1])["running"]:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert state["results"][0]["status"] == "submitted"


def test_sending_needs_json_from_this_page(server: Any) -> None:
    status, _ = call(server.at("/api/apply"), {"ids": [job("high").id]}, {"Origin": "https://elsewhere.example"})
    assert status == 403
    status, _ = call(server.at("/api/apply"), {"ids": [job("high").id]}, {"Content-Type": "text/plain"})
    assert status == 403
    status, body = call(server.at("/api/apply"), {"ids": [job("sent").id]})
    assert status == 404 and "申请过" in body["error"]
