"""The dashboard for several people on one computer: who they are, the key and job database they share, and each one's
own resume, search, scores and applications."""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import APIConnectionError

from aihawk.config import KEY_ENV
from aihawk.session import Session
from aihawk.store import Store
from fakes import load_script
from test_dashboard import FakeSession, job
from test_tui import CV_TEXT, ScriptedModelForApp, published

dashboard = load_script("scripts", "dashboard")


async def accept_any_key(key: str) -> str:
    return ""


async def refuse_every_key(key: str) -> str:
    return "OrcaRouter 不认这个 key，请检查后重新粘贴。"


@pytest.fixture
def legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """AIHawk's own folder as one person left it: a key, a job database here, two jobs found and one sent."""
    monkeypatch.delenv(KEY_ENV, raising=False)
    folder = tmp_path / "aihawk"
    (folder / "data" / "library").mkdir(parents=True)
    (folder / "data" / "library" / "state.json").write_text("{}", encoding="utf-8")
    (folder / "key").write_text("sk-orca-shared", encoding="utf-8")
    (folder / "cv.md").write_text("A CV", encoding="utf-8")
    store = Store(folder / "data" / "jobs.db")
    store.add_all([job("high"), job("low"), job("sent")])
    store.set_score(job("high").id, 4.0)
    store.set_status(job("sent").id, "submitted")
    store.close()
    return folder


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "everyone"


def test_the_first_start_brings_in_the_one_person_so_far_with_their_database_and_key(root: Path, legacy: Path) -> None:
    hub = dashboard.Hub(root, legacy=legacy, session=FakeSession, key_check=accept_any_key)
    [me] = hub.everyone()
    assert (me["id"], me["name"], me["found"], me["applied"], me["has_resume"]) == ("me", "我", 2, 1, True)
    # Their folder is left where it is; their job database becomes everyone's, so nothing is downloaded again.
    assert Path(hub.users.get("me")["workspace"]) == legacy
    assert Path(json.loads((root / "settings.json").read_text(encoding="utf-8"))["library"]) == legacy / "data" / "library"
    assert (root / "key").read_text(encoding="utf-8") == "sk-orca-shared" and os.environ[KEY_ENV] == "sk-orca-shared"
    # Started again, it does not bring them in twice.
    assert [person["id"] for person in dashboard.Hub(root, legacy=legacy, session=FakeSession).everyone()] == ["me"]


def test_people_are_added_and_renamed_each_with_a_folder_of_their_own(root: Path) -> None:
    hub = dashboard.Hub(root, session=FakeSession)
    first = hub.add("小王")
    second = hub.add("Ada Rossi")
    third = hub.add("小李")
    assert [first["id"], second["id"], third["id"]] == ["user", "ada-rossi", "user-2"]
    assert Path(hub.users.get("user")["workspace"]) == root / "users" / "user"
    assert (root / "users" / "user" / "data" / "jobs.db").is_file()
    assert first["found"] == 0 and not first["has_resume"]
    with pytest.raises(ValueError, match="同名"):
        hub.add("小王")
    with pytest.raises(ValueError):
        hub.add("   ")
    assert hub.users.rename("user", "王小明")["name"] == "王小明"
    with pytest.raises(ValueError, match="同名"):
        hub.users.rename("user-2", "王小明")
    # Kept on disk: a new start knows them.
    assert [person["name"] for person in dashboard.Hub(root, session=FakeSession).everyone()] == ["王小明", "Ada Rossi", "小李"]


