# BookInfo Weekly Nonfiction retired from the weekly selection (2026-11-12)

Editor request in her review notes of 2026-11-08, point 12. The operator decided on 2026-11-12 to retire the source.

## The editor's note

> Проверить BookInfo Weekly Nonfiction. Примеры указывают на появившиеся издания без сигнала продаж; №753 Трамп и №903 Камю. Его ценность для редакторского скаутинга сомнительна без рейтингов.

## What the data shows

BookInfo is a real sales ranking: the weekly top 20 of nonfiction sales in Brazil, onboarded on probation in #777.
The rank is on each card, in Portuguese, in the reason the book entered the list.

The source produced 36 cards between 2026-08-23 and 2026-11-07.
The editor rejected 29, marked 6 as already known and 1 as interested.
No card was reported by a second source.

The list mostly repeats international backlist in Portuguese translation, such as Carnegie, Frankl, Goleman, Camus and Machiavelli, and Brazil-specific titles.
The original edition was identified for 3 of the 36 cards, so a translated classic looked like a new Brazilian book.

## Decision

The source is retired (`active: false`, `lifecycle_status: "retired"` in `configs/current-weekly-portfolio.json`) and carries `retired_cards_hidden: true`.
The retirement is reversible.
The settings, replay fixtures, overlay entries and the `bookinfo-weekly-json` collector are kept, and the collector tests still run.
To restore the source, set it back to `active: true` with the lifecycle status the operator chooses.

## Consequences

- Weekly runs report the source as inactive and make no capture or extraction for it.
- Cards that only BookInfo reported leave the review at the next render. A card the editor marked interested, maybe watch or needs deeper analysis stays (#1025).
- Nothing is removed from Candidate History.
