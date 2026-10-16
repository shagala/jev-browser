"""The picker-versus-grid regression, against real captured observations. Offline.

`tests/fixtures/README.md` records where these came from and how they differ.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from usecases import grid, player
from usecases import match as m
from usecases.recipe import Match

FIXTURES = Path(__file__).parent / "fixtures"
# The expect block the flights step needs in order to tell the two surfaces apart.
GRID_EXPECT = {
    "all_of": [{"kind": "click", "role": "tab", "label": "Dates", "selected": "true"}],
    "must_not": [{"kind": "click", "role": "button", "label": "Reset"}],
    "count": {"label_matches": r"^\$[\d,]+, .* to .*$", "min": 10},
    "poll": {"attempts": 1, "interval_ms": 0},
}


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def results():
    return load("flights-results")


@pytest.fixture
def date_grid():
    return load("flights-date-grid")


@pytest.fixture
def picker():
    return load("flights-calendar-picker")


def test_the_recorded_descriptor_resolves_on_the_real_results_page(results):
    action = m.resolve(results, Match(kind="click", role="button", label="Date grid"))
    assert action["label"] == "Date grid"


def test_the_grid_step_does_not_resolve_against_the_calendar_picker(picker):
    # The picker is reachable by a misread of the same goal; the descriptor must not match it.
    with pytest.raises(m.NoMatch):
        m.resolve(picker, Match(kind="click", role="button", label="Date grid"))


def test_expect_accepts_the_real_grid(date_grid):
    assert player.evaluate(date_grid, GRID_EXPECT) is None


def test_expect_rejects_the_calendar_picker(picker):
    # Positive assertions alone would not: the picker renders a price per day too.
    assert player.evaluate(picker, GRID_EXPECT) is not None


def test_each_negative_assertion_earns_its_place(picker):
    positives_only = {k: v for k, v in GRID_EXPECT.items() if k in {"count", "poll"}}
    # The cardinality check alone already rejects it, because the picker's labels read
    # "Saturday, November 21, 2026 , 855 US dollars" rather than "$855, Nov 21 to Nov 28".
    assert player.evaluate(picker, positives_only) is not None
    assert any(a.get("label") == "Reset" for a in picker["actions"])
    assert not any(a.get("role") == "tab" for a in picker["actions"])


def test_the_verifier_parses_the_real_grid(date_grid):
    watch = grid.Watch("fixture", "MIA", "CCS", date(2026, 11, 21), date(2026, 11, 28))
    window = grid.Window(watch, date(2026, 11, 24), date(2026, 12, 1), "replay")
    cells, problem = grid.verify(date_grid, window)
    assert problem is None and len(cells) == 49


def test_the_verifier_rejects_the_calendar_picker(picker):
    watch = grid.Watch("fixture", "MIA", "CCS", date(2026, 11, 21), date(2026, 11, 28))
    window = grid.Window(watch, date(2026, 11, 24), date(2026, 12, 1), "replay")
    cells, problem = grid.verify(picker, window)
    assert cells == [] and problem
