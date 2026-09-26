"""Copied sessions: after ``/clear`` in a web or mobile session, Claude
Code writes the new session's lines both to its own file and into the
earlier session's file, keeping the new session's ``sessionId`` (seen in
Claude Code 2.1.283). Those lines must be priced once, from their own
file -- see ``parse.py``'s module docstring.
"""

from __future__ import annotations

from pathlib import Path

from claudeglass.corpus import load_corpus
from claudeglass.model import Diagnostics, TranscriptMeta
from claudeglass.parse import parse_transcript
from claudeglass.report import _merge_diagnostics

from helpers import turn_line, user_str_line, write_jsonl

EARLIER = "7e5fd47d-1816-569f-9241-e02958cf9196"
CLEARED = "2936e571-ffaa-4e4c-bfdd-cbb1c84d40d4"


def _cleared_lines() -> list[dict]:
    """The session started by /clear: a prompt and two replies."""
    return [
        user_str_line("/clear", sessionId=CLEARED, uuid="c-user-1"),
        turn_line(message_id="msg_c1", output_tokens=700, sessionId=CLEARED, uuid="c-asst-1"),
        user_str_line("next", sessionId=CLEARED, uuid="c-user-2"),
        turn_line(message_id="msg_c2", output_tokens=300, sessionId=CLEARED, uuid="c-asst-2"),
    ]


def _earlier_lines() -> list[dict]:
    """The earlier session: its own replies, then a verbatim copy of the
    cleared session's lines (same uuids, message ids and sessionId), then
    more of its own."""
    return [
        user_str_line("start", sessionId=EARLIER, uuid="e-user-1"),
        turn_line(message_id="msg_e1", output_tokens=100, sessionId=EARLIER, uuid="e-asst-1"),
        *[dict(line) for line in _cleared_lines()],
        user_str_line("carry on", sessionId=EARLIER, uuid="e-user-2"),
        turn_line(message_id="msg_e2", output_tokens=200, sessionId=EARLIER, uuid="e-asst-2"),
    ]


def _write_project(folder: Path, *, with_cleared_file: bool = True) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    write_jsonl(folder / f"{EARLIER}.jsonl", _earlier_lines())
    if with_cleared_file:
        write_jsonl(folder / f"{CLEARED}.jsonl", _cleared_lines())
    return folder


def _top_meta(path: Path, session_id: str) -> TranscriptMeta:
    return TranscriptMeta(path=str(path), kind="top-level", session_id=session_id)


def test_copy_is_skipped_when_its_own_file_is_beside_it(tmp_path: Path):
    folder = _write_project(tmp_path / "-home-user-shop")
    path = folder / f"{EARLIER}.jsonl"

    result = parse_transcript(path, _top_meta(path, EARLIER))

    assert [turn.message_id for turn in result.turns] == ["msg_e1", "msg_e2"]
    assert sum(turn.output_tokens for turn in result.turns) == 300
    assert result.diagnostics.copied_lines == 4
    assert result.diagnostics.replayed_lines == 0


def test_copy_is_kept_when_there_is_no_file_of_its_own(tmp_path: Path):
    folder = _write_project(tmp_path / "-home-user-shop", with_cleared_file=False)
    path = folder / f"{EARLIER}.jsonl"

    result = parse_transcript(path, _top_meta(path, EARLIER))

    assert [turn.message_id for turn in result.turns] == ["msg_e1", "msg_c1", "msg_c2", "msg_e2"]
    assert result.diagnostics.copied_lines == 0


def test_a_log_whose_session_id_is_not_its_file_name_keeps_every_line(tmp_path: Path):
    # Many logs (and most fixtures) carry a sessionId that isn't their
    # file name; with no file of that name beside them, nothing is skipped.
    path = tmp_path / "session-renamed.jsonl"
    write_jsonl(path, _cleared_lines())

    result = parse_transcript(path, _top_meta(path, "session-renamed"))

    assert len(result.turns) == 2
    assert result.diagnostics.copied_lines == 0


def test_only_a_top_level_transcript_skips_copies(tmp_path: Path):
    folder = _write_project(tmp_path / "-home-user-shop")
    path = folder / f"{EARLIER}.jsonl"

    result = parse_transcript(path, TranscriptMeta(path=str(path), kind="subagent", session_id=EARLIER))

    assert len(result.turns) == 4
    assert result.diagnostics.copied_lines == 0


def test_a_session_id_that_is_not_a_file_name_is_never_looked_up(tmp_path: Path):
    folder = tmp_path / "-home-user-shop"
    folder.mkdir()
    # A file the traversal would find if the id were used as a path.
    write_jsonl(tmp_path / "outside.jsonl", [])
    lines = [
        turn_line(message_id="msg_1", sessionId=EARLIER),
        turn_line(message_id="msg_2", sessionId="../outside"),
        turn_line(message_id="msg_3", sessionId="a.b"),
    ]
    path = folder / f"{EARLIER}.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, _top_meta(path, EARLIER))

    assert len(result.turns) == 3
    assert result.diagnostics.copied_lines == 0


def test_corpus_prices_each_reply_once(tmp_path: Path):
    folder = _write_project(tmp_path / "-home-user-shop")

    corpus = load_corpus([folder])

    by_session = {bundle.session_id: bundle for bundle in corpus.sessions}
    assert set(by_session) == {EARLIER, CLEARED}
    assert [t.message_id for t in by_session[EARLIER].top.turns] == ["msg_e1", "msg_e2"]
    assert [t.message_id for t in by_session[CLEARED].top.turns] == ["msg_c1", "msg_c2"]
    message_ids = [t.message_id for bundle in corpus.sessions for t in bundle.top.turns]
    assert len(message_ids) == len(set(message_ids))
    assert sum(t.output_tokens for bundle in corpus.sessions for t in bundle.top.turns) == 1300


def test_copied_lines_add_up_across_transcripts():
    total = Diagnostics()
    _merge_diagnostics(total, Diagnostics(copied_lines=4))
    _merge_diagnostics(total, Diagnostics(copied_lines=3))
    assert total.copied_lines == 7
