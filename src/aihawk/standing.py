"""The person's standing answers: the questions that come back on form after form with the same answer (the right to work,
sponsorship, the degree, the demographic questions), each answered once by the person, in standing-answers.yaml beside
their aihawk.yaml. A form uses them for what Simplify Copilot did not fill, before the model answers anything from the CV.

The file is the person's to write: sections of their choosing, each a list of entries with `question` and `answer`, and
optionally `topic`, `matches` (the question's other wordings), `choose` (the options to pick in a list, in order) and
`note`. An entry the dashboard adds goes to an `added` section at the end, written as text so that their comments stay.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from aihawk.config import ConfigError

FILE = "standing-answers.yaml"
ADDED = "added"


def path_of(workspace: Path) -> Path:
    return workspace / FILE


def _parse(text: str, where: Path) -> list[dict[str, Any]]:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"{where.name} is not valid YAML: {error}") from error
    if not isinstance(data, dict):
        raise ConfigError(f"{where.name} must be a mapping of sections, each a list of questions")
    entries = []
    for section in data.values():
        if not isinstance(section, list):
            continue
        for entry in section:
            # A list of plain lines (notes to self, questions still open) is not an answer.
            if isinstance(entry, dict) and isinstance(entry.get("question"), str) and entry["question"].strip() and "answer" in entry:
                entries.append({
                    "topic": str(entry.get("topic") or ""),
                    "question": entry["question"].strip(),
                    "matches": [str(item) for item in entry.get("matches") or [] if str(item).strip()],
                    "answer": "" if entry["answer"] is None else str(entry["answer"]),
                    "choose": [str(item) for item in entry.get("choose") or [] if str(item).strip()],
                    "note": str(entry.get("note") or ""),
                })
    return entries


def load(workspace: Path) -> list[dict[str, Any]]:
    """The standing answers in the person's file, in its order; none when there is no file."""
    path = path_of(workspace)
    if not path.is_file():
        return []
    return _parse(path.read_text(encoding="utf-8-sig"), path)


def brief(entries: list[dict[str, Any]]) -> str:
    """What the model is told about them: the rule, then one line each. Empty with none."""
    if not entries:
        return ""
    lines = [(
        "The candidate's standing answers. These are the candidate's own answers to questions that come back from form to form, "
        "and they are right as written. A field Simplify Copilot already filled stays as it is. A field still empty that asks one "
        "of these questions, in these words or in others that mean the same (the 'also asked as' wordings), gets exactly this "
        "answer: in a list, the first option of 'choose' that the list offers, else the option that means the answer. They come "
        "before every default in your rules: a demographic question is answered as here, not declined; a written field gets this "
        "answer, not 'Please see my attached CV'. An empty answer means: leave the field empty. A question that is not among them "
        "is answered from the CV and the answers as before, and goes in finish_form's `inferred`."
    )]
    for entry in entries:
        parts = [f"Q: {entry['question']}"]
        if entry["matches"]:
            parts.append("also asked as: " + "; ".join(entry["matches"]))
        parts.append(f"answer: {entry['answer']!r}" if entry["answer"] else "answer: (leave empty)")
        if entry["choose"]:
            parts.append("choose: " + "; ".join(entry["choose"]))
        if entry["note"]:
            parts.append(f"note: {entry['note']}")
        lines.append("- " + " | ".join(parts))
    return "\n".join(lines)


def _item(question: str, answer: str, options: list[str]) -> str:
    """One entry as YAML text, each value a JSON string, which YAML reads as itself."""
    chosen = [option for option in options if option.strip().lower() == answer.strip().lower()]
    lines = [
        f"  - topic: {json.dumps(question[:60], ensure_ascii=False)}",
        f"    question: {json.dumps(question, ensure_ascii=False)}",
        f"    answer: {json.dumps(answer, ensure_ascii=False)}",
    ]
    if chosen:
        lines.append(f"    choose: [{json.dumps(chosen[0], ensure_ascii=False)}]")
    lines.append(f"    note: {json.dumps('从看板加入，' + datetime.now(UTC).strftime('%Y-%m-%d'), ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def append(workspace: Path, question: str, answer: str, options: list[str] | None = None) -> None:
    """One standing answer added at the end of the person's file, in its `added` section, the rest of the file as they wrote
    it. A file that would not read after it is put back as it was, and the error says why."""
    question, answer = " ".join(question.split()), answer.strip()
    if not question:
        raise ValueError("问题不能为空")
    path = path_of(workspace)
    before = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    if before:
        # A file that does not read now is the person's to mend: nothing is added to it.
        _parse(before, path)
    keys = re.findall(r"^([A-Za-z_][\w-]*):", before, flags=re.MULTILINE)
    text = before if before.endswith("\n") or not before else before + "\n"
    if keys and keys[-1] == ADDED:
        text += _item(question, answer, options or [])
    elif ADDED not in keys:
        text += f"\n{ADDED}:   # 从看板加入的固定答案\n" + _item(question, answer, options or [])
    else:
        # `added` is there but not last: the entry goes under a section of its own, at the end, so the others stay untouched.
        text += f"\n{ADDED}_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}:\n" + _item(question, answer, options or [])
    _parse(text, path)
    path.write_text(text, encoding="utf-8")
