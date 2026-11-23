# Issue 458 title evidence contract

The authoritative original work title remains in the `title` field.

The editor-facing helper remains in `english_title` and is paired with `english_title_type`.

The only title kinds are `original`, `sourced_published`, and `generated_rendering`.

A typed title may carry the optional `english_title_provenance` object.

The canonical provenance shape is `{ "kind": "...", "research_sources": [], "mapping": {} }`.

`research_sources` is an ordered bounded list of HTTP(S) URLs.

A `sourced_published` mapping contains `original_title`, `original_authors`, `published_title`, and `published_authors`.

A `generated_rendering` mapping may contain `original_title` and `original_authors`, but it has no research sources and is never identity evidence.

An `original` title has no publication evidence or mapping.

A title is identity evidence only when it is `sourced_published`, has valid provenance and at least one source URL, and its mapping matches the candidate original title and authors and the published helper title.

Published authors in the mapping must also conservatively match the candidate authors.

Identity checks use the existing normalized work-title and conservative normalized-author functions.

Untrusted collector and recorded input cannot self-assert `sourced_published`; canonical candidate projection preserves already valid sourced evidence.

The CSV keeps the existing `English title` column and adds a visible `(published title)`, `(rendering)`, `(original)`, or `(unverified)` suffix when applicable.

The CSV and HTML mark `english_author_display` as `(original)` or `(transliteration)` without changing its helper text.

The HTML detail panel shows the effective verified title kind and links valid sourced-title URLs only.

Existing Candidate History rows are not backfilled by this issue.