def test_the_shared_key_is_checked_before_it_is_kept(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    hub = dashboard.Hub(root, session=FakeSession, key_check=refuse_every_key)
    with pytest.raises(ValueError, match="不认"):
        hub.save_key("sk-orca-wrong")
    assert not hub.key_set() and not (root / "key").exists()
    with pytest.raises(ValueError):
        hub.save_key("two words")
    hub._key_check = accept_any_key
    hub.save_key("  sk-orca-right ")
    assert hub.key_set() and os.environ[KEY_ENV] == "sk-orca-right" and (root / "key").read_text(encoding="utf-8") == "sk-orca-right"


def test_one_batch_is_sent_at_a_time_for_everyone(root: Path, legacy: Path) -> None:
    gate = threading.Event()
    hub = dashboard.Hub(root, legacy=legacy, session=lambda path: FakeSession(path, gate=gate), key_check=accept_any_key)
    other = hub.add("小王")
    # Someone else's job, in their own store, so that their batch can start.
    store = Store(root / "users" / other["id"] / "data" / "jobs.db")
    store.add_all([job("theirs")])
    store.close()
    (root / "users" / other["id"] / "cv.md").write_text("A CV", encoding="utf-8")
    hub.send(hub.person("me"), [job("high").id])
    with pytest.raises(dashboard.Busy, match="我 正在发送"):
        hub.send(hub.person(other["id"]), [job("theirs").id])
    gate.set()
    deadline = time.monotonic() + 10
    while hub.person("me").sender.state()["running"]:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert hub.send(hub.person(other["id"]), [job("theirs").id])["running"]


def test_a_person_s_settings_answers_and_mail(root: Path, legacy: Path) -> None:
    hub = dashboard.Hub(root, legacy=legacy, session=FakeSession, key_check=accept_any_key)
    person = hub.person(hub.add("小王")["id"])
    person.answer("Notice period", "one month")
    person.answer("visa", "H-1B")
    person.answer("visa", "")
    assert person.profile()["answers"] == {"notice_period": "one month"}
    with pytest.raises(ValueError):
        person.answer("account_password", "x")
    assert person.set_mail("wang@gmail.com", "")["mail_provider"] == "gmail"
    assert person.set_mail("wang@company.com", "outlook")["mail_provider"] == "outlook"
    assert person.set_mail("wang@company.com", "")["mail_provider"] == "later"
    with pytest.raises(ValueError):
        person.set_mail("not an address", "")
    for wrong in ({"max_age_days": 0}, {"max_age_days": "15"}, {"places": "Milan"}):
        with pytest.raises(ValueError):
            person.change(wrong)
    person.change({"places": ["Milan", "Turin"], "exclude": ["Intern"], "max_age_days": 30})
    profile = person.profile()
    assert (profile["places"], profile["exclude"], profile["max_age_days"]) == (["Milan", "Turin"], ["Intern"], 30)
    # The shared database is downloaded for the longest anyone searches.
    assert hub.max_days() == 30
    # The first person's answers are theirs alone.
    assert "notice_period" not in hub.person("me").profile()["answers"]


@pytest.mark.parametrize(("name", "content", "words"), [
    ("cv.exe", base64.b64encode(b"x").decode(), "PDF"),
    ("cv.pdf", "not base64!", "完整"),
    ("cv.pdf", "", "选择"),
])
def test_a_resume_that_cannot_be_read_is_refused(root: Path, name: str, content: str, words: str) -> None:
    hub = dashboard.Hub(root, session=FakeSession)
    person = hub.person(hub.add("小王")["id"])
    with pytest.raises(ValueError, match=words):
        person.upload(name, content)


# ---------- the whole way, with the scripted model and a database published on this computer ----------


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Everyone's dashboard with a key, a published database of four postings, and sessions that ask the scripted model."""
    monkeypatch.setenv(KEY_ENV, "test-key")
    monkeypatch.setattr(dashboard, "RETRY_PAUSES", (0, 0, 0, 0))
    root = tmp_path / "everyone"
    root.mkdir()
    (root / "settings.json").write_text(json.dumps({"database": published(tmp_path)}), encoding="utf-8")
    models: list[ScriptedModelForApp] = []
    hub = dashboard.Hub(root, key_check=accept_any_key)

    def session(workspace: Path) -> Session:
        made = Session(workspace, hub.library)
        made._client = models[-1] if models else ScriptedModelForApp()  # type: ignore[assignment]
        return made

    hub._session = session
    return SimpleNamespace(hub=hub, root=root, models=models, tmp=tmp_path)


def finished(finder: Any) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while (state := finder.state())["running"]:
        assert time.monotonic() < deadline, f"the search never ended: {state}"
        time.sleep(0.05)
    return state


def upload(person: Any, text: str = CV_TEXT) -> Path:
    return person.upload("Ada CV.txt", base64.b64encode(text.encode()).decode())


def test_a_resume_uploaded_on_the_page_is_read_searched_and_scored(world: Any) -> None:
    hub = world.hub
    ada = hub.person(hub.add("Ada")["id"])
    kept = upload(ada)
    hub.search(ada, resume=kept)
    state = finished(ada.finder)
    assert state["phase"] == "done", state
    assert not kept.exists()  # the upload is removed once read
    found = ada.data.found()
    assert [(item["company"], item["score"]) for item in found] == [("Bestco", 4.5), ("Lowly", 3.5), ("Noplugin", 1.5)]
    profile = ada.profile()
    assert profile["has_resume"] and profile["resume_name"] == "Ada CV.txt" and profile["goals"] == "Senior Python work."
    assert profile["titles"][0]["forms"] == ["Python Engineer", "Python Developer"]
    # Someone else on the same computer sees none of it, and the database was downloaded once, into the shared folder.
    bob = hub.person(hub.add("Bob")["id"])
    assert bob.data.found() == [] and not bob.profile()["has_resume"]
    assert (world.root / "library" / "state.json").is_file()
    assert not (Path(hub.users.get(ada.id)["workspace"]) / "data" / "library").exists()


def test_a_new_resume_drops_the_jobs_found_for_the_old_one(world: Any) -> None:
    hub = world.hub
    ada = hub.person(hub.add("Ada")["id"])
    hub.search(ada, resume=upload(ada))
    finished(ada.finder)
    store = Store(ada.workspace / "data" / "jobs.db")
    store.add_all([job("stale")])
    store.close()
    hub.search(ada, resume=upload(ada))
    finished(ada.finder)
    assert "stale Inc" not in {item["company"] for item in ada.data.found()}


def test_a_round_the_ai_service_does_not_answer_is_asked_again(world: Any) -> None:
    class Unsure(ScriptedModelForApp):
        def __init__(self) -> None:
            super().__init__()
            self.silent = 1

        async def post(self, path: str, *, body: dict[str, Any], cast_to: type) -> dict[str, Any]:
            if self.silent:
                self.silent -= 1
                raise APIConnectionError(request=httpx.Request("POST", "https://api.orcarouter.ai/v1" + path))
            return await super().post(path, body=body, cast_to=cast_to)

    world.models.append(Unsure())
    hub = world.hub
    ada = hub.person(hub.add("Ada")["id"])
    hub.search(ada, resume=upload(ada))
    state = finished(ada.finder)
    assert state["phase"] == "done" and state["scored"] == 3, state
    assert world.models[-1].silent == 0


def test_a_search_with_no_resume_says_so(world: Any) -> None:
    hub = world.hub
    bob = hub.person(hub.add("Bob")["id"])
    hub.search(bob)
    state = finished(bob.finder)
    assert state["phase"] == "error" and "简历" in state["error"]


# ---------- the page ----------


@pytest.fixture
def page(root: Path, legacy: Path) -> Any:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), lambda *args: None)  # type: ignore[arg-type]
    port = httpd.server_address[1]
    hub = dashboard.Hub(root, legacy=legacy, session=FakeSession, key_check=accept_any_key)
    httpd.RequestHandlerClass = dashboard.handler(hub, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield SimpleNamespace(base=f"http://127.0.0.1:{port}", hub=hub)
    httpd.shutdown()
    httpd.server_close()


def call(url: str, body: Any = None) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        text = error.read()
        return error.code, json.loads(text) if text else None


def test_the_page_lists_people_and_adds_one(page: Any) -> None:
    status, body = call(f"{page.base}/api/users")
    assert status == 200 and body["key_set"] and [person["id"] for person in body["users"]] == ["me"]
    status, body = call(f"{page.base}/api/users", {"name": "小王"})
    assert status == 201 and body["id"] == "user"
    status, body = call(f"{page.base}/api/users/rename?user=user", {"name": "王小明"})
    assert status == 200 and body["name"] == "王小明"
    status, body = call(f"{page.base}/api/users", {"name": "王小明"})
    assert status == 400 and "同名" in body["error"]


def test_each_person_sees_only_their_own(page: Any) -> None:
    call(f"{page.base}/api/users", {"name": "小王"})
    assert len(call(f"{page.base}/api/found?user=me")[1]["items"]) == 2
    assert call(f"{page.base}/api/found?user=user")[1]["items"] == []
    status, _ = call(f"{page.base}/api/manual?user=me&id={job('high').id}", {})
    assert status == 200
    assert [item["company"] for item in call(f"{page.base}/api/data?user=me")[1]["items"]] == ["high Inc", "sent Inc"]
    assert call(f"{page.base}/api/data?user=user")[1]["items"] == []
    # A job of one person's cannot be marked through another.
    status, _ = call(f"{page.base}/api/manual?user=user&id={job('low').id}", {})
    assert status == 404


@pytest.mark.parametrize("path", ["/api/found", "/api/data", "/api/profile", "/api/search", "/api/apply"])
def test_a_request_without_a_known_person_is_refused(page: Any, path: str) -> None:
    for query in ("", "?user=nobody"):
        status, body = call(f"{page.base}{path}{query}")
        assert status == 400 and "用户" in body["error"]


def test_a_change_without_a_known_person_is_refused(page: Any) -> None:
    for path in ("/api/answers", "/api/search", "/api/apply", "/api/mail/signin"):
        status, body = call(f"{page.base}{path}?user=nobody", {})
        assert status == 400 and "用户" in body["error"], path


def test_the_page_changes_a_person_s_answers_and_rejects_a_bad_resume(page: Any) -> None:
    status, body = call(f"{page.base}/api/answers?user=me", {"name": "salary", "value": "100k"})
    assert status == 200 and body["answers"]["salary"] == "100k"
    status, body = call(f"{page.base}/api/resume?user=me", {"name": "cv.exe", "data": base64.b64encode(b"x").decode()})
    assert status == 400 and "PDF" in body["error"]
    # Nothing of a refused upload is kept.
    uploads = page.hub.person("me").workspace / "data" / "uploads"
    assert not uploads.exists() or not any(uploads.iterdir())


def _install_simplify(workspace: Path) -> None:
    profile = workspace / "browser-profile"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "extensions.json").write_text(json.dumps({"addons": [{"id": "sabre@simplify.jobs", "active": True}]}), encoding="utf-8")


