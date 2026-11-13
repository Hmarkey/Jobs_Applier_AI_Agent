"""Simplify Copilot does what its panel offers, when the person turned it on: on a form page it fills the page, on an account
page it makes the account or signs in and opens the application, each pressed as a person would and waited for; the model is
told what it did and may press it again on every later page. And the AI service is not waited on for minutes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aihawk import apply as apply_module
from aihawk.apply import (
    FILLED,
    NO_PANEL,
    PAGE_MARK,
    SIMPLIFY_ACCOUNT,
    SIMPLIFY_ACCOUNT_SEND,
    SIMPLIFY_BUTTON,
    SIMPLIFY_BUTTONS,
    SIMPLIFY_ID,
    SIMPLIFY_RULES,
    SYSTEM,
    _account_line,
    run_on_page,
    simplify_act,
    simplify_autofill,
    simplify_installed,
)
from aihawk.config import ConfigError, load_config
from aihawk.llm import RETRIES, TIMEOUT, make_client
from aihawk.plugins import parse_plugin
from aihawk.profile import save_setting
from fakes import ScriptedBrowser, ScriptedModel, tool_call
from test_apply import JOB, SUBMIT, VERIFIED, ready

ROOT = Path(__file__).resolve().parents[1]
FILL = {"fill-button": "Autofill This Page"}
FILLING = {"fill-button": "Autofilling"}
FILLED_ONCE = {"fill-button": "Run Autofill Again"}
ACCOUNT = {"account-fill-button": "Create Account & Autofill"}
ACCOUNT_READY = {"account-fill-button": "Create Account & Autofill", "account-submit-button": "Create Account"}


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """The add-on's waits, in milliseconds: the tests say what the panel says, poll by poll."""
    monkeypatch.setattr(apply_module, "SIMPLIFY_POLL_S", 0.01)
    monkeypatch.setattr(apply_module, "SIMPLIFY_PANEL_S", 0.05)
    monkeypatch.setattr(apply_module, "SIMPLIFY_FILL_S", 0.2)
    monkeypatch.setattr(apply_module, "SIMPLIFY_ACCOUNT_S", 0.1)
    monkeypatch.setattr(apply_module, "SIMPLIFY_PAGE_S", 0.1)
    monkeypatch.setattr(apply_module, "SIMPLIFY_STARTS_S", 0.03)
    # And a page after the send, read at once, as the other tests of a form read it.
    monkeypatch.setattr(apply_module, "SEND_POLL_S", 0)


class Panel(ScriptedBrowser):
    """A page with Simplify's panel. `stages` says what its buttons are, poll by poll (the last one stays), after each press
    of one of its buttons: stage 0 before any, stage 1 after the first, and so on. A press of the account's "Create Account"
    moves the page on, unless `moves` is false. How many fields hold something is read from `filled`, poll by poll."""

    def __init__(self, stages: list[list[Any]], filled: list[int] | None = None, click_fails: bool = False, moves: bool = True) -> None:
        super().__init__({"#send": SUBMIT})
        self.stages = [list(stage) for stage in stages]
        self.filled, self.click_fails, self.moves = list(filled or [0]), click_fails, moves
        self.pressed = self.sent = 0

    @staticmethod
    def _next(values: list[Any]) -> Any:
        return values.pop(0) if len(values) > 1 else values[0]

    def _answer(self, value: Any) -> SimpleNamespace:
        return SimpleNamespace(isError=False, content=[SimpleNamespace(text=json.dumps(value))])

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> SimpleNamespace:
        arguments = arguments or {}
        expression = arguments.get("expression")
        if name == "browser_evaluate" and expression in (SIMPLIFY_BUTTONS, FILLED, PAGE_MARK):
            self.calls.append((name, arguments))
            if expression == SIMPLIFY_BUTTONS:
                return self._answer(self._next(self.stages[min(self.pressed, len(self.stages) - 1)]))
            if expression == FILLED:
                return self._answer(self._next(self.filled))
            return self._answer(f"page {self.sent if self.moves else 0}")
        if name == "browser_click" and arguments.get("selector") in (SIMPLIFY_BUTTON, SIMPLIFY_ACCOUNT, SIMPLIFY_ACCOUNT_SEND):
            self.calls.append((name, arguments))
            if self.click_fails:
                return SimpleNamespace(isError=True, content=[SimpleNamespace(text="not actionable in 5000ms")])
            self.pressed += 1
            self.sent += arguments["selector"] == SIMPLIFY_ACCOUNT_SEND
            return self._answer("clicked")
        return await super().call_tool(name, arguments)


