# Issue #680 — evidence audit and rollover proposals

The second slice of #680.
`audit-annual-sources` can now read the pages each annual source declares, reach all six verdicts, and print a zero-mutation proposal.
The weekly run is unchanged: it still runs the offline audit.
Flags and output are in `docs/cli-reference.md`; applying a proposal is in `docs/source-onboarding-runbook.md` ("Annual rollover: applying one confirmed proposal").

## Rules

- A newer edition is looked for only once its release window has opened. Before that, the live check requests the configured page alone.
- A year-specific source with `next_edition_url` proves a published edition only when the next year's URL answers 200, stays on that URL, and shows `edition_marker` for that year. Goodreads's 2026 URL redirected to the 2025 page on 2026-11-08; that is `not-yet-released`, not a rollover.
- A stable source proves it on the configured page. A year-specific source without a URL pattern proves it on the landing page, and the proposed URL is left to be located there.
- A stage counts as published when its `stage_markers` text is exactly one visible heading, as `forbidden_visible_markers` are matched. Edition markers are matched in the visible text, the title included, as `expected_visible_markers` are.
- With no evidence, the verdict is exactly slice 1's.

## Replays of the real rollovers

The audit was given each source as configured before its change and the official page as captured (`tests/fixtures/issue-680-rollover-evidence/`, reduced to title and headings; provenance beside it).

| Change | Verdict | Proposal | What was done |
|---|---|---|---|
| #762 Baillie Gifford 2026 Longlist → Shortlist | `stage-review-required` | same URL, `… 2026 Shortlist`, `retire-old-and-add-new` | renamed in place; the frozen longlist benchmark case was removed |
| #826 National Book Awards 2025 → 2026 | `rollover-available` | `…/national-book-awards-2026/?cat=nonfiction`, `… 2026 Nonfiction`, stage `winner` kept, `retire-old-and-add-new` | renamed in place; the frozen 2025 case was removed |
| #1000 PW Best Books 2025 → 2026 | `rollover-available` | `…/pw/best-books/2026`, `PW Best Books 2026`, `retire-old-and-add-new` | same |
| #1000 Baillie Gifford Shortlist → Winner | `stage-review-required` | same URL, `… 2026 Winner`, `rename-in-place` | same |
| #1054 Goodreads Choice 2025 → 2026 | `rollover-available` | `…-books-2026`, `… 2026 Nonfiction`, stage `winner` kept, `retire-old-and-add-new` | same |
| Goodreads on 2026-11-08 | `not-yet-released` | none | not rolled over |
| #679 Deutscher Sachbuchpreis 2024 | `stable-url-new-edition` | same URL, edition 2026 | same URL; the name also lost its year |

The record-handling rule is the #1000 one: a source named by a frozen model-qualification benchmark case is retired and replaced, so the case stays valid.
#762 and #826 predate it and removed the case instead; the proposal names that as the operator's alternative.

## Metadata changes

- National Book Awards 2026 Nonfiction gained `edition_marker` `National Book Awards {year}`, from the page title of the real 2026 capture ("National Book Awards 2026 - National Book Foundation", 2026-10-15) and the live page.
- Deutscher Sachbuchpreis keeps no `edition_marker`. "Sachbuch des Jahres {year}" grounds the 2026 winner heading of the 2026-09-09 capture, but the live homepage of 2026-11-13 says "Gesucht: Sachbuch des Jahres 2027" in its call for entries, which would read as a published 2027 edition.
- `url_stability` on a source with `annual_edition` must now begin with `stable` or `year-specific`. All fifteen active annual sources already did.

## Live smoke, 2026-11-13

`./scripts/audit-annual-sources --today 2026-11-13 --live --propose --keep-evidence <scratch>`: 16 GETs (one per source, plus the Andrew Carnegie Medals landing page), exit 0, no proposal, every config file byte-identical afterwards.
Replaying the kept captures with `--evidence-dir` gives the same output.

| Source | Verdict | Reason |
|---|---|---|
| National Book Awards 2026 Nonfiction | current | configured-edition-confirmed |
| Goodreads Choice Awards 2026 Nonfiction | current | configured-edition-confirmed |
| Deutscher Sachbuchpreis | current | no-newer-edition-expected (no marker declared) |
| National Book Critics Circle Awards | current | configured-edition-confirmed ("2025 NBCC Award Winners") |
| PW Best Books 2026 | current | configured-edition-confirmed |
| Académie française Prix de l’essai | current | no-newer-edition-expected (no marker declared) |
| Baillie Gifford Prize 2026 Winner | current | configured-edition-confirmed |
| Premio Strega Saggistica 2026 Cinquina | current | no-newer-edition-expected (no marker declared) |
| Magnesia Litera 2026 — Naučná literatura a Publicistika | current | no-newer-edition-expected (no marker declared) |
| Cundill History Prize 2026 Longlist | current | configured-edition-confirmed |
| Andrew Carnegie Medals for Excellence - Nonfiction 2026 | not-yet-released | newer-edition-not-published: the 2027 window closed on 2026-10-31 and the landing page shows no "2027 Winners" |
| Women's Prize for Non-Fiction 2026 Shortlist | current | configured-edition-confirmed |
| Australian Book Industry Awards (ABIA) 2026 — Nonfiction | current | configured-edition-confirmed |
| Nagroda Złota Róża 2026 — nominacje | current | configured-edition-confirmed |
| Prêmio Jabuti — Não Ficção | current | configured-edition-confirmed |

Every declared edition marker was found on its live page.
The Andrew Carnegie Medals are on hiatus after the 2026 edition (ALA, 30 March 2026), so no 2027 edition is coming; whether to keep or retire the source is an operator decision, not a rollover.
