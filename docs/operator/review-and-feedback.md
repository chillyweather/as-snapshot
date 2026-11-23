# Review, feedback and the Editorial Taste Profile

Part of the [operator workflow](../current-weekly-operator-workflow.md).

## Review And Feedback Workflow

**Every conversational feedback action and Editorial Taste Profile entry requires confirm-before-record — no exceptions.** The CLI commands are two-phase by default:

### Mandatory two-phase protocol

**Phase 1 — Preview (zero mutation):**

Run the command **without `--confirmed-by-editor`**:

```bash
# Per-candidate feedback preview
python -m autoscout record-feedback \
  --identity-key "<full Candidate ID>" \
  --status <interested|maybe_watch|reject|already_known|needs_deeper_analysis> \
  --note "<verbatim editor note>" \
  --state-dir runs/current-weekly-pilot-russian
```

The preview prints: matched title, author display, full Candidate ID / identity key, intended canonical status, the editor note verbatim, and `confirmation_required=true`. Return code 3 means "preview shown, confirmation required." No state file is created or modified.

```bash
# Editorial Taste Profile entry preview
python -m autoscout editorial-profile add \
  --kind <hard-rule|soft-preference> \
  --scope <author|topic|publisher|source|source_family> \
  --subject "<subject>" \
  --quote "<verbatim editor words>" \
  --provenance "<conversation context>" \
  --state-dir runs/current-weekly-pilot-russian
```

A Hard Rule preview says it will hide matching future candidates (while remaining reversible). A Soft Preference preview explains its ranking effect. No profile entry is allocated or written before confirmation.

