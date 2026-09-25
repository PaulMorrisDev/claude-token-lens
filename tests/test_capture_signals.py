"""The free signals metrics capture logs: ``hooks/capture-hook.py`` on
SessionEnd, Notification, PermissionRequest, Stop and StopFailure, run
as Claude Code runs it, and ``signals.py`` reading them back. A line
holds the time, a salted hash of the session id and one word, tool name
or (SIG-4, written by ``statusline.py`` instead) a plain number; never a
message, a tool's input, a path, ``last_assistant_message``,
``error_details`` or a cron's ``prompt``. Nothing is logged while the
metric is off, without ClaudeGlass's salt, or for a session capture
skips.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claudeglass import signals
from claudeglass.parse import load_or_create_salt
from claudeglass.service.contracts import ServeOptions
from claudeglass.service.store import Store
from claudeglass.service.watcher import FileWatcher

from test_capture_hook import CATALOGUE, HOOK, _config, _run, _session_id

NOW = datetime(2026, 9, 24, 6, 0, tzinfo=timezone.utc)
SECRET = "C:/Users/someone/secret-project/.env"


def _setup(tmp_path: Path, capture: str = 'level = "free"') -> Path:
    config_dir = _config(tmp_path / "claudeglass", f"[capture]\n{capture}\n")
    load_or_create_salt(config_dir)
    return config_dir


def _lines(config_dir: Path) -> list[dict]:
    folder = config_dir / "signals"
    if not folder.exists():
        return []
    return [json.loads(line) for path in sorted(folder.iterdir()) for line in path.read_text(encoding="utf-8").splitlines()]


def _end(session_id="s1", **extra) -> dict:
    return {
        "session_id": session_id,
        "hook_event_name": "SessionEnd",
        "reason": "clear",
        "cwd": "/work/app",
        "transcript_path": SECRET,
        **extra,
    }


def _wait(session_id="s1", **extra) -> dict:
    return {
        "session_id": session_id,
        "hook_event_name": "Notification",
        "notification_type": "permission_prompt",
        "message": f"Claude needs your permission to use Read on {SECRET}",
        "cwd": "/work/app",
        **extra,
    }


def _perm(session_id="s1", **extra) -> dict:
    return {
        "session_id": session_id,
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": f"cat {SECRET}"},
        "cwd": "/work/app",
        **extra,
    }


def _turn(session_id="s1", **extra) -> dict:
    return {
        "session_id": session_id,
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "cwd": "/work/app",
        "transcript_path": SECRET,
        "last_assistant_message": "the secret final answer",
        "background_tasks": [{"id": "t1", "command": f"cat {SECRET}"}],
        "session_crons": [{"id": "c1", "prompt": "a private scheduled prompt"}],
        **extra,
    }


def _fail(session_id="s1", **extra) -> dict:
    return {
        "session_id": session_id,
        "hook_event_name": "StopFailure",
        "error": "rate_limit",
        "error_details": "retry after 30s, account acct_secret_123",
        "last_assistant_message": "API Error: Rate limit reached",
        "cwd": "/work/app",
        **extra,
    }


def _turn_id(pct: int, now: datetime, *, inside: bool, prefix: str = "turn") -> str:
    import hashlib

    for n in range(2000):
        sid = f"{prefix}-{n}"
        key = f"{sid}:{now.isoformat()}"
        bucket = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) % 100
        if (bucket < pct) == inside:
            return sid
    raise AssertionError("no session id found")


def _log(config_dir: Path, payload: dict) -> None:
    assert _run(config_dir, payload) == (0, "", "")  # never prints, so never steers Claude Code


# -- what the hook logs ----------------------------------------------------


def test_each_signal_is_one_line_of_closed_words(tmp_path):
    config_dir = _setup(tmp_path)
    for payload in (_end(), _wait(), _perm()):
        _log(config_dir, payload)
    salt = (config_dir / "salt").read_bytes()
    lines = _lines(config_dir)
    assert [{k: v for k, v in line.items() if k != "ts"} for line in lines] == [
        {"sid": signals.session_hash(salt, "s1"), "e": "end", "reason": "clear"},
        {"sid": signals.session_hash(salt, "s1"), "e": "wait", "kind": "permission"},
        {"sid": signals.session_hash(salt, "s1"), "e": "perm", "tool": "Bash"},
    ]
    text = "".join(p.read_text(encoding="utf-8") for p in (config_dir / "signals").iterdir())
    assert "secret" not in text and "s1" not in text and "Read" not in text
    assert [p.name for p in (config_dir / "signals").iterdir()] == [lines[0]["ts"][:7] + ".jsonl"]


@pytest.mark.parametrize("payload, field, value", [
    (_end(reason="prompt_input_exit"), "reason", "prompt_input_exit"),
    (_end(reason="resume"), "reason", "resume"),  # SIG-1: curl-verified against docs/en/hooks.md
    (_end(reason={"x": 1}), "reason", "other"),
    (_wait(notification_type="idle_prompt"), "kind", "idle"),
    (_wait(notification_type="elicitation_dialog"), "kind", "question"),
    (_wait(notification_type="elicitation_url_dialog"), "kind", "question"),
    (_wait(notification_type="agent_needs_input"), "kind", "agent"),
    (_wait(notification_type="quota_auto_resume_fired"), "kind", "quota"),
    (_wait(notification_type="quota_auto_resume_stale"), "kind", "quota"),
    (_wait(notification_type="quota_auto_resume_disabled"), "kind", "quota"),
    (_wait(notification_type="auth_success"), "kind", "other"),
    (_wait(notification_type=None, message="Claude is waiting for your input"), "kind", "idle"),
    (_wait(notification_type=None, message="Claude needs your permission to use Bash"), "kind", "permission"),
    (_perm(tool_name="mcp__github__create_issue"), "tool", "mcp__github__create_issue"),
    (_perm(tool_name="Bash; rm -rf /"), "tool", "other"),
    (_perm(tool_name=None), "tool", "other"),
])
def test_anything_outside_the_word_lists_is_logged_as_other(tmp_path, payload, field, value):
    config_dir = _setup(tmp_path)
    _log(config_dir, payload)
    assert _lines(config_dir)[0][field] == value


def test_stop_and_stop_failure_lines_are_closed_words(tmp_path):
    config_dir = _setup(tmp_path)
    config = {"capture": {"level": "free"}}
    salt = (config_dir / "salt").read_bytes()
    turn_id = _turn_id(HOOK._TURN_SAMPLE_PCT, NOW, inside=True)  # SIG-3: force the sample in
    turn_record = HOOK.signal_for(_turn(turn_id), config, CATALOGUE, salt, NOW)
    assert turn_record == {
        "ts": "2026-09-24T06:00:00Z", "sid": signals.session_hash(salt, turn_id), "e": "turn", "state": "normal",
    }
    fail_record = HOOK.signal_for(_fail(), config, CATALOGUE, salt, NOW)
    assert fail_record == {
        "ts": "2026-09-24T06:00:00Z", "sid": signals.session_hash(salt, "s1"), "e": "fail", "error": "rate_limit",
    }
    reentrant_record = HOOK.signal_for(_turn(turn_id, stop_hook_active=True), config, CATALOGUE, salt, NOW)
    assert reentrant_record["state"] == "reentrant"


def test_stop_never_leaks_the_final_reply_error_details_or_a_cron_prompt(tmp_path):
    config_dir = _setup(tmp_path)
    config = {"capture": {"level": "free"}}
    salt = (config_dir / "salt").read_bytes()
    turn_id = _turn_id(HOOK._TURN_SAMPLE_PCT, NOW, inside=True)
    for payload in (_turn(turn_id), _fail()):
        record = HOOK.signal_for(payload, config, CATALOGUE, salt, NOW)
        assert record is not None
        text = json.dumps(record)
        assert "secret" not in text and "private" not in text and "acct_secret" not in text
        assert "last_assistant_message" not in record and "error_details" not in record and "prompt" not in record


@pytest.mark.parametrize("payload, field, value", [
    (_fail(error="overloaded"), "error", "overloaded"),
    (_fail(error="cloud_credential_error"), "error", "cloud_credential_error"),
    (_fail(error="not_a_real_kind"), "error", "unknown"),
    (_fail(error=None), "error", "unknown"),
])
def test_stop_failure_error_kind_is_a_closed_word(tmp_path, payload, field, value):
    config_dir = _setup(tmp_path)
    _log(config_dir, payload)
    assert _lines(config_dir)[0][field] == value


def test_stop_is_sampled_independently_of_the_session(tmp_path):
    config_dir = _setup(tmp_path)
    config = {"capture": {"level": "free"}}
    salt = (config_dir / "salt").read_bytes()
    inside = _turn_id(10, NOW, inside=True)
    outside = _turn_id(10, NOW, inside=False)
    assert HOOK.signal_for(_turn(inside), config, CATALOGUE, salt, NOW) is not None
    assert HOOK.signal_for(_turn(outside), config, CATALOGUE, salt, NOW) is None
    # StopFailure is never sampled: every one of these logs regardless.
    assert HOOK.signal_for(_fail(outside), config, CATALOGUE, salt, NOW) is not None


def test_a_subagent_call_is_marked(tmp_path):
    config_dir = _setup(tmp_path)
    _log(config_dir, _perm(agent_id="a1", agent_type="Explore"))
    assert _lines(config_dir)[0]["sub"] == 1 and "Explore" not in json.dumps(_lines(config_dir))


def test_nothing_is_logged_when_capture_or_the_metric_is_off(tmp_path):
    off = _setup(tmp_path / "off", 'level = "off"')
    _log(off, _end())
    assert _lines(off) == []
    only_waits = _setup(tmp_path / "custom", 'level = "custom"\nmetrics = ["waits"]')
    for payload in (_end(), _wait(), _perm()):
        _log(only_waits, payload)
    assert [line["e"] for line in _lines(only_waits)] == ["wait"]


def test_nothing_is_logged_without_the_salt(tmp_path):
    config_dir = _config(tmp_path, '[capture]\nlevel = "free"\n')
    _log(config_dir, _end())
    assert _lines(config_dir) == [] and not (config_dir / "salt").exists()
    (config_dir / "salt").write_bytes(b"short")
    _log(config_dir, _end())
    assert _lines(config_dir) == []


def test_a_session_capture_skips_logs_nothing(tmp_path):
    sampled = _setup(tmp_path / "sampled", 'level = "free"\nsample = 10')
    _log(sampled, _end(_session_id(10, inside=False)))
    assert _lines(sampled) == []
    _log(sampled, _end(_session_id(10, inside=True)))
    assert len(_lines(sampled)) == 1
    skipped = _setup(tmp_path / "skipped", 'level = "free"\nprojects = ["!secret"]')
    _log(skipped, _end(cwd="/work/secret-thing"))
    assert _lines(skipped) == []
    ended = _setup(tmp_path / "ended", 'level = "free"\nuntil = "2020-01-01"')
    _log(ended, _end())
    assert _lines(ended) == []


def test_the_hook_and_the_reader_hash_session_ids_alike():
    salt = bytes(range(32))
    assert HOOK.session_hash(salt, "abc") == signals.session_hash(salt, "abc")


def test_signal_for_uses_the_given_time():
    config = {"capture": {"level": "free"}}
    catalogue = HOOK.load_catalogue()
    record = HOOK.signal_for(_end(), config, catalogue, bytes(32), NOW)
    assert record["ts"] == "2026-09-24T06:00:00Z"
    assert HOOK.signal_for(_end(), config, catalogue, None, NOW) is None
    assert HOOK.signal_for({**_end(), "session_id": ""}, config, catalogue, bytes(32), NOW) is None


# -- reading them back -----------------------------------------------------


def _write(config_dir: Path, name: str, lines: list) -> None:
    folder = config_dir / "signals"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(
        "".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines), encoding="utf-8"
    )


def test_load_keeps_only_well_formed_lines(tmp_path):
    good = {"ts": "2026-09-01T10:00:00Z", "sid": "0123456789abcdef", "e": "wait", "kind": "idle"}
    _write(tmp_path, "2026-09.jsonl", [
        good,
        "not json",
        {**good, "kind": "a message someone typed"},
        {**good, "sid": "not-a-hash"},
        {**good, "e": "prompt", "text": "hello"},
        {**good, "ts": "yesterday"},
        {**good, "e": "perm", "tool": "../../etc/passwd"},
        {**good, "e": "perm", "tool": "Read", "sub": 1},
    ])
    _write(tmp_path, "notes.txt", ["ignored"])
    loaded = signals.load(tmp_path)
    assert [(s.event, s.value, s.subagent) for s in loaded] == [("wait", "idle", False), ("perm", "Read", True)]
    assert signals.load(tmp_path / "nowhere") == []


def test_load_since_skips_older_months_and_lines(tmp_path):
    line = {"sid": "0123456789abcdef", "e": "end", "reason": "clear"}
    _write(tmp_path, "2026-08.jsonl", [{**line, "ts": "2026-08-31T23:00:00Z"}])
    _write(tmp_path, "2026-09.jsonl", [{**line, "ts": "2026-09-01T01:00:00Z"}, {**line, "ts": "2026-09-20T01:00:00Z"}])
    since = datetime(2026, 9, 10, tzinfo=timezone.utc)
    assert [s.at.day for s in signals.load(tmp_path, since=since)] == [20]
    assert len(signals.load(tmp_path)) == 3


def test_by_session_joins_on_the_salted_hash(tmp_path):
    salt = bytes(range(32))
    sid = signals.session_hash(salt, "sess-1")
    _write(tmp_path, "2026-09.jsonl", [
        {"ts": "2026-09-01T10:00:00Z", "sid": sid, "e": "wait", "kind": "permission"},
        {"ts": "2026-09-01T10:01:00Z", "sid": sid, "e": "wait", "kind": "permission"},
        {"ts": "2026-09-01T10:02:00Z", "sid": sid, "e": "perm", "tool": "Bash", "sub": 1},
        {"ts": "2026-09-01T10:03:00Z", "sid": sid, "e": "end", "reason": "prompt_input_exit"},
        {"ts": "2026-09-01T10:04:00Z", "sid": signals.session_hash(salt, "other"), "e": "end", "reason": "clear"},
    ])
    joined = signals.by_session(signals.load(tmp_path), ["sess-1", "sess-2"], salt)
    assert joined == {
        "sess-1": signals.SessionSignals(
            end_reason="prompt_input_exit", waits={"permission": 2}, permission_prompts={"Bash": 1}, subagent_events=1
        )
    }


def test_by_session_counts_turn_states_and_failures(tmp_path):
    salt = bytes(range(32))
    sid = signals.session_hash(salt, "sess-1")
    _write(tmp_path, "2026-09.jsonl", [
        {"ts": "2026-09-01T10:00:00Z", "sid": sid, "e": "turn", "state": "normal"},
        {"ts": "2026-09-01T10:01:00Z", "sid": sid, "e": "turn", "state": "reentrant"},
        {"ts": "2026-09-01T10:02:00Z", "sid": sid, "e": "turn", "state": "normal"},
        {"ts": "2026-09-01T10:03:00Z", "sid": sid, "e": "fail", "error": "rate_limit"},
    ])
    joined = signals.by_session(signals.load(tmp_path), ["sess-1"], salt)
    assert joined["sess-1"].turn_states == {"normal": 2, "reentrant": 1}
    assert joined["sess-1"].failures == {"rate_limit": 1}


def test_by_session_keeps_the_latest_cost_and_recache_sample(tmp_path):
    salt = bytes(range(32))
    sid = signals.session_hash(salt, "sess-1")
    _write(tmp_path, "2026-09.jsonl", [
        {"ts": "2026-09-01T10:00:00Z", "sid": sid, "e": "cost", "usd": 1.2},
        {"ts": "2026-09-01T10:01:00Z", "sid": sid, "e": "recache", "tokens": 500},
        {"ts": "2026-09-01T10:02:00Z", "sid": sid, "e": "cost", "usd": 1.5},
    ])
    joined = signals.by_session(signals.load(tmp_path), ["sess-1"], salt)
    assert joined["sess-1"].statusline_cost_usd == 1.5
    assert joined["sess-1"].recache_tokens == 500


def test_cost_and_recache_values_must_be_plain_non_negative_numbers(tmp_path):
    good = {"ts": "2026-09-01T10:00:00Z", "sid": "0123456789abcdef", "e": "cost", "usd": 2.5}
    _write(tmp_path, "2026-09.jsonl", [
        good,
        {**good, "usd": -1},
        {**good, "usd": "2.5"},
        {**good, "usd": True},
        {**good, "usd": 10_000_000},
        {**good, "e": "recache", "tokens": 12345},
    ])
    loaded = signals.load(tmp_path)
    assert [(s.event, s.value) for s in loaded] == [("cost", 2.5), ("recache", 12345)]


def test_prune_deletes_months_wholly_past_retention(tmp_path):
    for name in ("2026-06.jsonl", "2026-07.jsonl", "2026-08.jsonl", "2026-09.jsonl", "keep.txt"):
        _write(tmp_path, name, [])
    assert signals.prune(tmp_path, 40, now=NOW) == 2  # cutoff 2026-08-15: June and July are over
    assert sorted(p.name for p in (tmp_path / "signals").iterdir()) == ["2026-08.jsonl", "2026-09.jsonl", "keep.txt"]
    assert signals.prune(tmp_path / "nowhere", 1, now=NOW) == 0


def test_the_watcher_prunes_signals_with_the_store(tmp_path):
    config_dir = tmp_path / "config"
    _write(config_dir, "2020-01.jsonl", [])
    _write(config_dir, f"{datetime.now(timezone.utc):%Y-%m}.jsonl", [])
    (tmp_path / "projects").mkdir()
    store = Store(":memory:")
    store.open()
    FileWatcher(store, ServeOptions(projects_root=tmp_path / "projects", config_dir=config_dir, retention_days=30)).run_once()
    assert [p.name for p in (config_dir / "signals").iterdir()] == [f"{datetime.now(timezone.utc):%Y-%m}.jsonl"]
