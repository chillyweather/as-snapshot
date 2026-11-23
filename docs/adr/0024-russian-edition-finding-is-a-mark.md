---
status: accepted
---

# A Russian-edition finding marks a book and never hides it

Issue: #1181. Refines ADR 0003; keeps ADR 0015.

## Context

ADR 0003 excludes a book whose editorial Russian-edition status is `found in Russian` from the review.
The Russian-edition completion pass of #1181 finds Russian editions by the author's Cyrillic name and asks a model which listed Russian title, if any, is the same work.
That match is a model judgement behind fixed guards, and it is unproven: a wrong match that hid the book would remove a real opportunity without the editor ever seeing it.

## Decision

The pass records its finding as a learned field of its own, the Russian-edition Finding (`russian_edition`, with the status `confirmed`, `unconfirmed` or `no_find`).
It never writes the editorial status `russian_edition_status` or the Russian-edition Verification record of ADR 0015.

A book with a `confirmed` or `unconfirmed` finding stays in the review, in the same tabs, order and priority, and carries a mark in the HTML review, the Markdown review and the editor CSV: the Russian title, the publisher and year when known, and a link to the record that shows the edition.
The editor checks the evidence and decides with the usual statuses.
A `no_find` finding shows nothing, because absence from a catalogue is weak evidence.

ADR 0003 still applies to the editorial status `found in Russian`; the finding does not set that status and does not trigger the exclusion.

## Consequences

The review now shows books that have a Russian edition, and the mark is what tells the editor to skip them.
Hiding a book on a `confirmed` finding may be reconsidered after the editor has seen several weeks of marks and their accuracy.
