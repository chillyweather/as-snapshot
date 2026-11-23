# Issue #761: Deutscher Sachbuchpreis between editions — evidence

## What happened

The 2026-10-05 weekly run refused the source before extraction:

> page shape validation failed: expected page heading 'Die Nominierten' not found

The source reads the current edition from the stable homepage `https://www.deutscher-sachbuchpreis.de/` (#679).
The homepage no longer shows an edition.

## Cause

This is the annual rollover, not a broken page.
The 2026 award was given on 2026-06-08, and the call for the 2027 edition opened on 2026-09-21.
The homepage now presents the 2027 cycle and has no nominee list.

Headings on the homepage as served on 2026-10-05:

`Lesen. Denken. Reden.`, `Die Ausschreibung läuft.`, `2027`, `Rückblick 2026`, `Aktuelles`, `Newsletteranmeldung`, `Machen Sie mit!`

Key dates on the same page: jury announced 2027-01-12, nominees published 2027-04-20, award 2027-06-08.

The page is retained at `tests/fixtures/issue-761-retained-capture/deutscher-sachbuchpreis-homepage-2026-10-05.json`.

The 2026 winner and seven nominees were collected while the homepage still listed them; the last accepted capture is dated 2026-09-13.
Nothing new can be collected from this source until the 2027 nominees are published.

## Why the source is not retargeted

- #679 settled that the durable identity is the stable homepage, and the portfolio entry says not to retarget to year-specific archive URLs.
- The 2026 edition now lives at `/archiv/2026`, which answered HTTP 500 with a partial body on 2026-10-05.
- Pointing at the archive would add no book and would have to be undone in April.

## How the gap looks across a cycle

Archived copies of the homepage from the previous cycle show which heading survives the whole gap:

| Archived | Phase | `Lesen. Denken. Reden.` | `Die Ausschreibung läuft.` | `Die Nominierten` |
|---|---|---|---|---|
| 2025-10-15 | call open | yes | yes | no |
| 2025-12-15 | call closed | yes | no | no |
| 2026-01-25 | jury announced | yes | no | no |
| 2026-02-25 | jury announced | yes | no | no |
| 2026-04-23 | nominees published | no | no | yes |

The banner `Lesen. Denken. Reden.` is on the page for the whole gap and leaves it when the nominees arrive.
`Die Ausschreibung läuft.` covers only the first weeks.

## Decision

The source stays active and declares its between-editions state:

| Field | Value |
|---|---|
| `expected_page_heading` | `Die Nominierten` (unchanged) |
| `between_editions_page_heading` | `Lesen. Denken. Reden.` |
| `between_editions_reason` | the homepage presents the next edition (call for entries, jury, key dates) and lists no nominees yet; the nominees are published each spring |

A run that finds the banner and not `Die Nominierten` records the source as **skipped** with the reason `between editions: …`.
It requests no capture and no extraction.

The guard that #679 added is kept:

- `Die Nominierten` wins when both headings are on the page, so the source collects again on the first run after the nominees are published, with no configuration change.
- A page that shows neither heading still fails page-shape validation.
- The state is read from visible headings only; a hidden heading does not count.

## What this does not change

A skipped source is still named in the weekly caveats and still counts toward a degraded run, now with a true reason in place of a failure.
Removing a between-editions source from the caveats altogether needs the release-window design tracked in #680.
