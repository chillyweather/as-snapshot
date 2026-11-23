# Issue #826 — AP News retirement and National Book Awards 2026 rollover

Run audited: the 2026-10-15 weekly run (operation `d07f0435`), and bounded requests made on 2026-10-15.

## Decision

Operator decision, 2026-10-15, recorded in #826.
The editor was not consulted.
Two separate configuration changes, one commit each.

| Source | Decision | Reason |
|---|---|---|
| Current Weekly Nonfiction Agent Extraction | retired (soft, reversible) | the URL is one AP News article, not a recurring list |
| National Book Awards 2025 Nonfiction | rolled over to National Book Awards 2026 Nonfiction | the 2026 finalists and longlist are published |

## Current Weekly Nonfiction Agent Extraction

- The configured URL is a single AP News article: `https://apnews.com/article/ana-huang-matt-dinniman-bde7ca724c62a6735d528333421a0a78`.
- It returned HTTP 403 in the 2026-09-26, 2026-10-14 and 2026-10-15 runs, and exceeded the response-size limit on 2026-10-10.
- #766 counts 0 new books in its 7 successful runs.
- It is a leftover from the early pilot, not a weekly signal.

The retirement sets `active: false` and `lifecycle_status: "retired"` with a recorded reason, as for Die Zeit (#764).
The replay fixtures, overlay entries, and the frozen model-qualification case are kept; qualification accepts a retired source that is still configured.
To restore: a recurring list URL, then onboarding under the runbook.

## National Book Awards 2026 Nonfiction

- The 2025 identity was flagged `rollover-check-required` by the annual source audit in every run, and failed extraction on 2026-10-15 (#823).
- `https://www.nationalbook.org/awards-prizes/national-book-awards-2026/?cat=nonfiction` returns HTTP 200 (about 130 KB) to the collector's own request headers (`AutoScout/1.0 (+https://github.com/chillyweather)`, `Accept: */*`).
- The page is server-rendered. Under its `FINALISTS` heading it lists five books, under `LONGLIST` the five further longlisted books, and the `WINNER` section is empty.
- The page states that the winners are announced on November 18.
- The year-specific longlist article URL used for 2025 has no 2026 counterpart (`/2026-national-book-awards-longlist-for-nonfiction/` returns HTTP 404), so the source moves to the year page with the nonfiction category selected.

The source keeps its legacy contract and its place in the grandfathered list in `tests/test_source_onboarding_contract.py`, under the new name: it is the same configured source in its next edition, not a new source.
The replay fixtures are replaced by a 2026 page excerpt that keeps the live entry markup of the first finalist, and one representative mention with signal strength `shortlist`.
The 2025 identity keeps its Candidate History; only the configuration name changes.

### Model-qualification benchmark

The frozen benchmark case for the 2025 identity is removed, and the 2026 identity is listed as uncovered, as for Baillie Gifford (#762).
This takes the benchmark from 30 to 29 cases.
The floor in `test_every_benchmark_reference_still_validates_under_the_current_contract` is lowered from 30 to 29 by operator decision on 2026-10-15.
Restore it after the next `./scripts/qualify-model --refresh-benchmark` captures the 2026 edition.

### Next step for this source

When the winner is announced on 2026-11-18, the same page shows it under `WINNER`.
No configuration change is needed for that; the next run reads the winner from the same URL.
