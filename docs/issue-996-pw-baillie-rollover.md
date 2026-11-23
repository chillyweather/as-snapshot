# Issue #996 — PW Best Books and Baillie Gifford rollovers

Checked 2026-11-08 with the collector's own request headers
(`AutoScout/1.0`, `Accept: */*`). Goodreads Choice Awards is the third source
in #996 and is not changed here; see the last section.

## PW Best Books: rolled over to 2026

- `https://best-books.publishersweekly.com/pw/best-books/2026` returns HTTP
  200 with no redirect. The page is titled "Best Books 2026" and lists 150
  selections as `<ul class="candy">` rows of `<h3>` title and `<h4>` author,
  the same shape as the 2025 page (#199, #700). It is a different page from
  2025, not a copy of it.
- On 2026-09-14 the same URL returned HTTP 404 (#700), so the 2026 list was
  published between the two checks.
- The year page carries no genre labels. The genre page
  `/pw/best-books/2026/nonfiction` lists 25 nonfiction selections; the replay
  fixture's twelve nonfiction rows are taken from it, and all 25 are on the
  year page.

Change: a new `PW Best Books 2026` source at the 2026 URL, with the full
onboarding metadata. The `PW Best Books 2025` record is retired, not
renamed. It stays as a retired record because the model-qualification
benchmark holds a frozen hard case for its 2025 page, and a frozen case
stays valid only while its source is configured, active or retired.

The replay page fixture keeps the live row markup of sixteen rows: twelve
nonfiction and the first four rows of the page, which are a graphic novel,
a novel, a children's book and a picture book, so the fixture also exercises
the adult-nonfiction filter. Review prose is not kept, under PW's permission
(#935).

## Baillie Gifford Prize: moved to the winner stage

- `https://www.thebailliegiffordprize.co.uk/year-by-year/2026` now has
  sections headed The winner, The shortlist and The longlist, in that order.
  The winner, announced 4 November 2026, is *Alexander: God, King, Man* by
  Edmund Richardson.
- The winner card's heading prints the title and subtitle on two lines, and
  the shortlist card printed the title as "Alexander" (#762). The extraction
  keeps "Alexander", which grounds on the card and is the identity the book
  already has in Candidate History (`alexander|edmund richardson`).
- The shortlist source failed page-shape validation by design: its
  forbidden visible marker `The winner` is now a heading on the page.

Change: the source becomes `Baillie Gifford Prize 2026 Winner`, bounded to the
section headed The winner, with signal strength `winner`. The winner is the
last stage of the cycle, so the source declares no forbidden later stage.
The next rollover is to the 2027 longlist, usually announced in September.

Operator decision, 2026-11-08: the winner stage, over retiring the source
until 2027 or tracking winner and shortlist together. All six shortlisted
books are already in Candidate History, and the editor had already marked
*Alexander* as rejected, so no option brings a new book this year. The winner
stage stops re-serving the shortlist every week and keeps the source active
for the next cycle.

The live page as served on 2026-11-08 is retained under
`tests/fixtures/issue-996-retained-capture/`.

## Goodreads Choice Awards: not yet

`https://www.goodreads.com/choiceawards/readers-favorite-nonfiction-books-2026`
redirects to the 2025 Readers' Favorite page. A year page that answers with
last year's content would pass a status check, so renaming the source now
would collect the 2025 nominees under a 2026 identity. The page states that
books published until 10 November 2026 are eligible for 2026; in 2025 the
opening round began on 11 November. The source stays at 2025 until the 2026
nominee page shows its own year.

Rolled over on 2026-11-13: see `docs/issue-996-goodreads-rollover.md`.
