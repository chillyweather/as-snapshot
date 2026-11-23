# Weekly source-processing runtime budget

The blessed weekly path plans extraction from the current worklist rather than from a fixed source count.
Capture has a **150-second base budget** plus **10 seconds for each page a source declares beyond its first** and **3 seconds for each detail page a source declares**.
One source is held to **10 seconds** for its first page, that same 10 seconds for every further page it declares, and 3 seconds for each detail page its own route declares, so a declared 100-page detail route is bounded by 310 seconds, a four-page ranking that also declares an identity route by 50, and a single-page source by 10.
Both bounds are derived from the configured portfolio before the run starts, so every declared page - a paging sequence, an identity route, an Amazon Charts landing page, a ranking detail route - is paid for in the budget instead of overrunning it.
The **180-second** value is a no-progress lease renewed only by recognized structured Codex execution events.
The **1800-second** value is both the absolute ceiling for one subprocess and the budget for one scheduling segment.
The weekly ceiling is one configured value of **7200 seconds**.
The workflow reserves **600 seconds** for finalization and recovery.
The extraction bound is derived as the weekly ceiling minus elapsed earlier stages minus that finalization reserve.
Segments are admitted only when the remaining extraction time holds the segment budget plus the required retry headroom.
A segment boundary is not a source failure.
A portfolio capacity stop is reported as unfinished work.

