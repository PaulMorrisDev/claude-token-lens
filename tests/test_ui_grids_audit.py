"""The data grid after the design audit (``service/static/grid.js``).

What a screenshot can't prove, read from the source (docs/ui.md,
"Data grid"):

- a table the server lists A to Z opens biggest first on its lead
  measure until the reader picks a sort, with a total row kept last;
  dated, bucketed, grouped and server-ranked tables keep their order;
- a folded grid never hides a row an action cites;
- headings wrap to two lines, never mid-word, and a heading still cut
  says the rest in its title;
- a grid that scrolls sideways shades the edge with more to see, from
  its scroll position, and again when it resizes;
- on a heading that sorts, the column's (?) leaves the Tab order and
  the ? key opens it; the heading's name is its label and unit;
- a table empty for a known reason says it (five-hour blocks on API
  billing);
- the note under a section's tables opens each table left to the full
  report in the table drawer;
- a long project slug ends in an ellipsis with the full slug on hover;
- controls every grid has name their table, and an evidence mark its row.
"""

from __future__ import annotations

import re

from test_service_static import _app_js, _function_source, _static_text


def _grid() -> str:
    return _static_text("grid.js")


def _data_grid() -> str:
    return _function_source(_grid(), "dataGrid")


def _css_rule(selector: str) -> str:
    """The bodies of the top-level app.css rules whose selector list is
    exactly ``selector``, one after another."""
    css = _static_text("app.css")
    bodies = re.findall(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert bodies, f"no {selector} rule in app.css"
    return "\n".join(bodies)


def _js_regex(src: str, name: str) -> re.Pattern[str]:
    """A ``var NAME = /.../;`` regex literal from grid.js, compiled (its
    syntax is the part JavaScript and Python share)."""
    match = re.search(r"var " + re.escape(name) + r" = /(.+)/;\n", src.replace("\r\n", "\n"))
    assert match, f"no regex {name} in grid.js"
    return re.compile(match.group(1))


# -- P1-5: an A-to-Z list ranks ---------------------------------------------


def test_an_a_to_z_table_opens_biggest_first_until_a_sort_is_picked() -> None:
    grid = _data_grid()
    stored = grid.index('var sort = sortable ? readJson("tls:sort:" + gridId) : null;')
    ranked = grid.index("if (!sort && sortable && spec.rank && bar !== -1) sort = { key: columns[bar].key, ascending: false };")
    assert stored < ranked, "the reader's own sort wins over the ranking"
    # Only the reader's pick is stored, never the ranking's.
    assert grid.count('storageSet("tls:sort:"') == 1
    assert 'storageSet("tls:sort:"' in _function_source(grid, "sortBy")
    render = _function_source(_grid(), "renderTable")
    assert "var rank = ranking(table);" in render
    assert "rank: !!rank," in render and "totalLast: rank ? rank.total : null," in render


def test_only_a_to_z_lists_rank() -> None:
    ranking = _function_source(_grid(), "ranking")
    # Dated, read down its rows, or led by a number: the order means something.
    assert "NEWEST_LAST[table.name]" in ranking
    assert "hasKeys(table.row_groups) || hasKeys(table.row_kinds)" in ranking
    assert "NUMERIC_KINDS[first.kind] || SCALE_KEY.test(first.key" in ranking
    assert "SCALE_VALUE.test(name)" in ranking
    # A to Z whole, or with one total or "other" row after the run.
    assert "if (aToZ(names)) return { total: null };" in ranking
    assert "aToZ(names.slice(0, -1))) return { total: names[names.length - 1] };" in ranking


def test_a_scale_keeps_its_order() -> None:
    grid = _grid()
    key = _js_regex(grid, "SCALE_KEY")
    value = _js_regex(grid, "SCALE_VALUE")
    for scale in ("gap_bucket", "window", "effort", "hour", "day", "iso_week", "month", "depth", "level", "start"):
        assert key.search(scale), scale
    for name in ("agent_type", "model", "tool", "project", "skill", "cause"):
        assert not key.search(name), name
    for bucket in ("<5m", "5-60m", "> 1h", "~200k", "0-50k", "1"):
        assert value.match(bucket), bucket
    for name in ("Explore", "general-purpose", "top-level", "claude-opus-4"):
        assert not value.match(name), name


def test_a_total_row_stays_last_and_takes_no_bar() -> None:
    grid = _data_grid()
    assert "String(keyOf(row)) === String(spec.totalLast)" in _function_source(grid, "isTotal")
    assert "if (isTotal(a) !== isTotal(b)) return isTotal(a) ? 1 : -1;" in _function_source(grid, "applySort")
    assert "!isTotal(row);" in grid, "the total row doesn't set the bars' scale"
    body_row = _function_source(grid, "bodyRow")
    assert "var total = isTotal(row);" in body_row and "column.index === bar && !total" in body_row


def test_a_fold_never_hides_a_cited_row() -> None:
    grid = _data_grid()
    fold = _function_source(grid, "foldSize")
    assert "cited[String(keyOf(row))]" in fold
    assert "fromEnd() ? orderedRows.length - i : i + 1" in fold
    assert "return limited && !expanded && rows.length > foldSize() + 2;" in _function_source(grid, "folded")
    draw = _function_source(grid, "drawBody")
    assert "orderedRows.slice(-size)" in draw and "orderedRows.slice(0, size)" in draw
    marks = grid[grid.index("markRows: function (mark)") :]
    marks = marks[: marks.index("\n    },")]
    assert "cited = {};" in marks and "if (foldSize() !== before)" in marks and "drawBody();" in marks


# -- P1-6: headings wrap, edges say the grid scrolls ------------------------


def test_headings_wrap_to_two_lines_never_mid_word() -> None:
    assert "white-space: normal;" in _css_rule(".data-grid thead th")
    label = _css_rule(".th-label")
    assert "-webkit-line-clamp: 2;" in label and "line-clamp: 2;" in label and "overflow: hidden;" in label
    grid = _grid()
    # Each heading keeps the longer line of its most even two-line split.
    width = _function_source(grid, "labelWidth")
    assert "if (length <= LABEL_ONE_LINE) return length;" in width
    assert "best = Math.min(best, Math.max(first, length - first - 1));" in width
    cell = _function_source(_data_grid(), "headerCell")
    assert 'label.style.minWidth = labelWidth(words) + "ch";' in cell
    # A hyphenated word, and the last word with its unit, stay whole.
    assert 'if (!last && word.indexOf("-") === -1) {' in cell
    assert 'el("span", { class: "nowrap", text: word })' in cell
    assert 'if (last) whole.appendChild(el("span", { class: "unit", text: " " + unit }));' in cell
    assert "white-space: nowrap;" in _css_rule(".nowrap")
    cut = _function_source(_data_grid(), "titleCutLabels")
    assert "label.scrollHeight > label.clientHeight + 1" in cut and "label.title = label.textContent;" in cut


def test_a_grid_that_scrolls_sideways_shades_the_edge_with_more() -> None:
    grid = _data_grid()
    assert 'var frame = el("div", { class: "grid-frame" }, [scroller]);' in grid
    cues = _function_source(grid, "setCues")
    assert 'frame.classList.toggle("cue-start", room > 1 && scroller.scrollLeft > 1);' in cues
    assert 'frame.classList.toggle("cue-end", room > 1 && scroller.scrollLeft < room - 1);' in cues
    # Once a frame per scroll, from a passive listener, and on resize.
    assert "if (!cueFrame) cueFrame = requestAnimationFrame(setCues);" in grid
    assert "{ passive: true }" in grid
    observer = grid[grid.index("var observer = new ResizeObserver(") :]
    observer = observer[: observer.index("observer.observe(table);")]
    assert "setCues();" in observer and "titleCutLabels();" in observer
    assert "opacity: 1;" in _css_rule(".grid-frame.cue-end::after")
    assert "pointer-events: none;" in _css_rule(".grid-frame::after")
    assert "box-shadow:" in _css_rule(".grid-frame.cue-start .grid-wide .data-grid tbody td:first-child")


# -- P2-7 and P3-22: one Tab stop a column, a clean heading name -----------


def test_a_sortable_heading_is_one_tab_stop_and_its_help_opens_on_the_question_key() -> None:
    cell = _function_source(_data_grid(), "headerCell")
    assert "if (sortable) helpBtn.tabIndex = -1;" in cell
    assert 'if (helpBtn) th.setAttribute("aria-keyshortcuts", "?");' in cell
    key = cell[cell.index('} else if (event.key === "?" && helpBtn) {') :]
    assert "event.preventDefault();" in key[:300] and "helpBtn.click();" in key[:300]
    # The page's own ? (the shortcuts sheet) stands aside for a handled key.
    assert "if (event.defaultPrevented" in _function_source(_static_text("palette.js"), "onKeydown")
    # A focused heading shows its help is there.
    assert "border-color: var(--accent);" in _css_rule(".data-grid thead th.sortable:focus-visible .col-help-btn")


def test_a_headings_name_is_its_label_and_unit_without_the_question_mark() -> None:
    cell = _function_source(_data_grid(), "headerCell")
    assert '"aria-label": unit ? name + " (" + unit + ")" : name,' in cell


# -- P2-10: a table's own empty text ----------------------------------------


def test_five_hour_blocks_say_why_they_are_empty_on_api_billing() -> None:
    empty = _function_source(_grid(), "emptyText")
    assert 'table.name === "five_hour_blocks" && (state.units || {}).mode !== "subscription"' in empty
    assert "Pro or Max plan" in empty
    render = _function_source(_grid(), "renderTable")
    assert "var empty = emptyText(table);" in render
    assert "empty: empty[0]," in render and "emptyNext: empty[1]," in render


# -- P2-15: the full report's tables open in the drawer ---------------------


def test_the_full_report_note_opens_each_table_in_the_drawer() -> None:
    grid = _grid()
    note = _function_source(grid, "reportTablesNote")
    assert 'button("Open " + (table.title || table.name), {' in note
    assert "reportTableDrawer(table, sectionTitle);" in note
    assert "container.appendChild(reportTablesNote(reportOnly, sectionTitle));" in _function_source(grid, "renderPlacedTables")
    evidence = _static_text("evidence.js")
    assert "setReportTableDrawer(function (table, sectionTitle) {" in evidence
    assert 'showTable({ section: { title: sectionTitle || "" }, table: table }, "drawer-report-" + table.name, null, null);' in evidence
    assert "showTable(found," in _function_source(evidence, "tableDrawer")


# -- P3-20: long project slugs ----------------------------------------------


def test_a_long_project_slug_ends_in_an_ellipsis_with_the_slug_on_hover() -> None:
    cell = _function_source(_grid(), "cellContent")
    assert 'el("span", { class: "entity-name", title: value, text: projectName(value) })' in cell
    rule = _css_rule(".entity-name")
    for decl in ("max-width: 28ch;", "overflow: hidden;", "text-overflow: ellipsis;", "white-space: nowrap;"):
        assert decl in rule, decl


# -- names that say which table ---------------------------------------------


def test_controls_every_grid_has_name_their_table() -> None:
    grid = _data_grid()
    assert 'return spec.caption ? words + ": " + spec.caption : words;' in _function_source(grid, "inTable")
    assert 'chooserButton.setAttribute("aria-label", inTable(text));' in _function_source(grid, "updateChooserLabel")
    assert 'label: inTable("Columns to show")' in grid
    assert 'moreButton.setAttribute("aria-label", inTable(words));' in _function_source(grid, "updateMore")
    assert 'moreButton.setAttribute("aria-label", inTable("Show all " + rows.length + " rows"));' in grid
    cell = _function_source(grid, "headerCell")
    assert '"What is " + name + (spec.caption ? " in " + spec.caption.replace(/\\?$/, "") : "") + "?"' in cell


def test_an_evidence_mark_names_its_row_and_table() -> None:
    app_js = _app_js()
    button = _function_source(app_js, "feedsButton")
    assert '"aria-label": named("Evidence for " + words), title: "Evidence for " + words' in button
    assert '"aria-label": named("Feeds " + words)' in button
    feeds = _function_source(app_js, "markFeeds")
    assert "feedsButton(actions, true, title)" in feeds
    assert 'feedsButton(rowActions, false, (name || key) + (title ? " in " + title : ""))' in feeds
    grid = _data_grid()
    assert "markRow(tr, rowMark(String(key), rowName(tr)))" in grid
    assert 'markRow(tr, mark(tr.getAttribute("data-row-key"), rowName(tr)))' in grid
