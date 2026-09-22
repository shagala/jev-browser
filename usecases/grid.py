"""Pure logic for the Google Flights date grid: watches, 7x7 window tiling, cell parsing, verification."""

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import quote

CITIES = {"MIA": "Miami", "CCS": "Caracas", "MAD": "Madrid", "MEX": "Mexico City"}
HALF = timedelta(days=3)  # Google centres the grid on the selected pair: 3 days each side.
STEP = timedelta(days=7)
HOME = "https://www.google.com/travel/flights?hl=en&curr=USD"
# Google's calendar picker also shows a price per day, so goals must name the "Date grid" view explicitly.
DATE_GRID_STEP = (
    'Click the button named "Date grid" (next to "Price graph", below the flight results) to open the grid of '
    "round-trip prices with departure dates as columns and return dates as rows. That button is often below "
    "the visible area: scroll down until it appears, then click it. Do not open the Departure or Return date "
    "fields or the calendar. Stop when that price grid is visible."
)
DIRECT_GOAL = DATE_GRID_STEP
MONTH_NAMES = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
MONTHS = {name: number for number, name in enumerate(MONTH_NAMES, 1)}
CELL = re.compile(
    r"^(?P<currency>\D{1,3}?)(?P<price>\d[\d,]*),(?: (?P<flag>low price|cheapest price),)?"
    r" (?P<dm>[A-Za-z]{3,4}) (?P<dd>\d{1,2}) to (?P<rm>[A-Za-z]{3,4}) (?P<rd>\d{1,2})(?P<selected>, selected)?$"
)


@dataclass(frozen=True)
class Watch:
    name: str
    origin: str
    destination: str
    start: date
    end: date

    @property
    def origin_city(self):
        return CITIES[self.origin]

    @property
    def destination_city(self):
        return CITIES[self.destination]


@dataclass(frozen=True)
class Window:
    watch: Watch
    anchor_depart: date
    anchor_return: date
    mode: str

    @property
    def columns(self):
        return [self.anchor_depart - HALF + timedelta(days=i) for i in range(7)]

    @property
    def rows(self):
        return [self.anchor_return - HALF + timedelta(days=i) for i in range(7)]

    @property
    def expected_pairs(self):
        # Google renders same-day returns too; they are stored but never in scope.
        return [(d, r) for d in self.columns for r in self.rows if r >= d]


@dataclass(frozen=True)
class Cell:
    depart: date
    ret: date
    price: int
    flag: str | None
    selected: bool
    raw: str


def load_watches(path):
    watches = []
    for item in json.loads(path.read_text()):
        for code in (item["origin"], item["destination"]):
            if code not in CITIES:
                raise ValueError(f"Unknown airport code {code}; add it to CITIES in usecases/grid.py")
        w = Watch(
            item["name"],
            item["origin"],
            item["destination"],
            date.fromisoformat(item["start"]),
            date.fromisoformat(item["end"]),
        )
        if w.end <= w.start:
            raise ValueError(f"Watch {w.name}: end must be after start")
        watches.append(w)
    return watches


def _anchors(start, last):
    """Anchors start+3, start+10, ... while the window's first day is still on or before `last`."""
    anchor, out = start + HALF, []
    while anchor - HALF <= last:
        out.append(anchor)
        anchor += STEP
    return out


def windows(watch):
    seen, out = set(), []
    for d in _anchors(watch.start, watch.end - timedelta(days=1)):
        for r in _anchors(watch.start, watch.end):
            if r <= d:
                r = d + timedelta(days=1)  # Google rejects a return on or before departure.
            first_depart, last_return = max(d - HALF, watch.start), min(r + HALF, watch.end)
            if first_depart >= last_return or (d, r) in seen:
                continue
            seen.add((d, r))
            out.append(Window(watch, d, r, "full" if not out else "direct"))
    return out


