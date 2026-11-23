# Candidate matching

AutoScout identifies a work using a normalized title and primary authors. The identity key combines those values. Work-title normalization removes the subtitle after the first ASCII or fullwidth colon, lowercases the title and replaces punctuation with spaces. Author display normalization handles supported honorifics, credentials and last-name-first forms. Author comparison also removes accents and splits contributor lists.

A durable Candidate ID survives changes to an identity key. The alias table maps keys to IDs; approved merges redirect retired IDs to the survivor without rewriting their stored aliases. An ISBN can prove identity. An ISBN marked discovery-only is a search hint, not identity evidence (#531).

## Reviewing possible duplicates

The offline `probable-duplicates` report (#855, #916) suggests pairs; it does not merge them. Records with the same normalized work title pair when authors share a surname or one author is missing. Same-title records with incompatible authors appear separately as author disagreements. Given-name relations are signals, not a pairing decision.

Different titles require a shared surname. A title can match a token prefix or suffix of at least three words. A valid sourced published English title can match the other record's title (`translated_title`); two records can share a valid sourced published English title (`shared_english_title`); or a valid confirmed original-edition title can match the other title (`original_title`). Translation and original-title matches are `possible_editions`; containment and same-title matches are `probable_duplicate`, unless different identity ISBNs make them `possible_editions`. Matching ISBNs add a signal. Multiple matching rules produce one pair with their combined signals. Dismissed pairs and IDs already redirected to the same survivor are omitted.

Review a proposed correction with `merge-candidate-identity --dry-run`, then obtain explicit editor confirmation before applying it. Conflicting decisions and descriptions require explicit resolution. Dismissal also requires preview and confirmation.

## Weekly review

The «Другое издание» line shows a linked historical record's decision and note. For probable duplicates it identifies another record of the same book. It is display only: it copies no decision and makes no merge.

Matching is deterministic: there is no fuzzy or semantic matching and no automatic merge. Machine-translated titles are never identity evidence.
