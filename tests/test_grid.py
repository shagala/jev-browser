"""Offline contracts for grid tiling, parsing, verification, and row shaping."""

from datetime import date, timedelta
from pathlib import Path

import pytest

from usecases import grid

ROOT = Path(__file__).resolve().parents[1]


def watch(name="mia-ccs-spring", origin="MIA", destination="CCS", start=date(2027, 3, 23), end=date(2027, 4, 5)):
    return grid.Watch(name, origin, destination, start, end)


def test_load_watches_reads_the_repo_config():
    watches = grid.load_watches(ROOT / "usecases" / "watches.json")
    assert [w.name for w in watches] == ["mia-ccs-spring", "mia-mad-spring", "mia-ccs-nov", "mia-mex-xmas"]
    assert watches[3].destination_city == "Mexico City"
    assert watches[3].start == date(2026, 12, 25) and watches[3].end == date(2027, 1, 3)


def test_load_watches_rejects_unknown_airport(tmp_path):
    path = tmp_path / "w.json"
    path.write_text(
        '[{"name": "x", "origin": "MIA", "destination": "ZZZ", "start": "2027-01-01", "end": "2027-01-09"}]'
    )
    with pytest.raises(ValueError, match="ZZZ"):
        grid.load_watches(path)


def test_load_watches_rejects_empty_trip_box(tmp_path):
    path = tmp_path / "w.json"
    path.write_text(
        '[{"name": "x", "origin": "MIA", "destination": "CCS", "start": "2027-01-09", "end": "2027-01-09"}]'
    )
    with pytest.raises(ValueError, match="end"):
        grid.load_watches(path)


@pytest.mark.parametrize(
    "start, end, expected",
    [
        # Spring box: two departure columns, two return rows, clamped/deduped to three windows.
        (date(2027, 3, 23), date(2027, 4, 5), [
            (date(2027, 3, 26), date(2027, 3, 27)),
            (date(2027, 3, 26), date(2027, 4, 2)),
            (date(2027, 4, 2), date(2027, 4, 3)),
        ]),
        # One-week box: one departure column, two return rows.
        (date(2026, 11, 21), date(2026, 11, 28), [
            (date(2026, 11, 24), date(2026, 11, 25)),
            (date(2026, 11, 24), date(2026, 12, 1)),
        ]),
        # Year wrap.
        (date(2026, 12, 25), date(2027, 1, 3), [
            (date(2026, 12, 28), date(2026, 12, 29)),
            (date(2026, 12, 28), date(2027, 1, 4)),
            (date(2027, 1, 4), date(2027, 1, 5)),
        ]),
    ],
)
def test_windows_tile_the_trip_box(start, end, expected):
    result = grid.windows(watch(start=start, end=end))
    assert [(w.anchor_depart, w.anchor_return) for w in result] == expected
    assert [w.mode for w in result] == ["full"] + ["direct"] * (len(expected) - 1)


def test_window_geometry():
    w = grid.Window(watch(), date(2027, 3, 26), date(2027, 3, 27), "full")
    assert w.columns == [date(2027, 3, 23 + i) for i in range(7)]
    assert w.rows == [date(2027, 3, 24 + i) for i in range(7)]
    assert all(r >= d for d, r in w.expected_pairs)
    assert (date(2027, 3, 23), date(2027, 3, 24)) in w.expected_pairs
    assert (date(2027, 3, 24), date(2027, 3, 24)) in w.expected_pairs  # same-day return is a real Google cell
    assert (date(2027, 3, 29), date(2027, 3, 24)) not in w.expected_pairs
    assert len(w.expected_pairs) == 34  # rows overlap columns; only r >= d pairs


def test_every_in_scope_pair_is_covered_by_some_window():
    w = watch()
    covered = {pair for win in grid.windows(w) for pair in win.expected_pairs}
    days = [date(2027, 3, 23) + timedelta(days=i) for i in range(14)]
    wanted = {(d, r) for d in days for r in days if d < r}
    assert wanted <= covered


# --- parsing -----------------------------------------------------------------------------------


def test_parse_cell_reads_price_dates_and_flags():
    anchor = date(2026, 10, 3)
    plain = grid.parse_cell("$745, Oct 3 to Oct 10", anchor)
    assert plain == grid.Cell(date(2026, 10, 3), date(2026, 10, 10), 745, None, False, "$745, Oct 3 to Oct 10")
    low = grid.parse_cell("$689, low price, Oct 6 to Oct 7", anchor)
    assert (low.price, low.flag, low.selected) == (689, "low price", False)
    cheapest = grid.parse_cell("$1,004, cheapest price, Sep 30 to Oct 13, selected", anchor)
    assert (cheapest.price, cheapest.flag, cheapest.selected) == (1004, "cheapest price", True)
    assert cheapest.depart == date(2026, 9, 30)


@pytest.mark.parametrize("label", ["Scroll right", "Dates", "OK", "Track prices", "$745", "Oct 3 to Oct 10"])
def test_parse_cell_ignores_non_cells(label):
    assert grid.parse_cell(label, date(2026, 10, 3)) is None


def test_parse_cell_rejects_other_currencies():
    with pytest.raises(ValueError, match="currency"):
        grid.parse_cell("€745, Oct 3 to Oct 10", date(2026, 10, 3))


@pytest.mark.parametrize(
    "month, day, anchor, expected",
    [
        (12, 30, date(2027, 1, 4), date(2026, 12, 30)),  # column before a January anchor
        (1, 2, date(2026, 12, 28), date(2027, 1, 2)),  # row after a December anchor
        (3, 26, date(2027, 3, 26), date(2027, 3, 26)),
    ],
)
def test_resolve_year_picks_the_nearest_year(month, day, anchor, expected):
    assert grid.resolve_year(month, day, anchor) == expected


