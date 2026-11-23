# NuevosLibros nonfiction frontlists (#901, #902)

The explicit `nuevoslibros-frontlist` contract reads Spanish nonfiction
release calendars. It supports the stable ensayo, historia,
politica-y-sociedad, biografia-y-memorias and divulgacion genre routes.
Nothing dispatches by a URL without the explicit family declaration.

The existing generic linked-list probe sees the publisher directory as book
entries and cannot distinguish a card's genre and publisher links from its
book link. The card contract requires one matching genre body, one main,
a visible genre heading, at most 500 dated `article.book` cards, one
same-site `/libro/` title anchor with class `t`, and visible `/autor/`
links. Ambiguous cards, invalid dates, missing authors, duplicate book
URLs, and unrelated destinations refuse the page. Hidden cards and page
navigation contribute nothing. HTTP denial and bot-wall policy is unchanged.

This is a page contract, not a new collector. Live collection still uses
agent extraction (ADR 0006). Before model input, the validated cards are
compacted to title, all visible authors, book URL and the source's edition
release date. The full bounded original capture remains the grounding
record. Exceeding the model input budget refuses the source rather than
truncating its book list.

Recent window (#993). A weekly extraction covers only the cards dated within
the 14 days before the capture's `reference_date`, or later (forthcoming).
A month-precision date (`YYYY-MM`) counts by the last day of that month, so
a card dated 2026-10 is in the window for a reference date of 2026-11-07. The
window is applied to model input and to the completeness count only; the
validator still grounds every card against the full capture. A capture with
no readable `reference_date` is refused, never read as all cards.

Batches (#993). The Codex path sends the recent cards in consecutive batches
of at most 30 cards, one Codex call per batch, all inside the attempt's one
time bound. The batches' entries are merged into one artifact, de-duplicated
by evidence URL. Any failed batch (command failure, timeout, invalid JSON)
fails the whole attempt as it would a single call. A batch that holds cards
but returns no entries, even with an empty reason, fails the attempt as
`unexpected_empty_extraction` for this source by name, and no artifact is
written. A capture with no card in the window makes no model call. Its empty
result states that no card is dated within the last 14 days or later, and the
run reports that section by name as a source that contributed nothing: a genre
page without recent or forthcoming cards is abnormal, so it is not recorded as
a successful empty week. On the Codex path the source is marked failed with the
extractor's empty reason; on the Platform API route the collector reports that
the extraction returned no mentions.

On the opt-in Platform API route, the recent-window cards go to the model in batches sized
by `model_max_output_tokens`, so each answer fits its budget; one refused or
malformed batch fails the whole source. Mentions use the ordinary exact-list helper,
signal and category contract, plus agreement with one complete dated card.
A nonempty declared list cannot silently produce an empty extraction.

Replay retains at most three representative cards with their actual source
dates and coauthors. Source-specific tests cover the four configurations
and replay collection; generic exact-list rewrapping does not preserve this
family's deliberately explicit card shape.

Listing dates describe Spanish editions, including reissues. Listing
cards do not consistently state original titles or translation languages.
Blank sample fields are unknown facts, so high novelty against Candidate
History is an upper bound on new original works. The source needs probation
review for reissue/translation noise and extraction volume. No original
language is inferred from an author's name. The report records the first
200 cards; production may hold more cards, up to the declared safety bound.
