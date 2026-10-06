"""The speech gate's classifier against fixed windows, for real (`live`, #656).

`tests/test_speech_gate_disposition.py` asserts what the prompt *states*; this
asks what the model *decides*. Each window is a shape seen in a real room,
rewritten to placeholder text (the repository is public, so no real message is
committed), and runs through the shipped classifier path: `build_window`, then
the tool-less `claude` call `executor.build_speech_gate_completer` uses on a
Claude Code deployment, then `parse_decision`.

Only the verdicts that matter are asserted, per disposition:

- **reserved** is the strict prompt and every yes is a message in the room, so
  a reaction, people talking past the bot and a passing mention all stay
  silent.
- **friendly** is a lenient filter in front of an agent that can decline
  (#675), so the costly error is a wrong no: reactions, a trailing name and a
  file the bot asked for speak. People talking past the bot stays silent, or
  the filter is a pass-through. A passing mention and an unprompted file are
  not asserted either way, since a yes there reaches an agent that may decline.

The model is not deterministic. A failure here is a reason to read the window
and the prompt, not proof of a regression; adjust the prompt against these same
fixtures. Running it costs money:

    uv run pytest -m live -n0 tests/live/test_speech_gate_windows.py

With no Claude Code credential it skips; `ISTOTA_LIVE_TIER=1` turns the skip
into a failure.
"""

import os
import shutil

import pytest

from istota.config import Config, SpeechGateConfig
from istota.rooms.speech_gate import (
    FRIENDLY,
    RESERVED,
    WindowTurn,
    build_window,
    parse_decision,
    unopened_file_line,
)
from istota.usage.subscription import resolve_token

pytestmark = pytest.mark.live

# Captured at import, before conftest's autouse credential scrub runs.
_AMBIENT_ENV = dict(os.environ)
_CREDENTIAL_NAMES = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

BOT = "Zorg"


def _unavailable(reason: str) -> None:
    if _AMBIENT_ENV.get("ISTOTA_LIVE_TIER") == "1":
        pytest.fail(f"ISTOTA_LIVE_TIER=1 says this host can run the live tier: {reason}")
    pytest.skip(reason)


@pytest.fixture(autouse=True)
def _requires_claude_code(monkeypatch) -> None:
    if shutil.which("claude") is None:
        _unavailable("no `claude` on PATH")
    if not (
        _AMBIENT_ENV.get("ISTOTA_LIVE_TIER") == "1"
        or _AMBIENT_ENV.get("ANTHROPIC_API_KEY")
        or resolve_token(_AMBIENT_ENV) is not None
    ):
        _unavailable("no Claude Code credential in reach")
    # The classifier's CLI env is built from the process env, which the
    # autouse scrub emptied of credentials.
    for name in _CREDENTIAL_NAMES:
        if _AMBIENT_ENV.get(name):
            monkeypatch.setenv(name, _AMBIENT_ENV[name])


def _person(name, text):
    return WindowTurn(author=name, text=text, is_bot=False)


def _bot(text):
    return WindowTurn(
        author=f"{BOT} (assistant)", text=text, is_bot=True,
        asks=text.rstrip().endswith("?"),
    )


ANSWER = [
    _person("Ann", "Zorg, when does the hardware shop close today?"),
    _bot("It closes at 6pm today, and it is closed on Sunday."),
]

WINDOWS = {
    "thanks_after_answer": ANSWER + [_person("Ann", "great, thanks!")],
    "someone_else_reacts": ANSWER + [_person("Ben", "ha, good to know, cheers Zorg")],
    "talking_past": [
        _person("Ann", "are we still doing the walk on Saturday?"),
        _person("Ben", "yes, meet at the bridge at ten"),
        _person("Cal", "I'll bring the flask"),
        _person("Ann", "perfect, see you both there"),
    ],
    "passing_mention": [
        _person("Ann", "I asked Zorg about the shop yesterday"),
        _person("Ben", "and what did you end up buying?"),
        _person("Ann", "just the paint in the end"),
    ],
    "correction_about_answer": ANSWER + [
        _person("Ann", "no, the sign on the door says 5pm on Saturdays"),
    ],
    "trailing_name": [
        _person("Ann", "the gutter on the north side is leaking again"),
        _person("Ann", unopened_file_line("an image")),
        _person("Ann", "seems relevant Zorg"),
    ],
    "file_after_the_bot_asked": [
        _person("Ann", "Zorg, which plant is this?"),
        _bot("Could you send me a photo of the leaves?"),
        _person("Ann", unopened_file_line("an image")),
    ],
    "file_unprompted": [
        _person("Ann", "look at this sunset"),
        _person("Ann", unopened_file_line("an image")),
    ],
}

#: The verdict each disposition must reach; a window absent from a map is
#: deliberately not asserted.
EXPECTED = {
    RESERVED: {
        "thanks_after_answer": False,
        "someone_else_reacts": False,
        "talking_past": False,
        "passing_mention": False,
    },
    FRIENDLY: {
        "thanks_after_answer": True,
        "someone_else_reacts": True,
        "talking_past": False,
        "correction_about_answer": True,
        "trailing_name": True,
        "file_after_the_bot_asked": True,
    },
}


def _ask(window: str) -> str | None:
    from istota.brain import ClaudeCodeBrain
    from istota.context import _claude_cli_triage

    config = Config()
    gate = SpeechGateConfig()
    model = ClaudeCodeBrain().resolve_model_name(gate.model)
    return _claude_cli_triage(
        window, model, gate.timeout_seconds, config, label="Speech gate live test",
    )


@pytest.mark.parametrize(
    "disposition,window,expected",
    [(d, w, v) for d, cases in EXPECTED.items() for w, v in cases.items()],
)
def test_the_classifier_reaches_the_verdict_that_matters(disposition, window, expected):
    prompt = build_window(WINDOWS[window], bot_name=BOT, disposition=disposition)
    raw = _ask(prompt)
    verdict = parse_decision(raw)
    assert verdict is not None, f"no parseable verdict for {window}: {raw!r}"
    assert verdict.speak is expected, (
        f"{disposition}/{window}: speak={verdict.speak}, reason={verdict.reason!r}"
    )


def test_every_fixture_is_asserted_or_named_as_open():
    """A window in no map is an open case by decision, and is listed here."""
    open_cases = {"passing_mention", "file_unprompted"}
    asserted = set().union(*(cases.keys() for cases in EXPECTED.values()))
    assert set(WINDOWS) == asserted | open_cases
