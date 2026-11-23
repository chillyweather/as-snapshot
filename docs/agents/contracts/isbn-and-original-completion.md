# ISBN and original-edition completion

## Blessed ISBN completion (Issue #979)

`./scripts/run-weekly` fills missing ISBNs of the week's candidates itself, after the pipeline, so a weekly run needs no separate step.
When the operator or the editor asks to fill ISBNs for books already collected, use `./scripts/complete-isbns` to preview and `./scripts/complete-isbns --apply` to write, then rerender the review.
Both calls read source and catalogue pages unless `--offline` is given, so in Codex the single tool-level sandbox-permission request is attached to each full `./scripts/complete-isbns` invocation, with this Russian rationale:

«Для поиска ISBN нужен сетевой доступ. Разрешить запуск?»

A learned ISBN never replaces an existing one and never merges two books; the procedure is in `docs/operator/pre-collection.md`.

## Blessed original-edition completion (Issue #968)

`./scripts/run-weekly` records the original edition of this run's translated books itself, after the description stages, so a weekly run needs no separate step.
When the operator or the editor asks to record original editions for books already collected, use `./scripts/complete-originals` to preview and `./scripts/complete-originals --apply` to write, then rerender the review.
Both calls read source pages, query the German, French and Spanish national libraries, call Codex and query Open Library, so in Codex the single tool-level sandbox-permission request is attached to each full `./scripts/complete-originals` invocation, with this Russian rationale:

«Для поиска оригинальных изданий нужен сетевой доступ. Разрешить запуск?»

With `--apply` it may also ask the booksellers' portal todostuslibros.com within its weekly ceiling; a preview asks it nothing.
An unconfirmed proposal is never stored as the original; the procedure is in `docs/operator/pre-collection.md`.

## Booksellers' portal first load (Issue #1140)

Only when the operator explicitly asks for the portal's first load, use `./scripts/portal-first-load` to preview and `./scripts/portal-first-load --apply` (optionally `--limit N`) to ask.
It asks todostuslibros.com about Spanish-list books already collected, at most 500 lookups in total under CEGAL's permission of 2026-11-19, writes only the portal ledger and calls no model.
The preview is offline and needs no permission. In Codex the single tool-level sandbox-permission request is attached to the full `./scripts/portal-first-load --apply` invocation, with this Russian rationale:

«Для первичной загрузки данных портала todostuslibros нужен сетевой доступ. Разрешить запуск?»

Exit 2 with `reason_code=portal_disabled` means the weekly config has switched the portal off; do not switch it on to get past it.
The permission, its conditions and the ceilings are in `docs/operator/bookseller-portal.md`.

## Blessed work-age completion (Issue #1138)

`./scripts/run-weekly` records the first publication year and edition count of this run's books itself, after original edition completion, so a weekly run needs no separate step.
When the operator or the editor asks to record the work age for books already collected, use `./scripts/complete-work-age` to preview and `./scripts/complete-work-age --apply` to write, then rerender the review.
Both calls query Open Library and call no model, so in Codex the single tool-level sandbox-permission request is attached to each full `./scripts/complete-work-age` invocation, with this Russian rationale:

«Для поиска года первой публикации нужен сетевой доступ. Разрешить запуск?»

A doubtful match is never stored as the work's age; the procedure is in `docs/operator/pre-collection.md`.

## Blessed Russian-edition completion (Issue #1181)

When the weekly config sets `russian_edition_completion: true`, `./scripts/run-weekly` looks for Russian editions of every undecided book itself, after work-age completion, so a weekly run needs no separate step; absent or false, it never does.
When the operator or the editor asks to look for Russian editions of books already collected, use `./scripts/complete-russian-editions` to preview and `./scripts/complete-russian-editions --apply` to write, then rerender the review.
Both calls query LiveLib, Wikidata, the Russian State Library and LitRes and call Codex, so in Codex the single tool-level sandbox-permission request is attached to each full `./scripts/complete-russian-editions` invocation, with this Russian rationale:

«Для поиска русских изданий нужен сетевой доступ. Разрешить запуск?»

The command refuses to write during a weekly run (exit 6); do not retry in a loop.
Never set the switch to get past a problem: the operator turns it on, and the steps, the limits and the config keys are in `docs/operator/pre-collection.md`.
No outcome of the weekly stage stops the run, and its source problems are operator diagnostics, never editor facts: relay them only when the operator asks.