def test_parse_cell_year_wrap_uses_the_anchor():
    cell = grid.parse_cell("$512, Dec 30 to Jan 2", date(2026, 12, 28))
    assert (cell.depart, cell.ret) == (date(2026, 12, 30), date(2027, 1, 2))


# --- goals, verification, rows -----------------------------------------------------------------


def window(mode="direct"):
    return grid.Window(watch(), date(2027, 3, 26), date(2027, 4, 2), mode)


def cell_actions(anchor_depart, anchor_return, columns, rows, price=700, skip=()):
    actions = [{"id": "e1", "kind": "click", "role": "tab", "label": "Dates", "selected": "true"}]
    for d in columns:
        for r in rows:
            if r < d or (d, r) in skip:
                continue
            selected = ", selected" if (d, r) == (anchor_depart, anchor_return) else ""
            actions.append({"id": f"e{len(actions) + 1}", "kind": "click", "role": "button",
                            "label": f"${price}, {d:%b} {d.day} to {r:%b} {r.day}{selected}"})
    return actions


def good_page(w=None, **kwargs):
    w = w or window()
    return {
        "title": f"{w.watch.origin_city} to {w.watch.destination_city} | Google Flights",
        "url": "https://www.google.com/travel/flights/search?tfs=x",
        "actions": cell_actions(w.anchor_depart, w.anchor_return, w.columns, w.rows, **kwargs),
    }


def test_start_url_and_goal_by_mode():
    full, direct = window("full"), window("direct")
    assert grid.start_url(full) == "https://www.google.com/travel/flights?hl=en&curr=USD"
    assert grid.start_url(direct) == (
        "https://www.google.com/travel/flights?hl=en&curr=USD&q="
        "Flights%20from%20MIA%20to%20CCS%20on%202027-03-26%20through%202027-04-02"
    )
    assert "Miami to Caracas" in grid.goal(full) and "March 26, 2027" in grid.goal(full)
    assert "April 2, 2027" in grid.goal(full) and "Do not select a flight" in grid.goal(full)
    assert grid.DATE_GRID_STEP in grid.goal(full)
    assert grid.goal(direct) == grid.DIRECT_GOAL and '"Date grid"' in grid.DIRECT_GOAL


def test_verify_accepts_a_centred_grid():
    cells, reason = grid.verify(good_page(), window())
    assert reason is None
    assert len(cells) == len(window().expected_pairs)
    assert sum(c.selected for c in cells) == 1


def test_verify_rejects_wrong_route_title():
    page = good_page()
    page["title"] = "Miami to Madrid | Google Flights"
    assert grid.verify(page, window()) == ([], "title mismatch: 'Miami to Madrid | Google Flights'")


def test_verify_rejects_when_dates_tab_not_selected():
    page = good_page()
    page["actions"][0]["selected"] = "false"
    assert grid.verify(page, window())[1] == "Dates tab not selected"


def test_verify_rejects_no_cells():
    page = good_page()
    page["actions"] = page["actions"][:1]
    assert grid.verify(page, window())[1] == "no grid cells parsed"


def test_verify_rejects_shifted_grid():
    w = window()
    page = good_page(w)
    page["actions"] = cell_actions(
        w.anchor_depart, w.anchor_return + timedelta(days=1), w.columns, [r + timedelta(days=1) for r in w.rows]
    )
    assert grid.verify(page, w)[1].startswith("selected cell 2027-03-26/2027-04-03 != anchor")


def test_verify_rejects_currency():
    page = good_page()
    page["actions"][1]["label"] = page["actions"][1]["label"].replace("$", "€")
    assert "currency" in grid.verify(page, window())[1]


def test_price_rows_cover_expected_pairs_with_null_for_missing():
    w = window()
    missing = (date(2027, 3, 24), date(2027, 3, 31))
    cells, reason = grid.verify(good_page(w, skip={missing}), w)
    assert reason is None
    rows = grid.price_rows(w, cells, date(2026, 9, 21), run_id=7)
    assert len(rows) == len(w.expected_pairs)
    by_pair = {(r["depart_date"], r["return_date"]): r for r in rows}
    assert by_pair[missing]["price_usd"] is None and by_pair[missing]["raw_label"] is None
    present = by_pair[(date(2027, 3, 23), date(2027, 3, 30))]
    assert present["price_usd"] == 700 and present["in_scope"] is True and present["run_id"] == 7
    assert present["watch"] == "mia-ccs-spring" and present["origin"] == "MIA" and present["destination"] == "CCS"
    assert present["snapshot_date"] == date(2026, 9, 21)


def test_price_rows_same_day_return_is_stored_but_out_of_scope():
    w = grid.Window(watch(), date(2027, 3, 26), date(2027, 3, 27), "direct")  # clamped: rows overlap columns
    cells, _ = grid.verify(good_page(w), w)
    rows = grid.price_rows(w, cells, date(2026, 9, 21), run_id=1)
    same_day = next(r for r in rows if r["depart_date"] == r["return_date"])
    assert same_day["price_usd"] == 700 and same_day["in_scope"] is False


def test_price_rows_flag_out_of_scope_pairs():
    w = grid.Window(watch(), date(2027, 4, 2), date(2027, 4, 3), "direct")  # rows reach Apr 6, past the box
    cells, _ = grid.verify(good_page(w), w)
    rows = grid.price_rows(w, cells, date(2026, 9, 21), run_id=1)
    late = next(r for r in rows if r["return_date"] == date(2027, 4, 6))
    assert late["in_scope"] is False