Configured capture timeouts are clamped to the 10-second production maximum, so configuration cannot silently expand the configured timeout.
One source's allowance is that clamped timeout, plus one further clamped timeout for each page the source declares beyond its first, plus the measured 3-second allowance for each detail page its route declares, and never more than the stage budget still left.
The allowance is a wall-clock deadline for the whole capture of one source, not only the timeout of each request on its own.
The deadline starts when the source starts and bounds its ranking pages, its identity page, and each declared detail page together.
The clamped timeout remains the bound on each single request and is shortened, never widened, to the time the source has left, so a hung connection is still cut at the socket level within one clamped timeout however much allowance the source still holds.
A socket timeout bounds one receive rather than a whole body, so each response body is also read in bounded slices with the deadline checked between them, and a body that keeps arriving is cut when the allowance runs out.
Each of those reads is narrowed to the time the source has left, so a slice that lands near the deadline cannot be followed by a read that waits out the timeout the request began with.
A body that only finishes arriving after the deadline is refused rather than published, so an overdue page never becomes current evidence.
A source that reaches its deadline stops issuing requests and fails closed with `capture per-source bound exhausted after Ns`.
It publishes no capture at all, because ADR 0011 requires a complete detail page for every surviving list row and a partial detail set is not evidence.
A source whose pages cost more than the measured allowance they earn therefore fails closed rather than spending the stage's remaining time.
The heartbeat reports the allowance each source is actually held to, so a source that declares more than one page shows the bound it earned rather than the per-request ceiling.
The stage deadline is still checked between sources.
Capture makes one attempt per source, plus at most one retry of that source (issue #1005).
A retry follows a request that timed out, either at its socket timeout or at its per-source bound (`capture per-source bound exhausted`), and it runs within the same stage budget: it is started only when a full first allowance is still left, its socket ceiling is the retry ceiling of **30 seconds** (never more than the stage budget left), and its allowance is clamped to that budget.
A source that is rate-limited (HTTP 429) with a parseable `Retry-After` waits that long, then retries once, but only when the wait ends before the stage deadline; without a `Retry-After`, or when the wait would outlast the budget, the source is skipped as `source_blocked` after its one request.
A retry that fails again is final.
Each source that was retry-eligible records `capture_attempts` in the capture-stage source runs: the `source-runs.json` written by `prepare-codex-extraction` and the `capture` stage detail of `workflow-log.json`. Each contains one entry per attempt with its status, reason, socket timeout, allowance, and any wait, and its `duration_seconds` is the total over attempts including the wait.
Sources are still retried in configuration order, and the progress heartbeat shows the retry as a second in-flight attempt on the same row.

Extraction permits at most two attempts per source.
An absolute execution safety stop is not retried.
A no-progress timeout gets one retry in a fresh Codex session, admitted through the same retry waves as any other retry (issue #824).
Production stalls hit sources that extract normally in neighbouring runs, and the run used to serve them from an earlier artifact with most of the extraction budget unused.
The extraction outcome records `no_progress_retry` and `no_progress_recovered`, or `no_progress_retry_refused` when the budget refused the retry, and `retry_admission.admitted_after_no_progress` names the admitted sources.
Attempts run in rounds, so every eligible source gets its first chance before retryable failures get another chance.
Deadline-aware agents use bounded parallel extraction with three workers by default.
Queued tasks do not invoke Codex after their segment admission deadline.
Retry backoff is bounded by the remaining extraction deadline.

Retries are admitted in waves (issue #732).
Each admitted retry is costed at the full per-attempt timeout, and a wave admits retries in submission order while their projected make-span fits the remaining time minus the retry headroom.
A retry that does not fit is deferred rather than refused, and is costed again against the time actually left once the admitted wave has finished.
Only a retry that no further wave can admit is refused with `retry_not_admitted_budget`.
The deadline guarantee is unchanged, because no wave is admitted past the deadline minus the headroom.
`retry_admission.admission_waves` records how many waves ran.
On 2026-09-26 the old single pass admitted 9 of 17 retries and refused the other 8, although the admitted retries took 30-77 seconds each and about 95 minutes of budget remained.

An empty extraction is not taken at its word (issue #740).
The contract asks the extractor to return every row that meets the required fields and to drop only the individual rows that break a rule.
It may return an empty result only when no row qualifies, and it must then state a top-level `empty_reason`.
Where code can count a capture's listing rows itself (BookMarks, and ranking tables that declare `ranking_row_selector`), an empty artifact from a capture holding rows is refused as `unexpected_empty_extraction` and retried, instead of validating as an empty page.
When an accepted empty artifact leaves a source without entries, its run record quotes the extractor's `empty_reason`.
The replay collector's authoritative-empty rule is unchanged: the refusal lives in the artifact validators, which is where it earns the retry.

An empty answer with no `empty_reason` is an unfinished answer, and the extractor is asked once to finish it (issue #751).
Replaying the frozen benchmark showed the extractor listing a capture's rows with its own tool calls and then answering `entries: []` with no reason; asked in the same session, it returned the rows.
The extraction agent therefore resumes that Codex session with one fixed follow-up, under the same read-only sandbox, disabled features, and pinned model.
The follow-up runs inside the attempt's own time bound and only when at least 60 seconds of it remain, so the retry and deadline accounting above is unchanged.
Its answer replaces the empty one only when it holds entries or states an `empty_reason`; a follow-up that fails, times out, or is empty again leaves the attempt exactly as it was, and the empty-extraction retry still applies.
The attempt diagnostic records the outcome as `empty_answer_followup`: `recovered`, `explained`, `unchanged`, or `not_run`.

Fresh extraction output is written to a **hidden unique staging path**
(`.<canonical-name>.<random-uuid>`) in the same directory and validated before
atomically replacing the canonical artifact via `os.replace`. An artifact
recorded as previously validated in the resume marker is preserved byte-for-byte
when fresh extraction times out or fails; preservation requires the canonical
file's SHA-256 digest to match the recorded validation evidence, and its stale
bytes are not counted toward current-run readiness. Legacy validation entries
without digests preserve their matching canonical artifact and are atomically
upgraded with the digest of those bytes; if marker persistence fails, the safe
fallback is still preservation rather than quarantine. Once digest evidence
exists, changed bytes are rejected. A fresh operation rotates the marker's
current run identity and stage completion while retaining prior evidence until
a replacement validates. Revalidating a fresh artifact replaces all older
evidence for the same source/canonical/capture identity with one current-run
entry, so repeated and weekly runs remain resumable without marker growth.
Validated artifacts from completed sources remain usable when
another source fails or exhausts its budget.

`workflow-log.json` stores a sanitized `source-processing` summary containing
the combined 1950-second budget, total measured duration, phase budgets and
durations, and per-source capture/extraction durations. These operational
details are intentionally absent from normal editor-facing Russian output.

## Pinned extraction model (issue #731)

Extraction runs on the model and reasoning effort pinned under `extraction` in `configs/codex-model-pin.json`.
Every extraction command and the Stage 0 exec probe pass the pin explicitly with `-m <model>` and `-c model_reasoning_effort="<effort>"`.
The operator's global `~/.codex/config.toml` default is never used for extraction.
Stage 0 reports it beside the pin as `global_default_model` and `global_default_matches_pin`, and a different global default is not a failure.

This replaces the issue #411 rule that AutoScout passes no model or reasoning override and runs on the operator's weekly selection.
On 2026-09-26 the global default changed to a new model one minute before the weekly run, and twelve healthy captures came back with no entries.
Extraction output depends on the model, so a model change is now a reviewed commit to the pin file.

Stage 0 fails closed before capture in three cases:

- `codex_model_pin_invalid`: the pin file is missing or malformed. No Codex request is spent.
- `codex_pinned_model_unavailable`: the CLI or API reports that the pinned model cannot be served, for example after it was retired. The message names the replacement procedure (issue #734).
- `codex_model_pin_not_applied`: the CLI header reports a model or effort other than the pin, so the flags did not take effect.

The `codex` stage detail records `pinned_model_profile`, which is what extraction asked for, beside `effective_model_profile`, which is what the CLI reported.

## One model client (issue #1018)

Every model call is built by `autoscout/model_client.py`: extraction and its empty-answer follow-up, description enrichment, description adjudication, the description-model qualification, original-edition completion and the Stage 0 exec probe.
The client always runs the model in the read-only sandbox; no caller can change that.
The top-level `backend` key of `configs/codex-model-pin.json` selects the backend that runs every pinned model, and a `backend` key inside the `extraction` or `description` entry overrides it for that entry alone (issue #1149).
`codex-cli` is the default when neither key is present; a value other than `codex-cli` or `claude-api` fails Stage 0 as `codex_model_pin_invalid`.

## Claude API backend (issue #1149)

`claude-api` calls the Claude Messages API over HTTPS with the standard library, so a stage can run on a model such as `claude-haiku-5-5` when the Codex subscription is spent.
No pin uses it yet: moving one is a reviewed commit that cites a PASS qualification report.
An entry on `claude-api` takes `reasoning_effort` from `low`, `medium`, `high`, `xhigh` and `max`, sent as the API's effort.

- **Key.** The key is read from `~/.config/autoscout/claude.env` (one line `ANTHROPIC_API_KEY=…`, mode `0600`) on the machine that runs the scan, and never from the environment. It goes into the request header and nowhere else: not a command line, a log, a diagnostic, a result or a state file. A missing file, a file without the key and a key the API rejects are typed failures that name the file.
- **Files.** The Codex CLI lets the model read the files the prompt names from its working directory (`capture.json`, `work_items.json`, `items.json`). The API backend sends their content in the request instead, and writes the answer to the same response file, so the stages read it as before.
- **Output schema.** A call with an output schema (original-edition completion) gets it as the API's structured output. A list of types becomes an `anyOf` of single types; a schema using anything else the API cannot express is refused before sending. Extraction and the description calls pass no schema on either backend; their callers validate the answer as before.
- **No session.** The empty-answer follow-up of extraction resends the first request and its answer, then the follow-up prompt.
- **No tools.** The model has no web search and no file access. A request with web search is refused before sending. Description adjudication searches the web, so it stays on the Codex CLI: on an `adjudication` entry naming a Codex model when the pin file has one, and otherwise on the `description` entry, which must then be on `codex-cli`. A `description` entry on another backend without an `adjudication` entry fails Stage 0 as `codex_model_pin_invalid`.
- **Publishers Weekly.** A request whose prompt, files or earlier exchange mention `publishersweekly.com` is refused before sending (`pw_text_not_permitted`): the PW permission names one other provider (`docs/agents/contracts/pw-precollection.md`). An extraction source is then reported by name as unavailable with that diagnostic, a grounded-description candidate as a synthesis failure (`synthesizer raised PwTextNotPermitted`), and the other sources and books go on.
- **Whole body.** Both refusals read every string of the finished request body, the output schema included.
- **Key in an answer.** An answer that contains the API key is discarded (`key_echoed`): it is not written, returned or resent.
- **Publishers Marketplace.** A request that carries a PM deal logline is refused before sending (`pm_text_not_permitted`): the PM permission names only the Codex description model and the DeepSeek fallback (`docs/agents/contracts/pm-precollection.md`). The PM logline stage then describes no book, reports each as failed with that reason, and does not start the DeepSeek fallback: the fallback follows only a Codex call that failed, never a call another backend made or refused. A logline that also links PW is reported with the PM reason. PM pages never reach a description request: the grounded-description classifier and the backfill work items already leave them out.
- **Deadline.** The caller's timeout bounds the whole request: the socket gets what is left of it before the connect, the upload, the headers and each chunk of the body, and a watchdog shuts the socket down at the deadline itself, so a server that keeps trickling bytes cannot hold the call past it. Each resolved address is tried with its socket registered before `connect()` and only inside what is left of the one deadline, and the TLS handshake is deferred until its socket is registered (verification stays on), so the watchdog and an abort can cut a stalled connect or handshake too; only the DNS lookup cannot be interrupted with the standard library, and it is bounded by the resolver's own timeouts. A passed deadline is a timeout and is not retried. A body that ends before its declared length is a dropped connection.
- **Failures.** The HTTP status maps to the failure kinds the stages already handle: 402 (credit exhausted), a 400 whose message says the credit balance is too low, and a 429 spend cap are a usage limit, as is a rate limit that lasts through its retries; 404 is an unavailable model; 401 and 403 are a rejected key. A 429 with `retry-after`, 500 and 529 and a dropped connection are retried inside the call's own timeout, at most twice per call in all, so one call makes at most three requests. An answer cut at `max_tokens` or declined by the model is not an answer.
- **Extraction bound.** A non-streaming request reports no progress, so the no-progress lease does not apply to it; the attempt's own timeout is its bound. An extraction answer may use up to 32,000 output tokens, thinking included, other calls 16,000.
- **Cost.** Every answered call records its input and output token counts. Each `workflow-log.json` entry carries the calls finished since the entry before it under `model_calls` (`purpose`, `model`, `input`, `output`, and the cache counts), so the cost of one run can be summed from the log. Codex calls report no counts.

Stage 0 checks a pin on `claude-api` before the Codex checks: one minimal request must answer.
A missing or rejected key fails it as `claude_api_key_unusable`, an unavailable model as `codex_pinned_model_unavailable`, a request the API or the client rejects outright (HTTP 400, an effort or schema it cannot take) as `claude_api_request_rejected`, naming the pin entry to fix; a timeout, rate limit or exhausted credit leaves it `claude_api_check_unknown`, as a Codex probe that ends that way does.
The Codex checks then run on the pins still on `codex-cli`, and on the pin adjudication runs on, resolved as the adapter resolves it: an `adjudication` entry is probed even while the `description` entry is on `codex-cli`. With every pin on `codex-cli` and no `adjudication` entry, a description pin that differs from the extraction pin is judged as before #1149: only a model that cannot be served fails it. In every other configuration each distinct Codex pin gets the full checks (exit code, reported model and effort, unavailable model), and each diagnostic names that pin's entry.
The extraction feature policy (`--disable memories`) is checked, and its flags passed to the Codex probe, only while extraction runs on `codex-cli`.

A new model is qualified before it is pinned (Issue #734), not tried on a weekly run.
`./scripts/qualify-model --model <name> [--backend claude-api]` replays the frozen benchmark in `data/model-qualification/benchmark` several times per source, through the production extraction agent, artifact validator, and collection validator.
It compares the candidate with the pinned model's stored report, or runs the pinned model too when there is none or when that report scored recall under another rule (`recall_scoring_version`).
It passes only when:

- no source that is reliable on the pinned model becomes unreliable;
- the empty-extraction rate is no higher;
- each source recalls at least 95% of its reference books, or stays within 5 points of the pinned model's recall on that source;
- the p95 durations fit half the weekly extraction budget.

Recall compares title and first author, with the author's name tokens in any order, so "Bildner, Jim" and "Jim Bildner" are one author (issue #751).
A reference row that names no author, because its page showed none, is found by its title alone; a row that names an author still needs that author.

A pin change sets `qualification_report` in `configs/codex-model-pin.json` to the PASS report it relies on.
On `claude-api`, a source whose capture mentions `publishersweekly.com` is excluded, not scored, and the report lists it under "Excluded sources", so the operator judges the PASS with it in mind.
The same holds for a baseline run on `claude-api`, and a stored baseline report that excluded a source is still reused, with that source left unscored. The empty-rate criterion then compares both models on the sources both scored; a stored report is reused only for the backend the pin names (a report without a backend ran on `codex-cli`).

## Bounded worker pool (issue #306)

Extraction parallelism uses a small bounded daemon-thread pool
(`_DaemonThreadPool`) instead of `concurrent.futures.ThreadPoolExecutor`.
`ThreadPoolExecutor`'s worker threads are non-daemon and registered for
interpreter-exit joining, so a hung worker blocks process exit even after
`shutdown(wait=False)`. The daemon pool guarantees that an abandoned hung
worker never blocks interpreter or pytest exit.

The pool is shut down with `wait=False` and `cancel_futures=True`. Queued
futures are marked as done with a `RuntimeError("cancelled")`; running workers
are abandoned (daemon). Workers exit when the input queue drains.

## Cancellation gate for safe late-worker publication (issue #306)

Each extraction attempt carries a per-invocation `_CancellationToken`.
Worker computation is **local**: `_run_attempt_local` reads from the shared
state dictionary but does NOT mutate it, and writes extraction output only to
a unique staging path. The collector thread calls `_apply_local_result`, which
is the **only** place where:

- `state["valid"]` / `state["retryable"]` / `state["duration"]` are updated;
- `staging_path.replace(canonical)` is called;
- diagnostics are accumulated.

When the collector times out waiting for a future, it cancels the token and
registers a **done callback** on the future.  The callback fires when the late
worker eventually completes and quarantines or removes its staging bytes
without touching canonical state, returned diagnostics, readiness counts,
timings, or resume evidence.

`_apply_local_result` for a cancelled-token result applies **no positive
readiness, canonical-artifact, or resume mutation** (negative status fields
like ``valid=False`` and cumulative duration are still updated so the source
correctly counts as skipped/failed with an accurate timing).  A late worker
that finishes after the token is cancelled cannot overwrite the canonical
artifact or mark the source as ready.

## Subprocess timeout and cleanup (issue #306)

Extraction uses `subprocess.Popen` with:

- **`start_new_session=True`** - the child owns its own process group / session
  so `os.killpg` on the child's pgid can never signal AutoScout's own process
  tree.
- **`stdin=subprocess.DEVNULL`** - the prompt text is the final CLI argument
  (Codex `exec … "…prompt…"`), never sent through stdin. This avoids
  descendants retaining pipe FDs.
- **Tempfile-backed stdout/stderr** - `mkstemp` files, not `PIPE`, so no
  `communicate` re-entry is needed to drain pipes after timeout. Stdout is
  discarded; stderr is read back after the process exits or is killed for
  existing secret-redaction-before-truncation.

After `process.wait(timeout=bounded_timeout)`, on `TimeoutExpired` the
`_terminate_process_tree` helper is called immediately and `communicate` is
never entered.

Process-tree termination (`_terminate_process_tree`):

- **Unix**: sends `SIGTERM` to the child's process group, waits up to 5
  seconds (`_SUBTERMINATE_GRACE_SECONDS`), then escalates to `SIGKILL` on
  the group and parent process, with a final bounded 5-second reaping window.
- **Windows**: calls `terminate()` then `wait(timeout=5)`; on timeout
  escalates to `kill()` then another bounded `wait(timeout=5)`.
- Fallback: every signal/wait step is individually guarded; the function
  never raises.

## Weekly workflow guard-release safety

Because worker collection is bounded by the remaining portfolio budget,
`fulfill()` always returns to `WeeklyWorkflow.run()`. The `finally` block
that releases the repository guard executes on every code path - even when
every extraction worker hangs. The daemon pool additionally guarantees that
the Python process itself can exit without being gated by stuck threads.

## Grounded description stage budget and retry boundary

The weekly grounded-description stage has an independent wall-clock budget of four seconds per eligible new or updated candidate, clamped to 900–2700 seconds, and does not borrow time from source capture or extraction.
Each admitted candidate has a two-part bound: up to 10 seconds for discovery, followed by a post-discovery 90-second candidate work envelope covering source fetching, challenge retries, and synthesis.
Stage admission reserves both the full bounded discovery phase and the complete post-discovery candidate envelope.
Each eligible candidate receives at most one discovery HTTP request.
Each candidate receives at most three source-fetch HTTP requests in total, including challenge retries across all discovered pages.
Every actual HTTP request checks the monotonic deadline for its current phase immediately before it starts.
Discovery uses the smaller of the 10-second inactivity timeout and the remaining 10-second discovery budget.
Source fetching and synthesis use the remaining post-discovery candidate envelope, capped by the stage deadline.
Host-rate-limit waits and challenge retries do not start after the relevant phase deadline expires.
Executor queue wait before a worker starts a candidate is outside that candidate's timing and envelope, but still consumes the overall stage wall-clock budget.
The explicit client-side vendor-rate-limit wait is also outside candidate timing and the candidate envelope, but still consumes the overall stage wall-clock budget.
A vendor queue refusal reports that the vendor queue could not fit before the stage deadline and that candidate work did not start.
The client/stage limiter keeps candidate timing accurate, while the loopback adapter applies an independent defense-in-depth floor for manual, concurrent, or other callers and may add a second wait when needed.
The stage uses no more than three workers.
A candidate that is not admitted because the remaining stage budget cannot hold both bounds remains waiting with reason `not_attempted_stage_budget`.
A discovery overrun is recorded as `candidate_budget_exhausted` whether the provider raises or returns normally.
A genuine discovery failure that occurs within its budget remains `search_or_fetch_failure`.
The separate small discovery cap prevents search or query latency and timeout stacking from starving source fetching, synthesis, and later candidates.
The cost is that unusually slow discovery is refused earlier and cannot use the full 90-second candidate work envelope.
This option is preferred because it protects later candidate admission and gives the stage a more useful bounded result, even though real discovery latency remains unpredictable within its cap.
A same-operation retry re-runs candidates that still have fallback descriptions or valid `codex-generated` descriptions, so a transient per-candidate failure can recover without changing the operation identity.
A same-operation attempt is upserted by exact operation ID, while an attempt from a later operation is appended as a new history entry.
Legacy attempts without an operation ID are not deduplicated by calendar date because date is not operation identity.
A whole-stage failure before bounded candidate results preserves existing description bytes, records a bounded stage attempt for a valid `codex-generated` description, leaves eligible candidates waiting, and records only the bounded stage reason `stage_execution_failure`.
The stage report classifies those candidate records as failed and separately counts candidates that still have fallback descriptions.
Each report record uses `result` for the applied, skipped, failed, or ambiguous classification and `final_state` for the description state.
The `still_fallback` count is a final-state count and can overlap the applied, skipped, failed, and ambiguous classification counts.
The description-stage `attention_required` field is driven only by current-run failed and ambiguous event sets, so a retained fallback backlog alone does not require attention.
Structurally invalid enrichment inputs return `input_failed` with a bounded reason code, while an explicitly disabled enrichment stage remains an ordinary skipped result.
Structural enrichment-input failure stops the workflow before post-enrichment refresh, review rendering, health validation, and repository finalization and requires diagnosis.
A grounded result stores the generated Russian description, applied date, and ordered evidence URLs in Candidate History, the current candidate report, and the validated stage outcome.
The weekly workflow keeps review rendering, health checks, and finalization after an ordinary bounded description-stage failure, and reports the result as attention required rather than claiming complete description coverage.
A description-results bundle publication failure that rolls back successfully returns `persistence_failed` with reason `description_persistence_failure` and stops the workflow with intervention required at failed stage `description-persistence` before review rendering.
A recovery failure returns `recovery_failed` with reason `description_persistence_recovery_failure`, retains recovery backups, and stops the workflow with intervention required at failed stage `description-recovery` before review rendering, health validation, or finalization.
The recovery-failure intervention recommends Dima diagnosis instead of repeating the weekly search.