A Hard Rule also hides the matching cards already in Candidate History, at the next review render (#1025).
Its preview, and the `set-terms` preview of a Hard Rule, print `history_matches=N` and one `history_match=...` row per card, with the card's editor decision when it has one.
The agent states that count in the confirmation question, beside the current matches.
Revoking the rule returns the cards at the next render.

**Topic Hard Rule matching terms (#281):** when creating a topic Hard Rule,
the agent MUST provide explicit `--match-term` values for safely derived
topic variants rather than relying only on the literal subject phrase:

```bash
python -m autoscout editorial-profile add \
  --kind hard-rule \
  --scope topic \
  --subject "queer studies" \
  --quote "Категорически не хотим \"queer studies\" — скрываем." \
  --provenance "editor chat 2026-07-14" \
  --match-term queerness \
  --match-term квир \
  --state-dir runs/current-weekly-pilot-russian
```

The `--match-term` option is repeatable. Values are trimmed, empty-only
explicit input is rejected, and duplicates are removed stably preserving
first-supplied order (no sorting). The verbatim editor quote (`--quote`)
stays byte-for-byte/verbatim. `--match-term` is only valid for topic
Hard Rules; supplying it for any other kind/scope returns a code-2
error.

**Term forms (#966):** a word written with a trailing `*` matches every
word that starts with it, so `трансгендер*` covers «трансгендерной» and
«трансгендерные», and `лгбт*` covers «ЛГБТИ+». The stem before `*` needs
at least four letters, and `*` may only end a word; a malformed term
returns a code-2 `invalid_matching_term` error with its `reason`. Without
quotes, the words of a multi-word term may appear anywhere in the same
field. A term wrapped in double quotes, such as `"истори* кухн*"`, matches
only those words next to each other in that order.

**Exception terms (#967):** a repeatable `--exception-term` keeps a
candidate outside a topic entry when it matches, even if a matching term
matches too. A cookbook rule can hide recipe collections while keeping
food history visible:

```bash
python -m autoscout editorial-profile add \
  --kind hard-rule --scope topic \
  --subject "[editor note withheld]" \
  --quote "<verbatim editor words>" \
  --provenance "<conversation context>" \
  --match-term "рецепт*" --match-term "кулинарная книга" --match-term "cookbook*" \
  --exception-term '"истори* кухн*"' --exception-term '"food history"' \
  --state-dir runs/current-weekly-pilot-russian
```

Exception terms follow the same forms, are valid only on topic entries,
and must not repeat a matching term. The preview prints them as
`exception_terms=[...]` and lists every current candidate an exception
keeps as a `kept_by_exception_match=...` row after the `current_match=`
rows, with `kept_by_exception=N` as the count.

**Preview for topic Hard Rules:** the preview (return code 3, zero
mutation) prints the effective matching terms as a JSON array and
evaluates them against current candidates from
`latest-scouting-candidates.json`. Each matched candidate is printed as
a JSON object (`current_match=...`) with the stored identity key (or a
fallback derived key when absent). The agent must present:
- The exact effective matching terms.
- An unambiguous match count.
- Every matched candidate by title, author display, and full identity key.

If candidate state is absent, zero matches are reported cleanly.

**Russian confirmation** must include those exact variants and the
current candidate impact. If safe variants cannot be derived, the agent
asks exactly one concise scope-clarification question in Russian and
does NOT create the rule.

**Overlap detection (#282):** before proposing an ``editorial-profile add``,
the agent MUST check active canonical entries for duplicates and probable
overlaps. The code is stdlib-deterministic — no network, model, or
translation service.

- **Exact duplicate** (same normalised kind, scope, subject, and
  behaviour): code 0, no new entry or effect needed. The agent reports
  the existing entry plainly.
- **Probable overlap** (same kind/scope with shared meaningful content
  token, or same subject with different behaviour): code 3, zero
  mutation. The agent lists **every** relevant existing entry's
  ``id``, ``subject``, and verbatim ``quote``, presents the count, and
  asks exactly one concise Russian question:

  1. **Keep existing** — no change:
     ``--overlap-resolution keep-existing``
  2. **Replace existing** — agent previews (code 3), editor confirms,
     then atomic revoke+create with bidirectional audit links
     (``replaced_by`` / ``replaces``):
     ``--overlap-resolution replace-existing --replace-id taste-0001 --replacement-reason "…"``
  3. **Add as distinct** — agent previews (code 3), editor confirms,
     adds normally alongside existing:
     ``--overlap-resolution add-distinct``

**Replacement requires** exactly all current probable-overlap IDs passed
via repeatable ``--replace-id`` and a nonblank ``--replacement-reason``.
Partial, stale, or wrong ID sets are rejected with zero mutation.

**Cross-language bridging** (e.g. legacy Russian subject
``[editor note withheld]`` + new English subject with explicit
``--match-term квир``) is detected through normalised token-level overlap.
Generic words ("books", "topic", "книги", "тематика", "автор", etc.)
are excluded.

Passing ``--confirmed-by-editor`` alone without resolving probable
overlap returns code 3 — the byte-identical state is preserved.

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

**Phase 2 — Apply (after explicit editor confirmation only):**

Re-run the same command with `--confirmed-by-editor`:

```bash
python -m autoscout record-feedback \
  --identity-key "<full Candidate ID>" \
  --status <interested|maybe_watch|reject|already_known|needs_deeper_analysis> \
  --note "<verbatim editor note>" \
  --state-dir runs/current-weekly-pilot-russian \
  --confirmed-by-editor

python -m autoscout editorial-profile add \
  --kind hard-rule \
  --scope author \
  --subject "Some Author" \
  --quote "Never recommend this author." \
  --provenance "editor chat 2026-07-14" \
  --state-dir runs/current-weekly-pilot-russian \
  --confirmed-by-editor

# Topic hard rule with matching terms (after confirmation)
python -m autoscout editorial-profile add \
  --kind hard-rule \
  --scope topic \
  --subject "queer studies" \
  --quote "Категорически не хотим \"queer studies\" — скрываем." \
  --provenance "editor chat 2026-07-14" \
  --match-term queerness \
  --match-term квир \
  --state-dir runs/current-weekly-pilot-russian \
  --confirmed-by-editor

# Revoke a profile entry (preview first, then confirm)
python -m autoscout editorial-profile revoke \
  --id taste-0001 \
  --reason "Changed editorial direction." \
  --state-dir runs/current-weekly-pilot-russian
# Preview: code 3, confirmation_required=true, shows id/subject/verbatim_quote/reason

python -m autoscout editorial-profile revoke \
  --id taste-0001 \
  --reason "Changed editorial direction." \
  --state-dir runs/current-weekly-pilot-russian \
  --confirmed-by-editor
```

Preserve editor notes/quotes verbatim. Do not clean up, summarize, translate, or normalize editor wording.

### A returned review file in one batch (#1153)

When the editor returns a whole review file, resolve its rows with `resolve-review-rows`, write one decision per `resolved` row to a JSON file, and record them with one batch instead of one `record-feedback` call per book:

```bash
python -m autoscout record-feedback-batch \
  --decisions decisions.json \
  --state-dir runs/current-weekly-pilot-russian
# Preview: code 3, every row, status_count_<status>=N, batch_digest=<digest>

python -m autoscout record-feedback-batch \
  --decisions decisions.json \
  --state-dir runs/current-weekly-pilot-russian \
  --confirmed-by-editor --digest <digest>
```

```json
[
  {"candidate_id": "candidate-…", "status": "reject", "note": "<verbatim editor note>"},
  {"identity_key": "<identity key>", "status": "maybe_watch", "note": "", "rights": "available"}
]
```

Ask one confirmation question for the whole file: the row count, the count per status, and each row the preview flags with `warnings=`.
One unresolved or invalid row refuses the whole batch and stages nothing; a file changed after the preview is refused by its digest.
A confirmed batch that prints `staged_rows=N` above 0 (no `batch_refused=true`) and then exits 1 with `state_export=failed` on stderr has staged its rows; a refused call can print `state_export=failed` too and staged nothing. Do not blindly repeat the command. Run `./scripts/doctor` and follow its one State Store action (`docs/operator/doctor.md`, "State Store (#1076)"); that action may rebuild the database from the files (`./scripts/state-database rebuild --overwrite`) and drop the staged rows. Then run the preview and the confirmed command again: `error=batch_already_staged` means the rows survived and nothing more is needed; otherwise the batch is confirmed again as usual.
The full output is in `docs/cli-reference.md`.

### Resolution and preview rules

- Resolve the reference by row number, title, author, or Candidate ID from canonical Candidate History / latest-candidates state — never guess from a loose title.
- Read back the matched title, author, full Candidate ID / identity key, intended status, and note.
- If both a per-book decision AND a standing profile rule are inferred from the same editor statement, preview and confirm them as two separate effects in one question.
- Apply only the effects the editor explicitly confirmed.
- After confirmed mutations, rerender/finalize with `run-mvp` then `render-review`.

### Key constraints

- `ingest-feedback` is the explicitly edited-file fallback for CSV imports. The two-phase conversational confirmation protocol does NOT apply to `ingest-feedback`.
- No state file, Git, network, or Codex side effects before confirmation.
- Invalid statuses and unknown identities still fail normally during preview (return code 1 or 2).

Supported statuses: `interested`, `maybe_watch`, `reject`, `already_known`,
`needs_deeper_analysis`.

Pending feedback in `pending-returned-editor-rows.json` is staged, not yet
consumed. The subsequent `run-mvp` consumes feedback into Candidate History,
suppression, or `memo-queue.json` behavior.

---