def act(browser: Panel) -> tuple[str, bool]:
    return asyncio.run(simplify_act(browser))  # type: ignore[arg-type]


def test_on_a_form_the_page_is_filled_and_waited_for() -> None:
    browser = Panel([[None, FILL], [FILLING, FILLING, FILLED_ONCE]], [0, 8])
    words, made = act(browser)
    assert browser.clicks() == [SIMPLIFY_BUTTON] and not made
    assert words.startswith("Simplify filled 8 fields of this page")


def test_a_page_with_no_panel_is_said_and_left_to_the_model() -> None:
    browser = Panel([[None]])
    assert act(browser) == (NO_PANEL, False)
    assert asyncio.run(simplify_autofill(browser)) == ""  # type: ignore[arg-type]
    assert browser.clicks() == []


def test_a_click_that_does_not_happen_is_said() -> None:
    words, _ = act(Panel([[FILL]], click_fails=True))
    assert "did not take the click" in words


def test_a_filling_that_never_says_it_ended_is_waited_for_no_longer_than_its_time() -> None:
    assert "filled 3 fields" in act(Panel([[FILL], [FILLING]], [2, 5]))[0]


def test_a_page_already_filled_is_filled_again_and_not_read_as_done_before_it_starts() -> None:
    browser = Panel([[FILLED_ONCE], [FILLED_ONCE, FILLING, FILLED_ONCE]], [3, 6])
    assert "filled 3 fields" in act(browser)[0]
    assert browser.clicks() == [SIMPLIFY_BUTTON]


def test_on_an_account_page_the_account_is_made_the_application_opened_and_its_page_filled() -> None:
    browser = Panel([[ACCOUNT], [ACCOUNT, ACCOUNT_READY], [FILLING, FILLED_ONCE]])
    words, made = act(browser)
    assert browser.clicks() == [SIMPLIFY_ACCOUNT, SIMPLIFY_ACCOUNT_SEND]
    assert made and "created the candidate's account on this site" in words and "password it keeps" in words


def test_a_sign_in_through_the_add_on_is_no_account_made() -> None:
    sign_in = {"account-fill-button": "Sign In & Autofill"}
    browser = Panel([[sign_in], [{**sign_in, "account-submit-button": "Sign In"}], [FILLED_ONCE]])
    words, made = act(browser)
    assert not made and "signed in to the candidate's account" in words


def test_an_account_page_that_does_not_move_on_is_said_and_no_account_counted() -> None:
    words, made = act(Panel([[ACCOUNT], [ACCOUNT_READY]], moves=False))
    assert not made and "did not move on" in words


def test_an_account_form_the_add_on_offers_no_send_for_is_left_to_the_page_s_button() -> None:
    words, made = act(Panel([[ACCOUNT], [ACCOUNT]]))
    assert not made and "press the page's own Create Account" in words