def test_simplify_is_turned_on_only_once_it_is_in_the_person_s_browser(root: Path, legacy: Path) -> None:
    hub = dashboard.Hub(root, legacy=legacy, session=FakeSession, key_check=accept_any_key)
    person = hub.person(hub.add("小王")["id"])
    assert (person.profile()["simplify"], person.profile()["simplify_installed"]) == (False, False)
    with pytest.raises(ValueError, match="安装"):
        person.set_simplify(True)
    _install_simplify(person.workspace)
    person.set_simplify(True)
    assert (person.profile()["simplify"], person.profile()["simplify_installed"]) == (True, True)
    # Each person's own: the first one's browser has no add-on and their switch stays off.
    assert hub.person("me").profile()["simplify"] is False
    person.set_simplify(False)
    assert person.profile()["simplify"] is False
    with pytest.raises(ValueError):
        person.set_simplify("yes")


def test_the_browser_is_opened_for_one_thing_at_a_time(root: Path, legacy: Path) -> None:
    hub = dashboard.Hub(root, legacy=legacy, session=FakeSession, key_check=accept_any_key)
    setup = hub.person("me").mail
    with pytest.raises(ValueError):
        setup.start("chrome")
    with pytest.raises(ValueError, match="Gmail"):
        setup.start("mail", "later")


