# Issue #1138 work-age fixtures

Saved Open Library search responses, fetched read-only on 2026-11-18 with a generic client name and no contact details, one request every 1.5 seconds.
Each file is the response body, re-indented and otherwise unchanged.

- `atomic-isbn.json`: `search.json?q=isbn:9780735211292` (Atomic Habits). Two works hold the ISBN; only one names an author.
- `sapiens-es-title.json`: `search.json?title=De animales a dioses&author=Harari`. Open Library keeps the Spanish translation as its own work, first published 2018, so a translation needs its confirmed original to get the work's real age.
- `castles.json`: `search.json?title=Castles made of sand&author=Jones`. The "Castles" probe case: the only document is "Castles made of sand" by Gwyneth Jones.

All three requests asked for `fields=key,title,author_name,first_publish_year,edition_count,editions,editions.key,editions.title`.
