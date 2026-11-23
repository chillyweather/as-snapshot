# Issue #678: Structured feedback dimensions and candidate decision support

## What this adds

The #667 editorial audit showed that the single feedback status mixes
several different signals: editorial taste, rights or acquisition
availability, product/data defects, missing information, and the final
disposition. Treating those signals as one label risks teaching the wrong
preference. This issue turns that finding into structured feedback capture
and decision support, without changing any ranking behavior.

## Structured feedback dimensions

`record-feedback` now accepts four optional structured dimensions, recorded
alongside the existing status and verbatim note:

| Flag | Dimension | Allowed values |
|------|-----------|----------------|
| `--appeal` | Editorial appeal | `appealing`, `not_appealing` |
| `--rights` | Rights/acquisition availability (editor-reported) | `available`, `unavailable`, `unknown` |
| `--quality-problem` | Product or data-quality problem | `none`, `description_missing`, `wrong_category`, `bad_links`, `unusable_title`, `other` |
| `--info-needed` | Information still needed | `none`, `purchase_status`, `rights_status`, `russian_edition`, `description`, `other` |

The fifth dimension — the **final editorial disposition** — is the existing
editor status (`interested`, `maybe_watch`, `reject`, `already_known`,
`needs_deeper_analysis`). It is presented as a dimension in the preview
(`final_disposition=…`) but is stored exactly as before.

A dimension that was not discussed stays `unspecified`. When every
dimension is unspecified, no dimension data is written at all — existing
state shapes are unchanged. Values are accepted exactly as declared in the
table above — no normalization, translation, or inference; an unknown value
is rejected with exit code 1.

### Preview and confirmation

The two-phase protocol is unchanged:

1. **Preview** (no `--confirmed-by-editor`): prints the exact dimensions
   that would be recorded (`dimension_editorial_appeal=…`,
   `dimension_rights_availability=…`, `dimension_quality_problem=…`,
   `dimension_information_needed=…`, `final_disposition=…`) and returns
   code 3 with zero mutation.
2. **Confirm** (`--confirmed-by-editor`): records the dimensions atomically
   with the verbatim note in one pending-feedback write. Feedback
   finalization then applies them to Candidate History and the weekly
   snapshot as `feedback_dimensions`.

Dimensions are decision-only data: they finalize in place like editor
status and note, and never force a full pipeline rerun by themselves.

### Bare refusals stay unspecified