def test_the_page_turns_simplify_on_and_off(page: Any) -> None:
    status, body = call(f"{page.base}/api/simplify?user=me", {"on": True})
    assert status == 400 and "安装" in body["error"]
    _install_simplify(page.hub.person("me").workspace)
    status, body = call(f"{page.base}/api/simplify?user=me", {"on": True})
    assert status == 200 and body["simplify"] and body["simplify_installed"]
    assert call(f"{page.base}/api/profile?user=me")[1]["simplify"] is True


def _asked(workspace: Path, *items: tuple[str, str]) -> None:
    store = Store(workspace / "data" / "jobs.db")
    store.note_asked(job("form"), [{"question": question, "answer": answer, "options": ["Yes", "No"]} for question, answer in items])
    store.close()


def test_the_questions_the_ai_answered_are_decided_on_the_page_and_added_to_the_file(page: Any) -> None:
    me = page.hub.person("me")
    _asked(me.workspace, ("Are you willing to relocate?", "Yes"), ("Licence number?", ""))
    status, body = call(f"{page.base}/api/questions?user=me")
    assert status == 200 and [item["question"] for item in body["questions"]] == ["Are you willing to relocate?", "Licence number?"]
    key = body["questions"][0]["key"]
    status, body = call(f"{page.base}/api/questions/add?user=me", {"key": key, "answer": "Yes"})
    assert status == 200 and [item["question"] for item in body["questions"]] == ["Licence number?"]
    assert ("Are you willing to relocate?", "Yes") in [(entry["question"], entry["answer"]) for entry in me.profile()["standing"]]
    status, body = call(f"{page.base}/api/questions/ignore?user=me", {"key": body["questions"][0]["key"]})
    assert status == 200 and body["questions"] == []
    # Decided once: a second decision on it says so.
    status, body = call(f"{page.base}/api/questions/add?user=me", {"key": "nothing like it", "answer": "x"})
    assert status == 404 and "处理过" in body["error"]


