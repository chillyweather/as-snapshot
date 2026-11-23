# Editorial Taste Profile entry rules

These rules extend Level 1 of the three-level policy in `AGENTS.md`; its two-phase protocol applies unchanged.

## Topic Hard Rule matching-term protocol (#281)

When the agent prepares a topic Hard Rule from editor conversation, it
MUST provide explicit `--match-term` values for safely derived topic
variants rather than relying only on the literal subject phrase.

**Deriving matching terms:** the agent may derive exact word-form
variants that appear in visible metadata fields (e.g. the English
inflection "queerness" and the Russian helper-description token
"квир" for the editor's subject "queer studies"). These variants
must be deterministic tokens — no stemming, translation, semantic
matching, model, or network calls.
A word ending in `*` is an explicit prefix that covers a Russian word's
inflected forms (`трансгендер*` matches «трансгендерной»; the stem needs
at least four letters). A term in double quotes (`"истори* кухн*"`)
matches only adjacent words in that order; without quotes, a term's
words may appear anywhere in the same field (#966).

**Exception terms (#967):** when the editor names what a topic rule must
not cover (e.g. food history for a cookbook rule), pass it as a
repeatable `--exception-term`. A candidate that matches an exception
stays outside the entry. The preview lists those candidates as
`kept_by_exception` rows, and the agent presents them beside the
matches.

**Preview:** the topic Hard Rule preview (return code 3, zero
mutation) MUST include the exact matching terms and evaluate them
against current candidates from `latest-scouting-candidates.json`.
The agent must present:
- The exact `--match-term` values.
- An unambiguous match count.
- Every matched candidate by title, author display, and full identity
  key.

**Cards already in Candidate History (#1025):** a Hard Rule of any scope
hides its matching cards from the whole cumulative review at the next
render, not only future candidates. The preview prints
`history_matches=N` and a `history_match=...` row per card. The
confirmation question must state that count, and how many of those cards
carry an editor decision.

**Confirmation:** the Russian confirmation question must include those
exact variants and the current candidate impact. The verbatim editor
quote (`--quote`) stays byte-for-byte/verbatim.

**If safe variants cannot be derived:** the agent asks exactly one
concise scope-clarification question in Russian and does NOT create the
rule. This is a confirmation boundary — no mutation occurs without
explicit editor approval.

## Editorial Taste Profile overlap detection (#282)

Before proposing an ``editorial-profile add``, the agent MUST check
active canonical entries for duplicates and probable overlaps (code is
stdlib-deterministic — no network, model, or translation service).

**Exact duplicates** (same normalised kind, scope, subject, and
behaviour): no new entry or effect is needed. The agent reports the
existing entry and does not allocate a new ID.

**Probable overlaps** (same kind/scope with shared meaningful content
token, or same subject with different behaviour): must be resolved
before any mutation. The agent lists **every** relevant existing
entry's ``id``, ``subject``, and verbatim ``quote``, presents the count,
and asks exactly one concise Russian question choosing among:
- **Keep existing** (``--overlap-resolution keep-existing``) — no change.
- **Replace existing** (``--overlap-resolution replace-existing
  --replace-id … --replacement-reason "…"``) — agent first previews
  (code 3, zero mutation), editor confirms, then atomic
  revoke+create with bidirectional audit links.
- **Add as distinct** (``--overlap-resolution add-distinct``) — agent
  first previews, editor confirms, then adds normally alongside
  existing entries.

**Replacement requires** exactly all current probable-overlap IDs
passed via repeatable ``--replace-id`` and a nonblank
``--replacement-reason``. Partial, stale, or wrong ID sets are
rejected with zero mutation.

**Cross-language bridging** (e.g. legacy Russian subject
``[editor note withheld]`` + new English subject with
explicit ``--match-term квир``) is detected through normalised
token-level overlap. Generic connector/domain words ("books",
"topic", "книги", "тематика", "автор", etc.) are excluded so that
``history books`` and ``science books`` do not falsely overlap.

**Russian confirmation question:** after listing every overlapping
entry (id, subject, verbatim quote), the agent asks **exactly one**
concise Russian question:

> «Найдено N пересекающихся правил: [перечислить id/тему/цитату].
> Оставить существующие (keep-existing), заменить перечисленные
> правила новым (replace-existing, нужно явно указать все ID и
> причину замены), или уточнить тему/область и добавить как
> отдельное правило (add-distinct — только если редактор
> подтвердит, что правила действительно существенно различаются)?»

For ``add-distinct`` the editor must confirm the material distinction;
when the distinction is a different scope the agent proposes the revised
``--scope`` and ``--subject``.
