# Issue #766 — regular sources that bring almost no new books: decisions

Operator decision, 2026-10-16, recorded in #766. The editor was not consulted.

New books are counted across the ten runs from 2026-08-26 to 2026-09-26, as a title and author no source had shown in an earlier run.

| Source | New books | Decision | Reason |
|---|---:|---|---|
| Die Zeit Sachbuch-Bestenliste | 0 | retired earlier (#764) | HTTP 403; subscriber content |
| Current Weekly Nonfiction Agent Extraction | 0 | retired earlier (#826) | one AP News article, not a list |
| TodosTusLibros Librerías Recomiendan No Ficción | 0, then 10 on 10-05 | **keep** | the low count was a collection defect, fixed in #753; it now brings new books |
| Amazon Charts Most Read Nonfiction | 1 | **keep** | an independent sales signal that corroborates books found elsewhere |
| Amazon.de Charts Meistgelesene Sachbücher | 1 | **keep** | the same, for the German market |
| IndieBound Indie Next List | 1 | **retire** | monthly and mostly fiction; usually one adult-nonfiction row per run |
| Publishers Marketplace Rights Offerings | 2 | **retire** | about 6 near-static rows per run |

## Consequences

- Both retirements are soft (`active: false`, `lifecycle_status: "retired"`) and reversible. The replay fixtures and overlay entries are kept, and the collector tests still measure them on a copy that is switched on.
- Publishers Marketplace was the only `rights/deal` source, so the portfolio no longer carries that signal family. Restore it, or onboard another rights source, if rights activity is wanted as a signal again.
- Each retirement saves one Codex extraction per weekly run.