def test_each_person_decides_on_their_own_questions_into_their_own_file(page: Any) -> None:
    other = page.hub.person(page.hub.add("小王")["id"])
    _asked(other.workspace, ("Do you need a visa?", "No"))
    assert call(f"{page.base}/api/questions?user=me")[1]["questions"] == []
    key = call(f"{page.base}/api/questions?user={other.id}")[1]["questions"][0]["key"]
    call(f"{page.base}/api/questions/add?user={other.id}", {"key": key, "answer": "No"})
    assert (other.workspace / "standing-answers.yaml").is_file()
    assert not (page.hub.person("me").workspace / "standing-answers.yaml").exists()


def test_a_standing_file_that_does_not_read_is_said_in_the_settings(page: Any) -> None:
    me = page.hub.person("me")
    (me.workspace / "standing-answers.yaml").write_text("work: [\n", encoding="utf-8")
    profile = call(f"{page.base}/api/profile?user=me")[1]
    assert profile["standing"] == [] and "not valid YAML" in profile["standing_error"]


def test_the_page_keeps_the_shared_key(page: Any) -> None:
    status, body = call(f"{page.base}/api/key", {"key": "sk-orca-new"})
    assert status == 200 and body["key_set"] and os.environ[KEY_ENV] == "sk-orca-new"


# ---------- from another computer ----------

PUBLIC = "demo.ngrok-free.app"


def outside(url: str, body: Any = None, cookie: str = "", **headers: str) -> tuple[int, Any, str]:
    """A request as the tunnel forwards it: the public address, and the visitor's sign-in cookie if any."""
    sent = {"Host": PUBLIC, "X-Forwarded-For": "203.0.113.7", "Content-Type": "application/json", **headers}
    if body is not None:
        sent.setdefault("Origin", f"https://{PUBLIC}")
    if cookie:
        sent["Cookie"] = f"{dashboard.REMOTE_COOKIE}={cookie}"
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, headers=sent, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            text, kept = response.read(), response.headers.get("Set-Cookie", "")
            status = response.status
    except urllib.error.HTTPError as error:
        text, kept, status = error.read(), error.headers.get("Set-Cookie", ""), error.code
    try:
        answer = json.loads(text) if text else None
    except ValueError:
        answer = text.decode()
    return status, answer, dashboard._cookie(kept, dashboard.REMOTE_COOKIE)


def signed_in(page: Any, user: str = "me") -> str:
    page.hub.remote.set_host(PUBLIC)
    password = call(f"{page.base}/api/remote?user={user}", {"on": True})[1]["password"]
    status, _, cookie = outside(f"{page.base}/api/login", {"password": password})
    assert status == 200 and cookie
    return cookie


def test_nothing_is_open_from_outside_without_a_public_address_and_a_sign_in(page: Any) -> None:
    # No public address set: the tunnel's requests get nothing, even the sign-in page.
    assert outside(f"{page.base}/")[0] == 403
    page.hub.remote.set_host(f"https://{PUBLIC}/")
    assert page.hub.remote.host == PUBLIC
    status, text, _ = outside(f"{page.base}/")
    assert status == 200 and "密码" in text and "投递看板" in text
    for path in ("/api/users", "/api/data?user=me", "/api/found?user=me", "/api/profile?user=me"):
        assert outside(f"{page.base}{path}")[0] == 401, path
    assert outside(f"{page.base}/api/answers?user=me", {"name": "salary", "value": "1"})[0] == 401
    # This computer's own address, but forwarded by the tunnel, is still from outside.
    assert outside(f"{page.base}/api/users", Host=page.base.removeprefix("http://"))[0] == 403


