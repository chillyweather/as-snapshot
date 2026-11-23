# A ranking row may declare that it carries no author

Issue: #591.

## Context

`ranking-table` treated a row author as mandatory, so one author-less row rejected a whole page.

The Cúspide live probe rejected a 100-row list on 2026-08-24 because row 15 carried no author, recorded in `docs/issue-584-argentina-source-feasibility.md`.
Nothing in that evidence says the empty field was transient, and anonymous, collective, and institutional titles are ordinary in a bookshop, so waiting for the retailer to fill it is not a plan.

The editor decided, on 2026-08-24, to allow an author-optional row rather than wait.

Author was enforced in six places, which is why this is contract work rather than a one-line relaxation:
the feasibility row shape, the pre-detail fetch plan, the shared `SOURCE_MENTION_REQUIRED_FIELDS` contract that covers every family, the codex per-entry required-field check, ranking-row grounding, and the non-English helper-metadata contract that requires an author transliteration note.

## Decision

A `ranking-table` source may declare `ranking_author_optional: true`.
A source that does not declare it behaves exactly as before.

The declaration is narrow by construction.

It is a declaration, never an inference.
No author is ever guessed from a neighbouring row, from a search, or from a detail page.

The author selector stays mandatory.
`ranking_author_optional` without `ranking_author_selector` is a declaration error, because "no author" must mean the page was examined and carried none, not that nothing looked.

Only an absent author is excused.
A row that carries author text is grounded exactly as before, and a mention whose author does not match its row is still dropped.
An author-less mention grounds only against a row whose author is genuinely empty, and an invented author never grounds against an author-less row.
Nothing else about a row is excused: a missing title, a missing identifier, or a missing detail link still rejects the page.

The mention exemption is limited to a declaring ranking-table source.
`primary_authors` stays required for every other source, in both the shared mention contract and the codex per-entry check.

An author-less mention from a non-English source does not require `author_transliteration_note`.
There is no author to transliterate, and requiring the note would force an invented one.
A mention from the same source that does carry an author still requires it.

## Known consequence

`ScoutingCandidate.identity_key_for_mention` builds `title|authors`, so two author-less mentions with identical normalized titles share one identity key and merge.

That is accepted rather than fixed here.
A ranking-table mention always carries a grounded ISBN, so a real collision needs two different books with the same normalized title where both rows lack an author.
Changing the identity key to include an identifier would alter every candidate key in retained state, which is a migration and not a side effect of this change.

## Scope not covered

Two recovery paths validate captured entries without the source configuration in hand: `autoscout/observed_extraction.py` and `autoscout/codex_recovery.py` receive a work item that carries only source name and URL.
They keep requiring an author, which is the conservative outcome: an author-less entry is rejected there rather than accepted unverified.
If an author-optional source is ever recovered through those paths, they need the declaration threaded through first.

## Consequences

Existing ranking sources are unaffected and produce identical output.

The gate that rejected Cúspide is removed for a source that declares it, so #584 no longer needs the retailer to change its page.

A page whose author selector silently stops matching now looks the same as a page of legitimately author-less rows.
The bounded raw-row count and the per-row title and identifier requirements remain the guard against that, and a source declaring this should keep its row-count bound exact rather than ranged.
