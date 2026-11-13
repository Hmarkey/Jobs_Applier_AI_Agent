"""The person's standing answers: read from their file, told to the model after Simplify and before its own reasoning, and
grown from the questions the model answered itself, which are kept for the person to decide on."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aihawk import apply as apply_module
from aihawk import session as session_module
from aihawk import standing
from aihawk.apply import Outcome, asked_of, run_on_page
from aihawk.config import ConfigError
from aihawk.database import Job
from aihawk.llm import Spend
from aihawk.session import Session
from aihawk.store import Store, question_key
from fakes import ScriptedBrowser, ScriptedModel, tool_call
from test_apply import JOB, SUBMIT, VERIFIED

FILE = """\
# The person's own notes, which must stay.
work_authorization:
  - topic: Right to work
    question: "Are you legally authorized to work in the United States?"
    matches: ["authorized to work", "eligible to work"]
    answer: "Yes"
    choose: ["Yes, I am legally authorized for employment", "Yes"]
  # A comment between entries.
  - question: "Preferred First Name"
    answer: ""
    note: "Leave it empty."
to_confirm:
  - "a line that is no answer"
"""


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apply_module, "SEND_POLL_S", 0)


def write(workspace: Path, text: str = FILE) -> Path:
    path = workspace / standing.FILE
    path.write_text(text, encoding="utf-8")
    return path


def test_the_file_s_answers_are_read_and_its_notes_left_out(tmp_path: Path) -> None:
    write(tmp_path)
    entries = standing.load(tmp_path)
    assert [entry["question"] for entry in entries] == ["Are you legally authorized to work in the United States?", "Preferred First Name"]
    assert entries[0]["choose"] == ["Yes, I am legally authorized for employment", "Yes"]
    assert entries[1]["answer"] == ""


def test_no_file_is_no_standing_answers_and_a_broken_one_says_so(tmp_path: Path) -> None:
    assert standing.load(tmp_path) == [] and standing.brief([]) == ""
    write(tmp_path, "work: [\n")
    with pytest.raises(ConfigError, match="standing-answers.yaml is not valid YAML"):
        standing.load(tmp_path)


def test_the_model_is_told_the_order_and_every_answer(tmp_path: Path) -> None:
    write(tmp_path)
    told = standing.brief(standing.load(tmp_path))
    assert "A field Simplify Copilot already filled stays as it is" in told
    assert "not declined" in told and "not 'Please see my attached CV'" in told
    assert "- Q: Are you legally authorized to work in the United States? | also asked as: authorized to work; eligible to work | answer: 'Yes' | choose: Yes, I am legally authorized for employment; Yes" in told
    assert "- Q: Preferred First Name | answer: (leave empty) | note: Leave it empty." in told


def test_an_answer_is_added_at_the_end_and_the_person_s_writing_stays(tmp_path: Path) -> None:
    path = write(tmp_path)
    standing.append(tmp_path, "Are you willing to relocate?", "Yes", ["Yes", "No"])
    standing.append(tmp_path, "What is your notice period?  ", "Two weeks", [])
    text = path.read_text(encoding="utf-8")
    assert text.startswith(FILE)  # every line of theirs, comments included, as it was
    assert text.count("\nadded:") == 1
    entries = standing.load(tmp_path)
    assert [(entry["question"], entry["answer"], entry["choose"]) for entry in entries[2:]] == [
        ("Are you willing to relocate?", "Yes", ["Yes"]), ("What is your notice period?", "Two weeks", [])]


def test_an_answer_is_added_to_a_new_file_and_under_a_section_of_its_own_when_added_is_not_last(tmp_path: Path) -> None:
    standing.append(tmp_path, "Q one?", "A", [])
    assert [entry["question"] for entry in standing.load(tmp_path)] == ["Q one?"]
    write(tmp_path, FILE.replace("to_confirm:", "added:\n  - question: \"Old?\"\n    answer: \"x\"\nto_confirm:"))
    standing.append(tmp_path, "Q two?", "B", [])
    assert [entry["question"] for entry in standing.load(tmp_path)][-2:] == ["Old?", "Q two?"]


def test_nothing_is_added_to_a_file_that_does_not_read(tmp_path: Path) -> None:
    path = write(tmp_path, "work: [\n")
    with pytest.raises(ConfigError):
        standing.append(tmp_path, "Q?", "A", [])
    assert path.read_text(encoding="utf-8") == "work: [\n"


def test_what_finish_form_lists_is_kept_and_what_is_not_a_question_left_out() -> None:
    assert asked_of(None) == [] and asked_of("x") == []
    assert asked_of([{"question": "  Do you   need a visa? ", "answer": "Yes", "options": ["Yes", "No"]}, {"answer": "x"}, "y", {"question": ""}]) == [
        {"question": "Do you need a visa?", "answer": "Yes", "options": ["Yes", "No"]}]


def test_the_standing_answers_come_after_simplify_and_the_questions_answered_reach_the_outcome(tmp_path: Path) -> None:
    write(tmp_path)
    told = standing.brief(standing.load(tmp_path))
    inferred = [{"question": "Years of Python?", "answer": "5", "options": []}]
    model = ScriptedModel([tool_call("finish_form", status="ready", summary="filled", submit_selector="#send", inferred=inferred)], VERIFIED)
    outcome = asyncio.run(run_on_page(ScriptedBrowser({"#send": SUBMIT}), [], model, "m", JOB, "cv", "answers", Path("cv.pdf"),  # type: ignore[arg-type]
                                      tmp_path / "proof.png", standing=told))
    assert outcome.status == "submitted" and outcome.asked == inferred
    assert model.requests[0]["messages"][1]["content"].endswith(told)


def test_a_form_blocked_for_a_missing_answer_keeps_the_question(tmp_path: Path) -> None:
    inferred = [{"question": "Licence number?", "answer": "", "options": []}]
    model = ScriptedModel([tool_call("finish_form", status="blocked", blocked_by="missing_info", summary="no licence", submit_selector="", inferred=inferred)])
    outcome = asyncio.run(run_on_page(ScriptedBrowser(), [], model, "m", JOB, "cv", "a", Path("cv.pdf"), tmp_path / "proof.png"))  # type: ignore[arg-type]
    assert outcome.reason == "missing_info" and outcome.asked == inferred


def test_the_same_question_is_counted_once_and_one_decided_on_is_not_asked_again(tmp_path: Path) -> None:
    store = Store(tmp_path / "jobs.db")
    first, second = (Job(f"https://u/{n}", f"Co{n}", "Engineer", "Rome", f"https://u/{n}", "a", "") for n in (1, 2))
    store.note_asked(first, [{"question": "Do you need a visa?", "answer": "Yes", "options": ["Yes", "No"]}])
    store.note_asked(second, [{"question": "do you need a VISA", "answer": "No", "options": []}, {"question": "Years of Python?", "answer": "5", "options": []}])
    asked = store.asked()
    assert [(item["question"], item["count"], item["answer"], item["company"]) for item in asked] == [
        ("Do you need a visa?", 2, "No", "Co2"), ("Years of Python?", 1, "5", "Co2")]
    store.set_asked(question_key("Do you need a visa?"), "added")
    store.note_asked(first, [{"question": "Do you need a visa?", "answer": "Yes", "options": []}])
    assert [item["question"] for item in store.asked()] == ["Years of Python?"]
    assert store.asked("added")[0]["count"] == 3
    with pytest.raises(ValueError):
        store.set_asked("x", "maybe")
    store.close()


def test_an_application_passes_the_standing_answers_and_keeps_what_the_model_answered_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "cv.md").write_text("# Ada Rossi\n", encoding="utf-8")
    (tmp_path / "data" / "cvs").mkdir(parents=True)
    (tmp_path / "data" / "cvs" / "original-cv.txt").write_text("Ada Rossi", encoding="utf-8")
    write(tmp_path)
    session = Session(tmp_path)
    session._client = SimpleNamespace()  # type: ignore[assignment]
    spend = Spend()
    spend.loaded = True
    monkeypatch.setattr(session_module, "SPEND", spend)
    given: list[dict[str, Any]] = []

    async def apply_to_job(*_: Any, **options: Any) -> Outcome:
        given.append(options)
        return Outcome("submitted", "sent", asked=[{"question": "Years of Python?", "answer": "5", "options": []}])

    monkeypatch.setattr(session_module, "apply_to_job", apply_to_job)
    job = Job("https://jobs.lever.co/acme/1", "Acme", "Engineer", "Rome", "https://jobs.lever.co/acme/1", "a", "")
    session.store.add_all([job])
    asyncio.run(session.apply(session.store.get(job.id), lambda line: None))  # type: ignore[arg-type]
    assert "Preferred First Name | answer: (leave empty)" in given[0]["standing"]
    assert [(item["question"], item["company"]) for item in session.store.asked()] == [("Years of Python?", "Acme")]
    # Without the file the call is the one it always was.
    (tmp_path / standing.FILE).unlink()
    session.store.set_status(job.id, "scored")
    asyncio.run(session.apply(session.store.get(job.id), lambda line: None))  # type: ignore[arg-type]
    assert "standing" not in given[1]
    session.close()
