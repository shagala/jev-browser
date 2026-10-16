# Real Google Flights observations

Captured 2026-10-16 from `usecases.grid`'s own start URL (MIA→CCS, 2026-11-24 →
2026-12-01), screenshots stripped and the signed-in account chip redacted. They exist so
the picker-versus-grid regression can be tested without the network.

| File | What it is |
| --- | --- |
| `flights-results.json` | the results page, with the `Date grid` button present |
| `flights-date-grid.json` | the real grid: a selected `Dates` tab and 49 `$…, <date> to <date>` cells |
| `flights-calendar-picker.json` | the lookalike — it renders a price per day too |

The picker was once mistaken for the grid, and it is the reason `expect` carries negative
and cardinality assertions rather than only positive ones. The two surfaces differ by:

| | real grid | picker |
| --- | --- | --- |
| `Dates` tab, selected | yes | absent |
| `Reset` button | absent | yes |
| price label shape | `$855, Nov 21 to Nov 28` | `Saturday, November 21, 2026 , 855 US dollars` |

Prices in these files are real quotes from that date and will not match today's.

**Not covered here:** the below-the-fold case. The `Date grid` button's position depends
on how tall the results list renders, so it cannot be pinned in a fixture.
`scripts/check_replay.py` covers the scroll ladder against a real DOM instead.