A short bare refusal — a note that is nothing but a refusal word — is
explicitly unspecified. The recognized forms are the bounded exact set
`Нет.`, `Нет`, `нет.`, `нет`, `No.`, `No`, `no.`, `no` (whitespace-tolerant;
«Нет.» is the canonical form observed in the #667 audit, 55 exact notes).
Recording any dimension beside a bare refusal is refused with
`bare_refusal_must_stay_unspecified` (exit code 1). A substantive note that
merely begins with a refusal word («Нет, но описание поправить.») is not a
bare refusal and may carry dimensions. No topic, treatment, or author
preference may be derived from a bare refusal, and no Editorial Taste
Profile entry is created from it by this feature.

## Candidate review card decision support

> **Superseded (2026-09-26, owner decision):** the editor-facing HTML review
> no longer shows the «Подсказка для решения» line, the Russian-edition
> column, or the Russian-edition verification rows. `decision_support_items`
> and the retained evidence below are unchanged; the Markdown review keeps
> its Russian-edition status and verification text. The rest of this
> section describes the original #678 behaviour.

The HTML review card carried a **«Подсказка для решения»** line
surfacing exactly the seven decision blockers named in the issue: method,
scope, genre/treatment, author relevance, usable translated title,
Russian-edition status, and rights/acquisition status. Values come from
retained candidate evidence (`narrative_classification`,
`candidate_category`, `english_title`, `russian_edition_status`), from
matching Editorial Taste Profile notes (author relevance), and from the
confirmed rights dimension. Every blocker without evidence is explicitly
marked «нет данных» — unknown values are never silently omitted. The
current candidate schema retains no geographical-scope field, so that
blocker renders as explicitly unknown.

The `quality_problem` and `information_needed` dimensions are recorded
feedback data consumed by future learning and evaluation; they are not
card rows, because the card contract is exactly the seven blockers above.

## How the dimensions may and may not be used

**May:**

- Separate positive-but-unavailable cases (appeal recorded, rights
  unavailable) from genuine negative taste, so future taste learning never
  has to infer one from the other.
- Route technical complaints (quality problem) to product follow-up instead
  of acquisition leads.
- Track recurring information needs (information needed) so future reviews
  answer them directly instead of inferring reasons from bare refusals.
- Feed future prospective ranking evaluations as clean, multi-dimensional
  labels — see the frozen-evaluation proposal in the #667 audit output.

**May not:**

- Create or modify an Editorial Taste Profile entry automatically. Profile
  learning still requires the existing two-phase preview-and-confirm
  protocol per entry.
- Change ranking weights, source allocation, or source activation. This
  issue deliberately changes no production ranking behavior.
- Down-rank or disable a source from rejection rate alone. Source
  evaluation stays multidimensional (novelty, corroboration, unique
  coverage, lifecycle status, sample size), as pinned by the #667 audit
  contract.
- Turn a presentation-specific rejection into a topic ban: a quality
  problem or appeal nuance recorded on one candidate never becomes a
  standing rule by itself.

## Later prospective evaluation (defined, not implemented)

No ranking weight, source-allocation, or source-activation behavior changes
in this issue. The evaluation that will eventually judge a ranking change is
defined now so future feedback is collected cleanly for it, following the
#667 audit recommendation:

- **Freeze before feedback.** Old and proposed rankings are both computed
  and stored before any new editor feedback for the evaluation window is
  recorded, so no ranking change can retroactively shape the labels it is
  judged on.
- **Group work identities.** All identities of one work (including
  translated-title aliases resolved through the guarded alias table) are
  judged together; a work is never split across ranking positions in the
  evaluation.
- **Judge on new books.** Evaluation runs on subsequent new books only, not
  on the historical population the proposal was derived from.
- **Use the dimensions as labels, not as rank inputs.** The structured
  dimensions supply clean multi-dimensional labels for that later
  evaluation; they are never ranking weights themselves.

Until that evaluation is explicitly authorized and run, production ranking
behavior is unchanged.

## Regression fixtures for the audit's complaint classes

`tests/test_issue_678_quality_class_regressions.py` reproduces the four
real complaint classes from the #667 audit against the current
implementation, citing the retained sanitized real-case evidence:

1. **Missing or inadequate description** — a candidate without a usable
   description renders the explicit fallback text, never a silent blank
   (Kindred-class note).
2. **Fiction leakage or wrong nonfiction category** — the eligibility gate
   excludes fiction genres, and authoritative nonfiction metadata overrides
   a stale retailer fiction label (Kindred-class note).
3. **Bad or irrelevant source links** — an evidence URL that cannot be
   verified against the fetched page is rejected with a reason, per ADR 0006
   (Kindred-class note).
4. **Untranslated or unusable titles** — an English helper title without
   valid provenance is displayed as explicitly unverified, never silently
   presented as a usable translation. Pinned to the retained real case
   «Ach, diese Lücke, diese entsetzliche Lücke» by Joachim Meyerhoff
   (editor note «Вот теперь появилось описание, но нет перевода названия…»,
   reject), committed as the sanitized fixture
   `tests/fixtures/issue-678-unusable-title-case.json` and cross-checked
   field-for-field against the example case published in the tracked
   `data/editorial-audit/editorial-audit.json` (audit identity, title, note,
   status, dates, and author display; the untyped English-helper payload is
   pinned as the retained Candidate History contract for the same identity);
   the regression always runs against that committed evidence (audit backlog class
   `untranslated_or_unusable_title`).

These tests pin the guarded current behavior; they do not declare the
historical defects repaired. Currency of each historical complaint still
requires a focused reproduction on a current run, as the #667 audit
backlog records.
