"""What MCP tool search saves (``tool_search``), from what ``parse.py``
keeps of Claude Code's deferred-tool records."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from claudeglass import quick_actions as qa
from claudeglass import tool_search
from claudeglass.model import TranscriptMeta, TranscriptResult, Turn
from claudeglass.parse import parse_transcript, tool_server
from claudeglass.pricing import load_pricing, price_turn
from claudeglass.units import Units

from helpers import attachment_line, tool_use_block, turn_line, write_jsonl

PRICING = load_pricing()
SONNET = PRICING.resolve_model("claude-sonnet-5").rates

LONG_DESCRIPTION = "Lists the pull requests of a repository, with their state and reviewers. " * 4


def _entry(name: str, description: str = "Does one thing.") -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {"type": "object", "properties": {"owner": {"type": "string"}}},
        "defer_loading": True,
    }


def _definition_chars(entry: dict) -> int:
    definition = {key: entry.get(key) for key in ("name", "description", "input_schema")}
    return len(json.dumps(definition, ensure_ascii=False, separators=(",", ":")))


# -- parsing ------------------------------------------------------------------


def test_tool_server_names_the_mcp_server_or_built_in():
    assert tool_server("mcp__github__list_issues") == "github"
    assert tool_server("mcp__Claude_Code_Remote__send_later") == "Claude_Code_Remote"
    assert tool_server("WebFetch") == "built-in"
    assert tool_server("mcp__broken") == "built-in"


def test_each_reply_keeps_its_deferred_tools_by_server(tmp_path: Path):
    names = ["mcp__github__list_prs", "mcp__github__merge_pr", "mcp__jira__search", "WebFetch"]
    loaded = _entry("mcp__github__list_prs", LONG_DESCRIPTION)
    lines = [
        attachment_line("deferred_tools_delta", addedNames=names, addedLines=names, removedNames=[]),
        turn_line(message_id="msg_1"),
        turn_line(message_id="msg_2", content=[tool_use_block("ToolSearch", "toolu_1", {"query": "prs"})]),
        attachment_line("deferred_tools_record", entries=[loaded]),
        turn_line(message_id="msg_3"),
        attachment_line("deferred_tools_delta", addedNames=[], removedNames=["mcp__jira__search"]),
        turn_line(message_id="msg_4"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    by_turn = [turn.deferred_tools_by_server for turn in result.turns]
    assert by_turn == [
        {"github": 2, "jira": 1, "built-in": 1},
        {"github": 2, "jira": 1, "built-in": 1},
        {"github": 1, "jira": 1, "built-in": 1},
        {"github": 1, "built-in": 1},
    ]
    assert result.turns[0].deferred_list_chars == sum(len(n) + 1 for n in names)
    assert result.turns[3].deferred_list_chars == sum(len(n) + 1 for n in names if n != "mcp__jira__search")
    assert result.tool_definition_chars == {"mcp__github__list_prs": _definition_chars(loaded)}


def test_a_transcript_without_tool_search_keeps_nothing(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    write_jsonl(path, [turn_line(message_id="msg_1")])

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    assert result.turns[0].deferred_tools_by_server == {}
    assert result.turns[0].deferred_list_chars == 0
    assert result.tool_definition_chars == {}


def test_only_names_and_sizes_are_kept_never_a_description_or_an_odd_name(tmp_path: Path):
    odd = ["../../etc/passwd", "name with spaces", "x" * 200]
    lines = [
        attachment_line("deferred_tools_delta", addedNames=["mcp__github__list_prs", *odd], removedNames=[]),
        attachment_line(
            "deferred_tools_record",
            entries=[_entry("mcp__github__list_prs", LONG_DESCRIPTION), _entry("../../etc/passwd"), "not an entry"],
        ),
        turn_line(message_id="msg_1"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    assert set(result.tool_definition_chars) == {"mcp__github__list_prs"}
    assert result.turns[0].deferred_tools_by_server == {}
    assert "pull requests of a repository" not in repr(result)


# -- the saving -----------------------------------------------------------------


def _turn(index: int, deferred: dict[str, int], *, read: int = 100_000, write: int = 0, list_chars: int = 0,
          tools: tuple[str, ...] = ()) -> Turn:
    return Turn(
        turn_index=index,
        model="claude-sonnet-5",
        input_tokens=10,
        cache_read_tokens=read,
        cache_creation_tokens=write,
        cc_5m=write,
        output_tokens=100,
        tool_names=tools,
        deferred_tools_by_server=deferred,
        deferred_list_chars=list_chars,
    )


def _result(*turns: Turn, definitions: dict[str, int] | None = None) -> TranscriptResult:
    return TranscriptResult(turns=list(turns), tool_definition_chars=definitions or {})


def test_deferred_tools_are_sized_by_their_own_server_or_every_server():
    # github's loaded definitions average 400 tokens; jira has none of its
    # own, so it takes the average of every loaded definition (400 too).
    definitions = {"mcp__github__a": 1200, "mcp__github__b": 2000}
    stats = tool_search.compute_tool_search(
        [_result(_turn(1, {"github": 10, "jira": 5}), definitions=definitions)], PRICING
    )

    github, jira = stats.servers["github"], stats.servers["jira"]
    assert github.definition_tokens == pytest.approx(400) and github.own_sizes and github.measured == 2
    assert jira.definition_tokens == pytest.approx(400) and not jira.own_sizes and jira.measured == 0
    assert stats.kept_tokens == pytest.approx(15 * 400)
    assert github.saving_usd == pytest.approx(10 * 400 * SONNET.cache_read / 1e6)
    assert stats.most_deferred == 15 and stats.most_deferred_mcp == 15


def test_a_reply_that_rebuilt_the_cache_prices_them_as_a_cache_write():
    definitions = {"mcp__github__a": 4000}
    stats = tool_search.compute_tool_search(
        [_result(_turn(1, {"github": 1}, read=0, write=50_000), definitions=definitions)], PRICING
    )
    assert stats.gross_usd == pytest.approx(1000 * SONNET.cache_write_5m / 1e6)


def test_the_name_list_and_search_only_replies_are_taken_off():
    definitions = {"mcp__github__a": 4000}
    search = _turn(2, {"github": 1}, tools=("ToolSearch",))
    both = _turn(3, {"github": 1}, tools=("ToolSearch", "Bash"))
    stats = tool_search.compute_tool_search(
        [_result(_turn(1, {"github": 1}, list_chars=400), search, both, definitions=definitions)], PRICING
    )

    search_cost = price_turn(search, PRICING.resolve_model(search.model)).total
    assert stats.search_replies == 1
    assert stats.search_usd == pytest.approx(search_cost)
    assert stats.list_usd == pytest.approx(100 * SONNET.cache_read / 1e6)
    assert stats.net_usd == pytest.approx(stats.gross_usd - stats.list_usd - stats.search_usd)
    assert stats.replies == 3 and stats.all_replies == 3


def test_nothing_is_priced_when_no_definition_was_loaded():
    stats = tool_search.compute_tool_search([_result(_turn(1, {"github": 40}))], PRICING)
    assert not stats.measurable
    assert stats.gross_usd == 0 and stats.kept_tokens == 0
    section = tool_search.build_section(stats)
    summary = {c.key: v for c, v in zip(section.tables[0].columns, section.tables[0].rows[0])}
    assert summary["replies"] == 1 and summary["most_deferred"] == 40
    assert summary["net_usd"] is None and summary["kept_per_reply"] is None
    assert any("isn't worked out" in note for note in section.notes)


def test_section_lists_servers_by_saving():
    definitions = {"mcp__github__a": 4000, "mcp__jira__b": 400}
    stats = tool_search.compute_tool_search(
        [_result(_turn(1, {"github": 2, "jira": 2, "built-in": 1}), definitions=definitions)], PRICING
    )
    section = tool_search.build_section(stats)
    assert section.key == "tool_search"
    names = [t.name for t in section.tables]
    assert names == ["tool_search_summary", "tool_search_by_server"]
    servers = [row[0] for row in section.tables[1].rows]
    assert servers[0] == "github"
    sized_from = {row[0]: row[4] for row in section.tables[1].rows}
    assert sized_from == {"github": "its own tools", "jira": "its own tools", "built-in": "all servers"}


# -- the check ----------------------------------------------------------------


def _table(name: str, rows: list[dict]):
    keys = list(rows[0]) if rows else []
    return NS(name=name, columns=[NS(key=k) for k in keys], rows=[[row[k] for k in keys] for row in rows])


def _ctx(tmp_path, sections):
    return qa.Context(
        model=NS(sections=sections, recommendations=[]),
        units=Units(billing_mode="api", currency="USD"),
        period="over the last 14 days",
        config_dir=tmp_path,
        effective={},
        effective_agents={},
    )


def _summary(**values):
    row = {"scope": "all replies", "replies": 0, "most_deferred": 0, "most_deferred_mcp": 0, "kept_per_reply": None,
           "net_usd": None}
    row.update(values)
    return _table("tool_search_summary", [row])


def test_check_reports_the_saving_by_server(tmp_path):
    servers = _table("tool_search_by_server", [
        {"server": "github", "most_deferred": 56, "kept_per_reply": 18_800.0, "saving_usd": 2.2},
        {"server": "built-in", "most_deferred": 32, "kept_per_reply": 15_500.0, "saving_usd": 1.8},
    ])
    section = NS(key="tool_search", tables=[
        _summary(replies=500, most_deferred=154, most_deferred_mcp=122, kept_per_reply=57_500.0, net_usd=5.58),
        servers,
    ])

    result = qa.run("tool-search", _ctx(tmp_path, [section]))

    assert result["status"] == "ok"
    assert "57,500 tokens" in result["summary"] and "122 of them MCP tools" in result["summary"]
    assert [row[0] for row in result["table"]["rows"]] == ["github", "Claude Code's own tools"]


def test_check_has_no_data_without_tool_search_or_loaded_definitions(tmp_path):
    none = qa.run("tool-search", _ctx(tmp_path, []))
    assert none["status"] == "no_data"
    unsized = qa.run("tool-search", _ctx(tmp_path, [NS(key="tool_search", tables=[_summary(replies=3,
                                                                                         most_deferred=40)])]))
    assert unsized["status"] == "no_data" and "40 tools" in unsized["summary"]
