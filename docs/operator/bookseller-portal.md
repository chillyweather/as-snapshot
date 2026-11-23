# The booksellers' portal todostuslibros.com (#1140)

CEGAL (Confederación Española de Gremios y Asociaciones de Librerías), the owner of todostuslibros.com, gave written permission on 2026-11-19, answering the publisher's email the same day; the letter is kept in the editor's mailbox.
Its conditions, which the code enforces:

- Automated lookups start from a title's ISBN and go directly to the book page by its URL. The site search (`/busquedas`) is never used: the robots file disallows it and the permission does not name it.
- Only «Idioma original» and the page count are read and kept, with the page URL as their source. No description, cover, price, availability or other page text is kept, logged or passed to a model.
- Internal editorial analysis only: no publication, redistribution, transfer to third parties or sale, and no use for training AI models.
- A few dozen lookups a week, plus one first load of a few hundred titles.
- At most one request per second.
- CEGAL may withdraw the permission at any time in writing. No licence or paid service is needed.

A book page is `https://www.todostuslibros.com/libros/<slug>_<ISBN-13>`. The site does not check the slug, so the pass uses the fixed slug `libro`; a book the portal does not hold answers 404, which is kept as "no page".
The page's canonical link must name the ISBN asked, or the answer is not used.

In the original-edition pass (`docs/operator/pre-collection.md`), a book found on a Spanish list with an ISBN is looked up on the portal when no national library answered it with an original language: the BNE had no record, a record silent about translation, a record naming no language, or was unreachable.
A BNE answer with a language always wins and the portal is not asked.
A page whose «Idioma original» is a language other than Spanish and the book's list languages states the translation.
The page is then the book's translation evidence, and its language is the one portal fact the model receives (`original_language` in its item), so the model proposes only the original title.
When the model has no title, or Open Library does not confirm it, the book is stored as `library_stated` with the portal page as evidence; the review links it as «Todos tus libros».
«Idioma original: Castellano» proves nothing, because the portal labels some translations that way: it never makes a book a translation, and no answer ever says "not a translation".
The page count is stored as `page_count` with the page URL as `page_count_source_url` when the book has none.
No page, no «Idioma original», or a portal failure is no answer, and the book goes on to the model as before.
A failure is counted under `portal` in the pass result (`failed`, and `unavailable` after three failures in a row, when the portal is not asked again in that pass); it never stops the weekly run.

The volume limits:

- Each ISBN is asked once. Its answer, "no page" included, is kept and never asked again.
- Requests are at least 1.5 seconds apart.
- The weekly runs and `./scripts/complete-originals --apply` share a ceiling of 50 lookups over the last 7 days. A book past it is not asked (`ceiling_reached`) and goes on to the model as before.
- The first load is a separate manual command with a ceiling of 500 lookups in total, counted apart from the weekly ceiling: `./scripts/portal-first-load` previews how many ISBNs it would ask, and `./scripts/portal-first-load --apply [--limit N]` asks them. It takes the books without an editor decision that the pass would ask about, writes only the ledger and calls no model; the next original-edition pass that meets those books uses the kept answers without a request.
- A preview of `./scripts/complete-originals` (without `--apply`) asks the portal nothing and uses only kept answers.

The answers and the request counts are kept in one ledger, `todostuslibros-lookups.json` in the state directory (`runs/current-weekly-pilot-russian/`).
It lives there, and not in a Git-ignored `data/` folder, so that the ceilings hold wherever AutoScout runs: the weekly run commits and pushes it with the rest of the state, and a fresh checkout or a replaced server starts from the same counts and the same answers instead of an empty ledger.
Every request is written to the ledger before it is sent, under a file lock, so a crash, a failure or a second process never lets the count slip; weekly requests older than 7 days are dropped, first-load requests are kept.
An unreadable ledger stops every portal request (`ledger_unavailable`) instead of starting from zero.
A run that stops before its repository finalization leaves the ledger modified beside the run's other outputs (Candidate History, the snapshots, the review), which already block the next run's Git check; the ledger adds no block of its own, and the recovery that commits those commits it too.
The public snapshot leaves out the trimmed portal pages under `tests/fixtures/issue-1140-todostuslibros/` and their tests (`EXCLUDED_PATHS` in `autoscout/showcase_snapshot.py`), because CEGAL allows no publication; `runs/` is never in it.
After a manual `portal-first-load --apply` or `complete-originals --apply`, commit and push the changed state, as after any manual `--apply`, so the next weekly run's Git check finds a clean tree.

The weekly-config switch `todostuslibros_lookups` turns the portal on; it is off unless set to `true`, and it is on in both live configs.
If CEGAL withdraws the permission, set it to `false`: no pass and no first load asks the portal again, and the kept answers stay where they were recorded.
The portal module is `autoscout/bookseller_portal.py`; the first load is `autoscout/portal_first_load.py`.

## The home page list

The live configs also read the portal's home page as a book list: the source «TodosTusLibros Librerías Recomiendan No Ficción» (#290), a weekly rolling list of the nonfiction that booksellers recommend.
The written permission above does not name it.
On 2026-11-19 the editor asked CEGAL about it and the operator reported that CEGAL approved; no text of that answer is on file, so AutoScout holds itself to the conditions of the written permission wherever they can apply, and to these:

- Only the home page (`https://www.todostuslibros.com/`) is read for the list, at most once per scheduled run. No other list, category or search page is read, and no link is followed from it except to a book page under the lookup rules above.
- From the page AutoScout uses only what identifies the recommended books: title, author and ISBN, with the home page as the source. The captured page itself stays in the private run state as the evidence of that run, like every source's capture, and goes nowhere else.
- The page is passed to the extraction model only to read out those books. It is never used to train a model and never published or passed on; a public snapshot holds neither the page nor its fixtures.
- The same internal-use rule and the same right of CEGAL to withdraw apply. If CEGAL withdraws or objects, remove the source from both live configs.

Because the approval is not in writing, anything wider than this (another page of the site, more fields, a higher frequency) needs a written answer from CEGAL first, asked through the publisher. A written confirmation of this approval is worth asking for as well; when it arrives, replace this paragraph's conditions with CEGAL's own.
