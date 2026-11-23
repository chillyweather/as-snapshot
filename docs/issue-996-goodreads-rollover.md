# Issue #996 — Goodreads Choice Awards rolled over to 2026

Checked 2026-11-13 with the collector's own request headers
(`AutoScout/1.0 (+https://github.com/chillyweather)`, `Accept: */*`). PW Best
Books and Baillie Gifford, the other two sources in #996, are in
`docs/issue-996-pw-baillie-rollover.md`.

## What the page shows

- `https://www.goodreads.com/choiceawards/readers-favorite-nonfiction-books-2026`
  returns HTTP 200 with no redirect. On 2026-11-08 the same URL redirected to
  the 2025 page.
- The page title is "Readers' Favorite Nonfiction 2026 — Goodreads Choice
  Awards". The only visible page heading is "Opening Round Nominees"; the
  headings that carry the year sit in a hidden rules panel.
- The page lists 20 nominees, each a cover link to `/book/show/<id>-<slug>?from_choice=true`
  with the title and author in the image's alt text ("Title by Author"). The
  same row shape as the 2025 page.
- The rules panel dates the cycle: opening round 10–22 November, final round
  24–29 November, winners announced 3 December 2026.

## What changed

A new `Goodreads Choice Awards 2026 Nonfiction` source at the 2026 URL, with
the full onboarding metadata. The `Goodreads Choice Awards 2025 Nonfiction`
record is retired, not renamed, because the model-qualification benchmark
holds a frozen hard case for its 2025 page, and a frozen case stays valid only
while its source is configured.

The source is the category page, not a stage of it. The final round and the
winner announcement appear at the same URL, so no rollover is due until the
2027 nominees are published. Page shape therefore pins the year and nothing
about the stage:

- Expected visible marker "Readers' Favorite Nonfiction 2026". It matches the
  page title, which kept the same "Readers' Favorite Nonfiction <year>" form
  on the 2025 winner-stage page. A 2026 URL that serves the 2025 page fails
  page shape instead of collecting the 2025 books under the 2026 name.
- No marker for "Opening Round" and no forbidden "winner" marker, since both
  would fail when the page moves on.

The replay fixtures keep the live row markup of all 20 nominees, without the
vote forms, scripts or anything else. The extraction fixture describes them
as opening-round nominees with signal strength "reader favorite", the value
the 2025 benchmark reference uses, not as winners.

## Books already collected

A Candidate identity is the book's title and authors, not the source name, so
a book seen before keeps its identity, Candidate ID and editor decision when
the renamed source cites it (`tests/test_issue_996_rollover_keeps_identity.py`).
Fifteen of the 20 nominees are already in Candidate History, most of them
decided. None of them has a reader-interest signal yet, so under the existing
material-change rule a new independent signal family brings them back to the
review as upgraded, not as new.
