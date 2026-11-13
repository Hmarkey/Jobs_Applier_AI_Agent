"""Simplify Copilot fills a form first, when the person turned it on: its panel found, its button clicked as a person would,
the filling waited for, and the model told what it did. And the AI service is not waited on for minutes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aihawk import apply as apply_module
from aihawk.apply import FILLED, SIMPLIFY_BUTTON, SIMPLIFY_ID, SIMPLIFY_LABEL, run_on_page, simplify_autofill, simplify_installed
from aihawk.config import ConfigError, load_config
from aihawk.llm import RETRIES, TIMEOUT, make_client
from aihawk.profile import save_setting
from fakes import ScriptedBrowser, ScriptedModel
from test_apply import JOB, SUBMIT, VERIFIED, ready


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """The add-on's waits, in milliseconds: the tests say what the panel says, poll by poll."""
    monkeypatch.setattr(apply_module, "SIMPLIFY_POLL_S", 0.01)
    monkeypatch.setattr(apply_module, "SIMPLIFY_PANEL_S", 0.05)
    monkeypatch.setattr(apply_module, "SIMPLIFY_FILL_S", 0.2)
    # And a page after the send, read at once, as the other tests of a form read it.
    monkeypatch.setattr(apply_module, "SEND_POLL_S", 0)


class Panel(ScriptedBrowser):
    """A form with Simplify's panel: its button's words poll by poll (the last one stays), and how many fields hold something."""

    def __init__(self, labels: list[Any], filled: list[int], click_fails: bool = False) -> None:
        super().__init__({"#send": SUBMIT})
        self.labels, self.filled, self.click_fails = labels, filled, click_fails

    @staticmethod
    def _next(values: list[Any]) -> Any:
        return values.pop(0) if len(values) > 1 else values[0]

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> SimpleNamespace:
        arguments = arguments or {}
        if name == "browser_evaluate" and arguments["expression"] in (SIMPLIFY_LABEL, FILLED):
            self.calls.append((name, arguments))
            value = self._next(self.labels if arguments["expression"] == SIMPLIFY_LABEL else self.filled)
            return SimpleNamespace(isError=False, content=[SimpleNamespace(text=json.dumps(value))])
        if name == "browser_click" and arguments.get("selector") == SIMPLIFY_BUTTON and self.click_fails:
            self.calls.append((name, arguments))
            return SimpleNamespace(isError=True, content=[SimpleNamespace(text="not actionable in 5000ms")])
        return await super().call_tool(name, arguments)


def test_the_panel_s_button_is_clicked_and_its_filling_waited_for() -> None:
    browser = Panel([None, "Autofill This Page", "Autofilling", "Autofilling", "Run Autofill Again"], [0, 8])
    told = asyncio.run(simplify_autofill(browser))  # type: ignore[arg-type]
    assert browser.clicks() == [SIMPLIFY_BUTTON]
    assert "has just filled 8 fields" in told and "never use it" in told


def test_a_page_with_no_panel_is_left_to_the_model() -> None:
    browser = Panel([None], [0])
    assert asyncio.run(simplify_autofill(browser)) == ""  # type: ignore[arg-type]
    assert browser.clicks() == []


def test_a_click_that_does_not_happen_is_left_to_the_model() -> None:
    browser = Panel(["Autofill This Page"], [0], click_fails=True)
    assert asyncio.run(simplify_autofill(browser)) == ""  # type: ignore[arg-type]


def test_a_filling_that_never_says_it_ended_is_waited_for_no_longer_than_its_time() -> None:
    browser = Panel(["Autofill This Page", "Autofilling"], [2, 5])
    assert "filled 3 fields" in asyncio.run(simplify_autofill(browser))  # type: ignore[arg-type]


def test_with_simplify_on_the_model_is_told_what_it_filled_and_the_form_is_sent_as_before(tmp_path: Path) -> None:
    browser = Panel(["Autofill This Page", "Autofilling", "Run Autofill Again"], [1, 9])
    model = ScriptedModel(ready(), VERIFIED)
    outcome = asyncio.run(run_on_page(browser, [], model, "m", JOB, "cv", "answers", Path("cv.pdf"), tmp_path / "proof.png", simplify=True))  # type: ignore[arg-type]
    assert outcome.status == "submitted"
    assert browser.clicks() == [SIMPLIFY_BUTTON, "#send"]
    brief = model.requests[0]["messages"][1]["content"]
    assert "Simplify Copilot, a browser add-on, has just filled 8 fields" in brief


def test_with_simplify_off_the_panel_is_never_looked_for(tmp_path: Path) -> None:
    browser = Panel(["Autofill This Page"], [0])
    model = ScriptedModel(ready(), VERIFIED)
    asyncio.run(run_on_page(browser, [], model, "m", JOB, "cv", "answers", Path("cv.pdf"), tmp_path / "proof.png"))  # type: ignore[arg-type]
    assert SIMPLIFY_BUTTON not in browser.clicks()
    assert not any(arguments.get("expression") == SIMPLIFY_LABEL for _, arguments in browser.calls)
    assert "Simplify" not in model.requests[0]["messages"][1]["content"]


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