def test_a_password_made_here_signs_in_one_person_who_sees_only_their_own(page: Any) -> None:
    call(f"{page.base}/api/users", {"name": "小王"})
    cookie = signed_in(page)
    status, body, _ = outside(f"{page.base}/api/users", cookie=cookie)
    assert status == 200 and body["remote"] and [person["id"] for person in body["users"]] == ["me"]
    # Whatever person the address names, it is the one signed in.
    assert len(outside(f"{page.base}/api/found?user=user", cookie=cookie)[1]["items"]) == 2
    assert "remote_access" not in outside(f"{page.base}/api/profile", cookie=cookie)[1]
    assert "remote_access" in call(f"{page.base}/api/profile?user=me")[1]
    # Their own things can be changed from outside.
    status, body, _ = outside(f"{page.base}/api/answers?user=user", {"name": "salary", "value": "100k"}, cookie)
    assert status == 200 and body["answers"]["salary"] == "100k"
    assert "salary" not in page.hub.person("user").profile()["answers"]
    assert call(f"{page.base}/api/users")[1]["remote"] is False


@pytest.mark.parametrize("path", ["/api/users", "/api/users/rename", "/api/key", "/api/remote", "/api/mail/signin", "/api/mail/signin/done",
                                  "/api/simplify/setup"])
def test_what_only_this_computer_does_is_refused_from_outside(page: Any, path: str) -> None:
    cookie = signed_in(page)
    status, body, _ = outside(f"{page.base}{path}?user=me", {"name": "x", "key": "sk-orca-x", "on": True}, cookie)
    assert status == 403 and "那台电脑" in body["error"]


def test_a_change_from_another_site_is_refused_even_signed_in(page: Any) -> None:
    cookie = signed_in(page)
    status, _, _ = outside(f"{page.base}/api/answers", {"name": "salary", "value": "1"}, cookie, Origin="https://elsewhere.example")
    assert status == 403


def test_a_wrong_password_is_refused_and_too_many_pause_signing_in(page: Any) -> None:
    page.hub.remote.set_host(PUBLIC)
    password = call(f"{page.base}/api/remote?user=me", {"on": True})[1]["password"]
    for _ in range(dashboard.LOGIN_TRIES):
        status, body, cookie = outside(f"{page.base}/api/login", {"password": "wrong-wrong"})
        assert status == 401 and "密码" in body["error"] and not cookie
    status, body, _ = outside(f"{page.base}/api/login", {"password": password})
    assert status == 409 and "15 分钟" in body["error"]


def test_a_new_password_or_none_ends_the_sign_ins_made_before(page: Any) -> None:
    cookie = signed_in(page)
    assert outside(f"{page.base}/api/users", cookie=cookie)[0] == 200
    again = call(f"{page.base}/api/remote?user=me", {"on": True})[1]["password"]
    assert outside(f"{page.base}/api/users", cookie=cookie)[0] == 401
    status, _, cookie = outside(f"{page.base}/api/login", {"password": again.upper()})
    assert status == 200 and outside(f"{page.base}/api/users", cookie=cookie)[0] == 200
    status, body = call(f"{page.base}/api/remote?user=me", {"on": False})
    assert status == 200 and body == {"on": False, "host": PUBLIC}
    assert outside(f"{page.base}/api/users", cookie=cookie)[0] == 401
    assert outside(f"{page.base}/api/login", {"password": again})[0] == 401


def test_signing_out_clears_the_cookie_and_a_forged_one_is_refused(page: Any) -> None:
    cookie = signed_in(page)
    status, _, cleared = outside(f"{page.base}/api/logout", {}, cookie)
    assert status == 200 and cleared == ""
    user, expires, _ = cookie.rsplit(".", 2)
    assert outside(f"{page.base}/api/users", cookie=f"user.{expires}.{'0' * 64}")[0] == 401
    assert outside(f"{page.base}/api/users", cookie=f"{user}.{int(expires) + 1}.{cookie.rsplit('.', 1)[1]}")[0] == 401
    # The passwords are kept only as hashes, and survive a restart with the public address.
    kept = (page.hub.root / dashboard.REMOTE_FILE).read_text(encoding="utf-8")
    assert PUBLIC in kept and '"hash"' in kept
    assert dashboard.Remote(page.hub.root).who(f"{dashboard.REMOTE_COOKIE}={cookie}") == "me"
