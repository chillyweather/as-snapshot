# Issue #680 rollover evidence: provenance

Built from real retained captures by reducing each page to verbatim elements: the `<title>` element and
the named heading elements, in document order. No markup was written by hand. Each case's
`case.json` holds the source as configured before the change (`git show <commit_before>:configs/current-weekly-portfolio.json`),
with the `annual_edition` block, `url_stability` and `refresh_cadence` it would have carried, and the
benchmark manifest's source names at that commit.

| Case | Capture | sha256 of the full original body |
|---|---|---|
| `issue-762-baillie-longlist-to-shortlist` | `450bfcb:runs/current-weekly-pilot-russian/codex-capture-baillie-gifford-prize-2026-shortlist-a081bf19.json` | `e091c49e91d311db52ef4e8de5fa09c3fd1b59b986671d60f9275612f8e3ba87` |
| `issue-826-national-book-awards-2025-to-2026` | `77f8d1b:runs/current-weekly-pilot-russian/codex-capture-national-book-awards-2026-nonfiction-c5ad2a8c.json` | `aaa79f57f4c9583c2a216e2ac70b246eabe46e5a045dd0566dfc64d40705ebae` |
| `issue-1000-pw-best-books-2025-to-2026` | `002593c:runs/current-weekly-pilot-russian/codex-capture-pw-best-books-2026-2caabe53.json` | `59c07b6d8b4b69f2f532b3b2e098ae298126c999606edf1477538e9e5de471a9` |
| `issue-1000-baillie-shortlist-to-winner` | `working tree:tests/fixtures/issue-996-retained-capture/baillie-gifford-prize-2026-year-page-2026-11-08.json` | `c49677510606a021f58c224bbf5e0711b466f28f3a9a65bd8e0029d1156d7b87` |
| `issue-1054-goodreads-2025-to-2026` | `790609d:data/current-weekly-pilot/goodreads-choice-2026-nonfiction-page.json` | `36143385bec2a5a4fcc8b26856e7de39f0a250e23e9c15b4a531b451e4a440d1` |
| `issue-996-goodreads-2026-redirected` | `12336d3:runs/current-weekly-pilot-russian/codex-capture-goodreads-choice-awards-2025-nonfiction-e28fc02b.json` | `c00999a6bd714de8e912b42ae421d90d39bf28a36db6f2435fa2dcb08c94feb7` |
| `issue-679-deutscher-sachbuchpreis-2024-stable-url` | `ba8842e:runs/current-weekly-pilot-russian/codex-capture-deutscher-sachbuchpreis-2024-6f39e69f.json` | `41a8867e5707bae8b9c59e3a1d4b387cb48783a5f12fe583547bd320eb40b27f` |
| `pages.json: womens-prize-2026-shortlist-article` | `459cef9:runs/current-weekly-pilot-russian/codex-capture-women-s-prize-for-non-fiction-2026-shortlist-be769f82.json` | `c4ecd0cc37e2f216c794706d2ac8e45a011dbcc70cf4da7c5540b6d3fe6dfdd8` |
