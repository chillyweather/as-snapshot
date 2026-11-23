# Issue #680 — annual edition metadata for the active annual sources

Backfilled on 2026-11-13.
Every active annual source in `configs/current-weekly-portfolio.json` now declares `url_stability`, `refresh_cadence` and an `annual_edition` object.
National Book Awards 2026 Nonfiction and National Book Critics Circle Awards also gained `url_stability` and `refresh_cadence`, which they lacked; no other existing field changed.

Sources of each value, in order: the repository (config notes, `docs/issue-*` evidence, retained captures and replay fixtures), then the award's own site, read with a few bounded requests on 2026-11-13.
A window resting on one announcement is widened, and the table says so.

## What the window means

`release_window` is the period in which the edition labelled Y first publishes the first stage in `stages`, in calendar year Y + `calendar_year_offset`.
The audit treats edition Y as possibly published once that window opens, and asks for a check of the landing page from then on.
`tracked_stage` is what the source is configured to show today.
Where one URL carries every stage of a cycle and needs no change between them (National Book Awards, Goodreads, Deutscher Sachbuchpreis, NBCC, Andrew Carnegie Medals), the tracked stage is the last one.

## Table

| Source | Edition | Tracked stage | Stages | Window | Landing URL | Basis |
|---|---|---|---|---|---|---|
| National Book Awards 2026 Nonfiction | 2026 | winner | longlist, finalists, winner | 09-01..09-30 | https://www.nationalbook.org/ | 2026 nonfiction longlist published September 2026 (the article prints the month only); finalists 6 October, winner 18 November (#826 and the longlist article). Window is the whole month. |
| Goodreads Choice Awards 2026 Nonfiction | 2026 | winner | nominees, finalists, winner | 11-01..11-30 | https://www.goodreads.com/choiceawards | Opening round began 10 November 2026 and 11 November 2025 (`docs/issue-996-goodreads-rollover.md`); widened to the month. Final round and winner use the same URL. |
| Deutscher Sachbuchpreis | 2026 | winner | nominees, winner | 04-01..04-30 | https://www.deutscher-sachbuchpreis.de/ | Nominees 21 April 2026 (#679); homepage schedules the 2027 nominees for 20 April 2027 (#761); widened to the month. |
| National Book Critics Circle Awards | 2025 | winner | longlist, finalists, winner | 12-01..12-31 | https://www.bookcritics.org/awards/ | The awards page shows the 2025 winners (replay fixture: "2025 NBCC Award Winners"). Publishing-year 2025 longlists were posted 16-18 December 2025, finalists 20 January 2026, winners 26 March 2026 (bookcritics.org). Edition label is the publishing year, so the offset is 0. |
| PW Best Books 2026 | 2026 | list | list | 10-15..11-15 | https://best-books.publishersweekly.com/ | The 2026 year page answered 404 on 2026-09-14 (#700) and was live on 2026-11-08 (#996); PW prints no date. Window spans the period between, conservatively. |
| Académie française Prix de l’essai | 2026 | winner | winner | 05-01..06-30 | https://www.academie-francaise.fr/prix-de-lessai | Page's newest laureate year is 2026; 2026 palmarès announced 18 June 2026; the official page cited in #289 says May or June each year. |
| Baillie Gifford Prize 2026 Winner | 2026 | winner | longlist, shortlist, winner | 09-01..09-30 | https://www.thebailliegiffordprize.co.uk/ | 2026 longlist 3 September 2026, shortlist 1 October, winner 4 November (#681, #762, #996). Markers from `expected_page_heading` and the section headings in the retained captures. |
| Premio Strega Saggistica 2026 Cinquina | 2026 | shortlist | shortlist, winner | 04-01..06-30 | https://premiostrega.it/PSS/ | 2026 cinquina announced 19 June 2026 (article date); the 2025 first edition's final was 20 June 2025, so its cinquina came earlier. No earlier stage is published. Widened from April. |
| Magnesia Litera 2026 — Naučná literatura a Publicistika | 2026 | longlist | longlist, shortlist, winner | 02-01..03-31 | https://www.magnesia-litera.cz/ | **Not confirmed officially.** The site prints no nomination date; nominated authors read on 25 and 31 March 2026 (news item of 27 March 2026) and the ceremony video dates from April 2026. Window covers February and March. The longlist ("Porota dále doporučuje") is published with the nominations. |
| Cundill History Prize 2026 Longlist | 2026 | longlist | longlist, shortlist, finalists, winner | 07-15..08-15 | https://www.cundillprize.com/ | Longlists published 28 July 2025 and 30 July 2026 at the same URL pattern, which grounds `next_edition_url`. |
| Andrew Carnegie Medals for Excellence - Nonfiction 2026 | 2026 | winner | longlist, finalists, winner | 10-01..10-31, offset -1 | https://www.ala.org/awards/books-media/andrew-carnegie-medals-excellence-fiction-and-nonfiction | 2026 longlist announced October 2025, finalists November 2025, winners January 2026 (ala.org news). ALA announced on 30 March 2026 that the medals are on hiatus after the 2026 edition. |
| Women's Prize for Non-Fiction 2026 Shortlist | 2026 | shortlist | longlist, shortlist, winner | 02-01..03-31 | https://womensprize.com/prizes/womens-prize-for-non-fiction/ | The 2026 longlist page prints no date (its images were uploaded in February 2026); shortlist 25 March, winner 11 June 2026. The 2025 and 2026 announcement slugs differ, so no `next_edition_url`. |
| Australian Book Industry Awards (ABIA) 2026 — Nonfiction | 2026 | shortlist | shortlist, winner | 03-15..04-30 | https://abiawards.com.au/ | Shortlist page dated 8 April 2026, winners 21 May 2026; `next_edition_url` is the 2027 URL #578 records. |
| Nagroda Złota Róża 2026 — nominacje | 2026 | nominees | nominees, winner | 09-01..09-30 | https://festiwalnauki.edu.pl/nagroda-zlota-roza | Nominations article dated 15 September 2026, winner 30 September 2026; `next_edition_url` is the 2027 URL #795 records. Landing page is the award rules page named in the source notes. |
| Prêmio Jabuti — Não Ficção | 2026 | semifinalists | semifinalists, finalists, winner | 09-01..10-15 | https://www.premiojabuti.com.br/ | 2026 semifinalists 29 September, finalists 14 October, ceremony 27 October 2026 (CBL announcement of the 68th edition calendar). The configured page prints ten rows per category, the semifinalist stage. Only one year checked; widened. |

`edition_marker`, `next_edition_url` and `stage_markers` are declared only where a past page or a recorded expectation establishes them.
None are declared for Deutscher Sachbuchpreis apart from its `Die Nominierten` heading, Académie française, Premio Strega Saggistica and Magnesia Litera.
National Book Awards declares no `stage_markers` (the WINNER heading is on the page before the winner is announced); its `edition_marker` was added with the rollover audit (`docs/issue-680-rollover-audit.md`).

## Audit on 2026-11-13

All fifteen sources are `current` except Andrew Carnegie Medals for Excellence - Nonfiction 2026, which is `unable-to-determine` with reason `newer-edition-expected-evidence-required`: by its window the 2027 longlist would have been published in October 2026.
ALA's hiatus announcement says no 2027 edition is planned, so this needs an operator decision on the source, not a rollover.
