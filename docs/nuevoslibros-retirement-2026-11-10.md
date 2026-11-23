# NuevosLibros retired from the weekly selection (2026-11-10)

Editor request, passed on by the operator on 2026-11-10. The operator authorized the change.

## The editor's note

> NuevosLibros нужно убрать из автоматической подборки. За 8 ноября этот источник дал 233 из 279 карточек (83,5%). В выборку массово попадают обычные каталожные новинки, переиздания классики, узкоиспанские исследования и книги без содержательных описаний. На их просмотр уходит слишком много времени, а наличие книги в каталоге само по себе ничего не говорит о ее коммерческом потенциале.
>
> В отчете есть подтвержденные дубли. Например, The Age of Confiscation Николаса Малдера представлена дважды, под №950 и №951, из разных источников. Такие совпадения нужно объединять в одну карточку, сохраняя оба источника.

## Decision

All five NuevosLibros sources are retired:

- NuevosLibros — Ensayo
- NuevosLibros — Historia
- NuevosLibros — Política y sociedad
- NuevosLibros — Divulgación
- NuevosLibros — Biografía y memorias

The retirement is soft (`active: false`, `lifecycle_status: "retired"` in `configs/current-weekly-portfolio.json`) and reversible.
The source settings, replay fixtures, overlay entries and the `nuevoslibros-frontlist` collector are kept.
The collector tests still measure each source on a copy that is switched on.
To restore a source, set it back to `active: true` with the lifecycle status the operator chooses.

## Consequences

- Weekly runs report the five sources as inactive and make no capture or extraction for them.
- Books NuevosLibros already brought stay in Candidate History; this change does not remove them.
- Since #1025 the five sources carry `retired_cards_hidden: true`, so a card that only NuevosLibros reported is left out of the review.
  It returns when an active source reports the same book, or when the source is switched back on.
  A card the editor marked interested, maybe watch or needs deeper analysis stays in the review.
  The other retired sources do not carry the mark, and their cards stay.
- The duplicate cards in the second paragraph of the note are a separate defect, tracked in #1023.
