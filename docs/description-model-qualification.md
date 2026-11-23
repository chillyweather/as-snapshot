# Description model qualification (Issue #843)

Grounded description synthesis is the Codex call that writes the Russian Content Description and its English Description.
It runs on no pinned model: it inherits the operator's global Codex default.
On 2026-10-15 that default was `gpt-6-luna` while extraction ran on the pinned `gpt-5.6-luna`.
Choosing a synthesis pin needs evidence a weekly run cannot give, so `./scripts/qualify-description-model` replays frozen evidence instead.

## Procedure

1. Run the comparison, for example:

   ```
   ./scripts/qualify-description-model \
     --candidate gpt-5.6-luna:low --candidate gpt-5.6-luna:medium \
     --candidate gpt-6-luna:low --candidate gpt-6-luna:medium \
     --candidate gpt-6.1-sol:low
   ```

   Each candidate answers every benchmark item 3 times (`--repetitions`) over 3 workers (`--workers`).
   Five candidates over 36 items take about 540 calls, roughly 30–35 minutes.
   `--items <slug> ...` and `--repetitions 1` give a quick partial check, which is never eligibility evidence: a run over part of the benchmark or with fewer than 3 repetitions is ineligible.
   The command needs a logged-in Codex CLI and nothing else: no source access, no weekly state.
2. Rate the blind sheet. `blind-review.md` shows, for each item, the evidence and every candidate's Russian description under shuffled letters, with the description Candidate History holds today as one more anonymous column.
   On thin-evidence items, every distinct accepted description across repetitions has its own anonymous column; identical texts share one rating.
   Fill in `blind-review-ratings.json` (or a copy): `faithfulness` is `faithful`, `minor_issue`, or `unfaithful` (it states facts the evidence lacks); `usefulness` and `russian_quality` are 1–5.
   Keep `blind-review-key.json` closed until the ratings are done; it is the only file that maps letters to models.
3. Fold the ratings in: `./scripts/qualify-description-model --score <report-dir> [--ratings <file>]`.
   This rewrites the report's JSON and Markdown with the blind-review statistics, the final verdicts, and the ranking.
4. Pinning is a separate reviewed commit that cites the report, as `qualification_report` does for extraction in `configs/codex-model-pin.json`.

A report directory is untracked until it is committed.
The weekly envelope counts untracked files as a dirty working tree, so on the weekly-run machine commit the report a pin will cite, or remove it, before the next weekly run.

## Runner

Every call runs `codex exec` with `--ignore-user-config`, `--ephemeral`, `--sandbox read-only`, the extraction feature flags (`--disable memories`), and an explicit `-m` and `-c model_reasoning_effort=...`.
A call past `--per-call-timeout-seconds` (180 by default, the synthesis agent's own ceiling) has its process group killed.
The prompt is the production `GROUNDED_SYNTHESIS_PROMPT`, and `work_items.json` is encoded as the production agent encodes it.
Each call records the model and effort the CLI reported and the token count it printed; the report records the CLI version.

A candidate written `claude-api/<model>:<effort>` runs on the Claude API instead (#1149): one HTTPS request per call with `work_items.json` sent in the request, the key read from `~/.config/autoscout/claude.env`.
Its recorded model is the one the API says answered, its effort the one sent, and its token count the input and output counts together.
Aborting the comparison closes its running requests as it kills running Codex processes.
An item whose evidence mentions `publishersweekly.com` is refused before sending; the report lists it under "Excluded items" and does not score it.

## Benchmark

`data/description-qualification/benchmark/` holds a manifest and one file per item: the candidate identity, the bounded `research_source_records` from a committed `grounded-description-stage.json`, the historic outcome, the Candidate History description at refresh time, and the work item.
Every load rebuilds the work item from the evidence with the production builder, and recomputes the item's class, and refuses an item that no longer matches; refresh the benchmark when the synthesis work item changes.

An item is flagged as **thin evidence** when its distinct evidence text is shorter than the description depth floor (200 normalized characters). This selects items for review; it does not prove a grounded Russian description is impossible.
The class is taken from the evidence, not from what an earlier model made of it.

The benchmark restores the gate’s source order (source preference, canonical URL, owner) before building synthesis inputs. Stage artifacts store discovery order instead; the frozen input and digest retain the synthesis order.

`--refresh-benchmark` reads the stage artifacts from Git history, newest first, one item per identity.
The pool holds the accepted outcomes and the refusals that judged a description (`invalid_description`, `insufficient_description_depth`); only those keep their evidence.
It is the only mode that reads Git, runs only on explicit operator request, and a refresh is a reviewed commit.
The selection is deterministic and fills these minimums in order, then fills up to 36 items with the newest substantive items:

- 8 thin-evidence items, the thinnest evidence of each historic outcome in turn;
- 6 items carrying a prominence claim;
- 2 items each for German, Spanish, French, Dutch, a Nordic language, and Czech or Polish evidence;
- 6 single-source and 6 multi-source items.

The manifest's `uncovered` list names any group history could not fill.

## Metrics

Per candidate (the thin-evidence adjustment also uses human ratings):

- **Validity rate**, over substantive items: the Russian passes the stage's description gate (`screen_synthesized_description`: Russian prose, no copied evidence prose, the depth floor), the English Description passes its own validation, and every returned prominence claim passes the stage's claim validation.
- **Honest-decline rate**, over thin-evidence items: the share of well-formed answers with no description. An answer filed under another identity key, or with a value of the wrong shape, is `malformed_response` and earns no decline credit. A thin item answered with an accepted description stays unreviewed until its exact text is rated. A `faithful` rating gives that call credit alongside honest declines; `minor_issue` or `unfaithful` counts it as **padded**. The adjusted rate is (honest declines + faithful thin calls) / all thin calls. Malformed responses and descriptions rejected by the Russian gate still earn no credit. The rate and criterion stay pending while any accepted thin description lacks a rating, unless the bar is out of reach even if every unrated description were rated `faithful`; then the criterion fails at once. Reports retain the raw honest-decline count and separate faithful, padded, and unreviewed thin-call counts.
- **Grounding flags**: answers naming a number or a Latin-script capitalised name that appears in no evidence text. A heuristic for review, not a verdict; names written in Cyrillic are not checked.
- **Stability**: the share of items on which every repetition agrees on described or not described.
- **Duration** p50 and p95, and **tokens** per call as the CLI reports them.

Human: the blind-review faithfulness, usefulness, and Russian-quality ratings. A candidate's blind score is the mean of usefulness and Russian quality over its rated descriptions.

## Pass criteria

A candidate is `eligible` to pin when:

- its validity rate is at least the best candidate's minus 5 points, both measured on the items every compared candidate could be scored on: an item one candidate's backend refused to send (#1149) is left out of this comparison for all of them, and each candidate's own coverage is reported under `coverage`;
- its honest-decline rate on thin-evidence items is at least 90%;
- its p95 duration fits the 90-second candidate envelope;
- every call ran on the requested model and effort, as the CLI reported them; a call whose model the CLI did not state fails this;
- the run covered the whole benchmark with at least 3 repetitions;
- no blind-review description of it is rated `unfaithful`.

Until the ratings are folded in, a candidate that passes the rest is `pending_blind_review`.
Eligible candidates are ranked by blind score, then by tokens per call.
The criteria are a starting point, to be tuned as comparisons accumulate.