def test_with_simplify_on_the_model_has_the_rules_and_the_tool_and_an_account_it_makes_is_kept(tmp_path: Path) -> None:
    # The first page: a panel with none of the buttons this needs (Workday's start page). Then the model presses simplify_fill
    # on the account page, where the add-on's "Create Account" is already offered.
    browser = Panel([[{}, ACCOUNT_READY], [FILLING, FILLED_ONCE]])
    model = ScriptedModel([tool_call("simplify_fill")], ready(), VERIFIED)
    outcome = asyncio.run(run_on_page(browser, [], model, "m", JOB, "cv", "answers", Path("cv.pdf"), tmp_path / "proof.png", simplify=True))  # type: ignore[arg-type]
    assert outcome.status == "submitted" and outcome.made_account
    assert browser.clicks() == [SIMPLIFY_ACCOUNT_SEND, "#send"]
    request = model.requests[0]
    assert SIMPLIFY_RULES in request["messages"][1]["content"] and NO_PANEL in request["messages"][1]["content"]
    assert "simplify_fill" in [tool["function"]["name"] for tool in request["tools"]]
    assert "let simplify_fill make it" in request["messages"][1]["content"]


def test_with_simplify_off_the_panel_is_never_looked_for_and_there_is_no_tool(tmp_path: Path) -> None:
    browser = Panel([[FILL]])
    model = ScriptedModel(ready(), VERIFIED)
    asyncio.run(run_on_page(browser, [], model, "m", JOB, "cv", "answers", Path("cv.pdf"), tmp_path / "proof.png"))  # type: ignore[arg-type]
    assert SIMPLIFY_BUTTON not in browser.clicks()
    assert not any(arguments.get("expression") == SIMPLIFY_BUTTONS for _, arguments in browser.calls)
    assert "Simplify" not in model.requests[0]["messages"][1]["content"]
    assert "simplify_fill" not in [tool["function"]["name"] for tool in model.requests[0]["tools"]]


def test_an_account_known_on_the_site_is_signed_in_to_through_simplify_first() -> None:
    line = _account_line(JOB, ("a@b.c", "2026-11-13T00:00:00"), simplify=True)
    assert "call simplify_fill first" in line and "account_password" in line
    assert "account_password from the answers" in _account_line(JOB, ("a@b.c", "2026-11-13T00:00:00"))


def test_a_review_page_is_the_end_and_a_second_resume_is_never_uploaded() -> None:
    assert "never go back from it to earlier steps" in SYSTEM
    assert "never upload a second" in SYSTEM
    workday = parse_plugin("workday", (ROOT / "src" / "aihawk" / "plugins" / "workday.md").read_text(encoding="utf-8"))
    assert "Review is the end" in workday.instructions and "Create Account & Autofill" in workday.instructions


def _profile(tmp_path: Path, addons: Any) -> Path:
    folder = tmp_path / "browser-profile"
    folder.mkdir()
    (folder / "extensions.json").write_text(json.dumps({"addons": addons}), encoding="utf-8")
    return folder


@pytest.mark.parametrize(("addons", "installed"), [
    ([{"id": SIMPLIFY_ID, "active": True, "userDisabled": False}], True),
    ([{"id": SIMPLIFY_ID, "active": False, "userDisabled": False}], False),
    ([{"id": SIMPLIFY_ID, "active": True, "userDisabled": True}], False),
    ([{"id": "newtab@mozilla.org", "active": True}], False),
    ("not a list", False),
])
def test_the_add_on_is_known_installed_from_the_profile(tmp_path: Path, addons: Any, installed: bool) -> None:
    assert simplify_installed(_profile(tmp_path, addons)) is installed


def test_a_profile_with_no_add_ons_listed_has_none(tmp_path: Path) -> None:
    assert simplify_installed(tmp_path) is False


def test_the_switch_is_kept_in_the_settings(tmp_path: Path) -> None:
    assert load_config(tmp_path).simplify is False
    save_setting(load_config(tmp_path), "simplify", "true")
    assert load_config(tmp_path).simplify is True
    save_setting(load_config(tmp_path), "simplify", "false")
    assert load_config(tmp_path).simplify is False
    (tmp_path / "aihawk.yaml").write_text("simplify: maybe\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="true or false"):
        load_config(tmp_path)


def test_a_request_with_no_answer_is_given_up_in_a_minute_and_tried_once_more(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-test")
    client = make_client(load_config(tmp_path))
    assert (TIMEOUT, RETRIES) == (60.0, 1)
    assert client.timeout == TIMEOUT and client.max_retries == RETRIES