def resolve_year(month, day, anchor):
    """Grid labels omit the year; pick the candidate nearest the anchor so Dec/Jan windows resolve correctly."""
    candidates = [date(anchor.year + delta, month, day) for delta in (-1, 0, 1)]
    return min(candidates, key=lambda d: abs((d - anchor).days))


def parse_cell(label, anchor):
    match = CELL.match(label.strip())
    if not match:
        return None
    if match["currency"] != "$":
        raise ValueError(f"Unexpected currency in {label!r}; only USD ($) is stored")
    dm, rm = MONTHS.get(match["dm"][:3].lower()), MONTHS.get(match["rm"][:3].lower())
    if not dm or not rm:
        return None
    return Cell(
        depart=resolve_year(dm, int(match["dd"]), anchor),
        ret=resolve_year(rm, int(match["rd"]), anchor),
        price=int(match["price"].replace(",", "")),
        flag=match["flag"],
        selected=bool(match["selected"]),
        raw=label,
    )


def start_url(window):
    if window.mode == "full":
        return HOME
    w = window.watch
    query = (
        f"Flights from {w.origin} to {w.destination} "
        f"on {window.anchor_depart:%Y-%m-%d} through {window.anchor_return:%Y-%m-%d}"
    )
    return f"{HOME}&q={quote(query)}"


def _long(d):
    return f"{d:%B} {d.day}, {d.year}"


def goal(window):
    if window.mode == "direct":
        return DIRECT_GOAL
    w = window.watch
    return (
        f"Search round-trip flights from {w.origin_city} to {w.destination_city} "
        f"departing on {_long(window.anchor_depart)} and returning on {_long(window.anchor_return)}, "
        "one adult in economy. In the calendar, click each date once, then Done, then Search. "
        f"On the results page: {DATE_GRID_STEP} Do not select a flight."
    )


def verify(page, window):
    """Independent checks on the final observed page. Jev's DONE is not evidence."""
    w = window.watch
    expected_title = f"{w.origin_city} to {w.destination_city} | Google Flights"
    if not page.get("title", "").startswith(expected_title):
        return [], f"title mismatch: {page.get('title')!r}"
    tabs = [a for a in page["actions"] if a.get("role") == "tab" and a["label"].strip() == "Dates"]
    if not any(a.get("selected") == "true" for a in tabs):
        return [], "Dates tab not selected"
    cells = []
    for action in page["actions"]:
        if action["kind"] != "click":
            continue
        try:
            cell = parse_cell(action["label"], window.anchor_depart)
        except ValueError as error:
            return [], str(error)
        if cell:
            cells.append(cell)
    if not cells:
        return [], "no grid cells parsed"
    selected = [c for c in cells if c.selected]
    anchor = (window.anchor_depart, window.anchor_return)
    if len(selected) != 1 or (selected[0].depart, selected[0].ret) != anchor:
        got = f"{selected[0].depart}/{selected[0].ret}" if len(selected) == 1 else f"{len(selected)} selected"
        return [], f"selected cell {got} != anchor {anchor[0]}/{anchor[1]}"
    expected = set(window.expected_pairs)
    stray = [c for c in cells if (c.depart, c.ret) not in expected]
    if stray:
        return [], f"grid shifted: {stray[0].raw!r} outside the expected window"
    return cells, None


def price_rows(window, cells, snapshot_date, run_id):
    w = window.watch
    by_pair = {(c.depart, c.ret): c for c in cells}
    rows = []
    for d, r in window.expected_pairs:
        cell = by_pair.get((d, r))
        rows.append(
            dict(
                snapshot_date=snapshot_date,
                origin=w.origin,
                destination=w.destination,
                depart_date=d,
                return_date=r,
                price_usd=cell.price if cell else None,
                flag=cell.flag if cell else None,
                in_scope=w.start <= d < r <= w.end,
                watch=w.name,
                raw_label=cell.raw if cell else None,
                run_id=run_id,
            )
        )
    return rows
