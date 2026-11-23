# CLI reference

The full command surface of AutoScout.
See the [README](../README.md) for what the tool does and how to run it.
Operational how-tos live in [current-weekly-operator-workflow.md](current-weekly-operator-workflow.md), and source onboarding follows [source-onboarding-runbook.md](source-onboarding-runbook.md).

## Available commands

Blessed launchers (resolve the project Python interpreter automatically; preferred entry points, also wired into the Codex agent contract in `AGENTS.md` and `docs/agents/contracts/`):

| Command | What it does |
|---------|--------------|
| `./scripts/help` | List every launcher and project slash command, grouped by task, with one line on what each does and touches. Offline and read-only. |
| `./scripts/run-weekly` | Run the full weekly scouting workflow against the live config (editor trigger: «Запусти еженедельный поиск книг»). Extra arguments go to `autoscout run-weekly`; the server's nightly timer passes `--scheduled` (see "Scheduled runs on the run server" below). |
| `./scripts/code-gate` | Say whether the checked-out code has passed `./scripts/check` on this machine, the gate a scheduled run applies to new code (#1124). Read-only; exit 0 when it passed, 1 when it has not. `--run` runs `./scripts/check` now and records a pass in the Git-ignored `data/code-gate/`. See "The gate on new code" below. |
| `./scripts/check-sources` | Quick source health check: probe every configured source live, capture-only, and print a per-source dead/alive verdict table (editor trigger: «Проверь источники»). Read-only; extra args pass through (`--replay`, `--output-dir`). |
| `./scripts/audit-annual-sources` | Annual-source edition audit (#680) against the weekly config. Offline by default (release windows only, the weekly run's check). Extra args pass through: `--live`, `--evidence-dir <dir>`, `--keep-evidence <dir>`, `--source "<name>"`, `--propose`, `--today YYYY-MM-DD`. Never changes configuration. See `audit-annual-sources` below. |
| `./scripts/collect-kirkus` | Run one Kirkus pre-collection pass with the site's 20-second delay and write the result the weekly run reads (Issue #816). Takes up to about half an hour. `./scripts/run-weekly` starts the same pass itself once a day, so this launcher is for a refresh outside a weekly run. Exits 0 when a result was written and 2 when the pass failed. The server's Kirkus timer passes `--scheduled`. |
| `./scripts/collect-pm-deals` | Run one read-only pass over the PM label of the Publishers Marketplace mailbox (Issue #943). It reads the new "Today's Deals" emails from the declared sender, parses their deals and writes the result, attempt record and watermark under the Git-ignored `data/pm-precollection/`. It opens the folder read-only and never sets a flag. The address and app password come from `AUTOSCOUT_PM_IMAP_USER` and `AUTOSCOUT_PM_IMAP_PASSWORD`, in the environment or in `~/.config/autoscout/pm-mailbox.env` (mode 0600). Prints counts only. Exits 0 when a result was written and 2 when the pass failed. `./scripts/run-weekly` starts the same pass itself on every run (Issue #946), so this launcher is for a refresh outside a weekly run; a second pass while one runs refuses with `pm_pass_in_progress`. |
| `./scripts/collect-pw-issue` | Run one PW Digital Library issue pass (Issue #954): read the `PW` label read-only, download each new issue's PDF with the subscriber link, parse the Nonfiction Reviews into bibliographic rows and write them under the Git-ignored `data/pw-precollection/`; the PDF is deleted afterwards. Needs the `[pw]` extra. Prints counts only; exits 0 when a result was written and 2 when the pass failed. `./scripts/run-weekly` starts the same pass itself on every run (Issue #956), so this launcher is for a refresh outside a weekly run; see "PW Digital Library issue pass" below. |
| `./scripts/probe-source` | Probe repeatable unconfigured Source Candidate URLs with bounded HEAD/GET evidence, a durable versioned ledger (JSON plus Markdown sidecar with counts), optional aligned `--proposing-context`, no weekly-state side effects, and one deterministic feasibility draft per accepted candidate under `data/source-feasibility-drafts`. The JSON is canonical; the Markdown sidecar renders entries losslessly (backslashes, pipes, newlines, and carriage returns are escaped deterministically) and is regenerated from the JSON whenever it is missing or stale, keeping the JSON untouched. Captures are discarded unless `--keep-captures` is explicit. |
| `./scripts/add-source` | Run `./scripts/add-source probe` to delegate the initial source probe, or run the launcher without `probe` for offline preview and confirmation. The probe and offline modes have separate exit contracts. |
| `./scripts/add-source discover` | Generate an offline, read-only source-gap report. It needs no URL and accepts optional `--signal-family <family>`. |
| `./scripts/add-source discover --probe` | Run the remaining discovery batch through the blessed probe launcher after the editor explicitly asks to probe and grants network authorization. |
| `./scripts/add-source monthly --operation-month YYYY-MM` | Publish the offline monthly nudge for the explicit month. It is an external or editor-controlled schedule target, has no installed or active scheduler, returns 0 for normal reports and 2 for input or artifact errors, and publishes only `data/monthly-source-discovery/reports/YYYY-MM/monthly-source-discovery.json`, `data/monthly-source-discovery/reports/YYYY-MM/monthly-source-discovery.md`, and `data/monthly-source-discovery/monthly-source-discovery-history.json`. See `docs/operator/sources.md`. |
| `./scripts/night-scout` | Start one unattended night of source scouting in Codex, the same procedure as `/scout-sources` in Claude Code (#896). With URLs (`<url> [...] [note]`) it onboards those sources. Runs Codex with live web search, full access and no approval prompts; `NIGHT_SCOUT_MODEL` and `NIGHT_SCOUT_EFFORT` override the model (`gpt-6.1-sol`) and effort (`high`). |
| `./scripts/source-novelty` | Offline and read-only: match the entries sampled from a candidate source page (`--sample`) against Candidate History (`--state-dir`) and print a novelty verdict (#896). See `source-novelty` below. |
| `./scripts/source-yield-report` | Offline and read-only: per source, how many of the books it reported the editor found useful, and which sources meet the proposed retirement rule (#1055). It retires nothing and changes no state. See `source-yield-report` below. |
| `./scripts/doctor` | Diagnose why the weekly run is blocked or stuck (editor trigger: «Проверь AutoScout»). Read-only by default: no network, model, Git, or file side effects. `--repair <name>` applies one allowlisted local repair; `--recover` previews an authorized recovery plan and needs `--confirmed-by-editor` to apply it. Extra args pass through. |
| `./scripts/qualify-model` | Qualify a model for extraction without a weekly run (Issue #734). `--model <name> [--effort medium] [--backend codex-cli|claude-api] [--repetitions 3] [--workers 3] [--source-name <name> ...] [--rerun-baseline]` replays the frozen benchmark in `data/model-qualification/benchmark` through the production extraction agent and validators, and writes `model-qualification.json` and `.md` under `data/model-qualification/reports/`. It never fetches source pages, never calls Git, and never changes the pin. On `--backend claude-api` (#1149) a source that mentions `publishersweekly.com` is excluded and listed in the report, not scored. Exit 0 PASS, 1 FAIL, 2 input error. `--refresh-benchmark` rebuilds the benchmark from committed weekly runs, and is the only mode that reads Git. |
| `./scripts/qualify-description-model` | Compare models for grounded description synthesis on frozen evidence (Issue #843). `--candidate [<backend>/]<model>:<effort>` (repeatable; `claude-api/claude-haiku-5-5:medium` runs on the Claude API, #1149, and an item that mentions `publishersweekly.com` is then excluded and listed, not scored) `[--repetitions 3] [--workers 3] [--items <slug> ...] [--per-call-timeout-seconds 180] [--seed <seed>]` replays the benchmark in `data/description-qualification/benchmark` through the production synthesis prompt and description gate, and writes `description-qualification.json` and `.md`, `blind-review.md`, `blind-review-key.json`, and `blind-review-ratings.json` under `data/description-qualification/reports/<timestamp>/`. `--score <report-dir> [--ratings <file>]` folds the blind-review ratings into the report. It never fetches source pages, never calls Git, and never changes a pin. Exit 0 when a candidate is eligible or awaits blind review, 1 when none is, 2 input error. `--refresh-benchmark` rebuilds the benchmark from committed stage artifacts, and is the only mode that reads Git. See `docs/description-model-qualification.md`. |
| `./scripts/editorial-fit-shadow` | Editorial-fit shadow scoring with the `fit-v2` Jev question set (Issue #725). `report` (default) prints the comparison of recorded shadow scores with the editor's later decisions; it is offline and writes nothing unless `--write` is given. `preview` prints the exact request for one candidate and sends nothing. `score` is live, separately billed TypeSafe usage: it needs `TYPESAFE_API_KEY` and scores the current editor-facing candidates. Every mode is advisory and changes no priority label, ordering, or weekly or editorial state. Exit 0 success, 2 input error or missing key. |
| `./scripts/export-decision-corpus` | Export the dated, frozen decision corpus for the editorial memory prototype (Issue #929, for #821). Reads current Candidate History and the alias table through the State Store and the earlier Candidate History versions from Git, and writes `decisions.jsonl` and `manifest.json` under the Git-ignored `data/decision-corpus/<commit date>-<commit>/`. Offline; never changes canonical state and refuses an `--output-dir` inside `runs/`, `configs/` or the state directory. `--ref <commit>` exports another commit's history (default `HEAD`). Exit 0 written, 2 refused. Record fields: `docs/editorial-memory/decision-corpus.md`. |
| `./scripts/ranking-benchmark` | Ranking benchmark (Issue #1104, for #895 and #1034). Compares each ordering (priority label, `fit-v2` fit only, `fit-v2` composite) with the current review order on the same books: the books the shadow report counts, split into development runs and held-out runs (2026-11-01 and later). Prints the within-run and pooled AUC, the paired differences with seeded bootstrap intervals, the positives moved down, and a `pass` or `fail` verdict per ordering by the rule fixed in #1104. Offline; prints the report and writes nothing. `--json` prints JSON; `--max-positives-moved-down-share <0..1>` adds a limit to the verdict. Exit 0 printed, 2 input error. |
| `./scripts/work-age-report` | Work-age report (Issue #1141). Measures, on the books the editor has decided, whether moving old, often-published works down would improve the review order. Reads Candidate History and the recorded `fit-v2` scores (run and backlog artifacts) and the `work_age` block of #1138; wanted is `interested`, `maybe_watch` or `needs_deeper_analysis`, rejected is `reject`, and `already_known` is counted apart. The age of the work is the year AutoScout first listed the book (`first_seen_date`) minus the year of first publication; today's date is never used. Writes one Markdown report with four parts: work-age coverage by list language, the share of wanted books by age and by edition count, a simulation of several age and edition-count thresholds on the top 40% by fit score, and the books the chosen threshold gets wrong. The threshold is chosen on a development half and checked on a held-out half, both fixed by a digest of the Candidate ID. Offline, no model call, changes no state; writes `data/work-age-report/work-age-report.md` (Git-ignored) and prints it. Exit 0 written, 2 input error. |
| `./scripts/publish-showcase` | Public showcase snapshot (Issue #930). Builds a sanitised copy of the repository from the working tree: an allow-list of paths, with only the example and demo files of `configs/` and none of the source research or feasibility reports (Issue #1192), test fixtures with editor-authored text or content captured from Publishers Weekly or Publishers Marketplace dropped with the tests that need them, quotes elsewhere replaced with `[editor note withheld]`, server addresses redacted, and a final scan that fails on anything private, including a withheld config, research file or licensed fixture. The copy is therefore not meant to run as it is. Runs gitleaks when installed, then syncs the copy into a clone of `chillyweather/as-snapshot`. Without `--push` it is a preview and pushes nothing, and its first line says how many test modules the copy leaves out; `--push` adds one commit with your own Git credentials. `--workdir <dir>` keeps the clone for inspection. Refuses to run with uncommitted changes to tracked files. The `Showcase snapshot` workflow runs the same command on every push to `main` once `SHOWCASE_PUBLISH=true` and the `SHOWCASE_DEPLOY_KEY` secret are set. Exit 0 success, 1 refused or leak found. |

CLI subcommands (`python -m autoscout <command> --help` for arguments):

| Command | What it does |
|---------|--------------|
| `run-weekly` | Full weekly workflow with Russian editor messaging (what `scripts/run-weekly` execs). |
| `run-mvp` | Deterministic local pipeline run from a config (fixtures or recorded data). |
| `check-sources` | Capture-only source health probe (what `scripts/check-sources` execs). |
| `collect-kirkus` | One Kirkus pre-collection pass for the config's Kirkus source (what `scripts/collect-kirkus` execs). |
| `collect-pm-deals` | One read-only PM deals mailbox pass for the declared PM source (what `scripts/collect-pm-deals` execs). |
| `probe-source-candidate` | Probe unconfigured Source Candidates in one live batch (what `scripts/probe-source` execs). Accepts repeatable `--url`, `--name`, `--signal-family`, and `--proposing-context`, plus `--keep-captures`, `--ledger`, `--feasibility-dir`, and explicit `--re-probe`. Existing ledger entries use the no-network `already_evaluated` latch unless `--re-probe` is supplied. Legacy batch probe exit codes are 0 for any accept, 1 for reject or malformed candidates without an accept, 2 for command, input, or storage errors, and 3 when all candidates defer. |
| `discover-source-gaps` | Generate the deterministic source-gap report from the configured portfolio, Search Optimization Review, and optional Source Candidate Ledger. It is offline unless explicit `--probe` is supplied, and returns structured JSON on stdout. |
| `monthly-source-discovery` | Publish one deterministic offline monthly report pair and derived history for required `--operation-month YYYY-MM`. It cannot probe or onboard sources. Valid same-month retries safely replace changed semantic evidence once, preserve unchanged bytes, and keep one history entry. |
| `capture-source-fixtures` | Offline replay-fixture generator. It requires a retained helper-metadata sidecar matching every representative title, author, and book URL; it never translates or fabricates helper values. |
| `scaffold-source` | Offline, confirmation-ready source onboarding scaffold. It verifies an accepted Source Proposal, retained evidence, the retained helper-metadata sidecar, and the exact replay fixture pair, then publishes four explicitly supplied config files as one bundle. |
| `add-source` | Confirmation-gated offline onboarding from retained accepted probe evidence that validates replay and contracts before one exact local commit without pushing. It accepts one scalar `--url`; accepted previews require an explicit `--run-date`; the commit excludes probe evidence, captures, ledgers, and feasibility drafts. A repeatable `--onboarding-artifact` declares a dirty file under `tests/`, such as a portfolio-composition pin, that is validated and committed with the bundle. Offline exit codes are 0 for success, 1 for correctable or other errors including repeated `--url`, 3 when confirmation is required, 4 for retained-evidence defer, and 5 for retained-evidence reject. |
| `qualify-model` | Replay the frozen extraction benchmark on a candidate model and report PASS/FAIL against the pinned model (what `scripts/qualify-model` execs). |
| `qualify-description-model` | Replay the frozen synthesis benchmark on candidate models and report per-candidate metrics, eligibility, and a blind-review sheet (what `scripts/qualify-description-model` execs). |
| `editorial-fit-shadow` | `score --run-date <date> --adapter live\|replay [--replay-file <file>]`, `report [--write]`, or `preview`, each with `--output-dir` and `--state-dir` (what `scripts/editorial-fit-shadow` execs). The adapter is always named explicitly; `replay` serves recorded responses and never reaches the network. |
| `run-health` | Summarize an existing run's readiness and source health from its artifacts (no network). With `--state-dir <dir>` (and `--config` for the backend) it also checks, read-only, that the State Store's database and its JSON export agree and prints `state_backend=`, `state_export=` and, for a mismatch, `state_export_command=`; a mismatch exits `1` (#1076). When the run's Russian-edition stage ran it prints `russian_edition_completion=<status>`, with `russian_edition_completion_problem=<cause>` for a failed stage and `russian_edition_source_problem=<source>|blocked=N|failed=N|not_asked=N` for a source that blocked or failed 3 times or more; they never change the exit code (#1181). |
| `audit-annual-sources` | Read-only: for each active annual source, judge its declared `annual_edition` on `--today YYYY-MM-DD` (default today) and print one line with the verdict, reason, edition year, tracked stage, release window, URL, landing URL and detail. The verdicts are #680's six. Offline (default) only the release window speaks, so only `current` and `unable-to-determine` occur (reasons `no-newer-edition-expected`, `newer-edition-expected-evidence-required`, `edition-year-ahead`, `annual-metadata-missing`, `annual-metadata-invalid`). `--live` fetches the pages the source declares with the collector's bounded GET (same User-Agent, size limit and timeout): the configured `url`, and, only once the next edition's release window has opened, either `next_edition_url` for the next year or `landing_url`; at most 2 GETs per source, sequential, no retry, no HEAD, no link following. `--evidence-dir DIR` replays JSON captures instead (no network); `--keep-evidence DIR` (with `--live`) writes the captures there and is refused inside `configs/`, `runs/` or any weekly/state directory. With evidence, `rollover-available`, `stable-url-new-edition`, `not-yet-released` and `stage-review-required` occur too, plus the reasons `configured-edition-confirmed`, `configured-edition-not-confirmed`, `newer-edition-published`, `newer-edition-not-published`, `newer-stage-published`, `edition-marker-undeclared`, `evidence-unavailable` and `newer-edition-marker-before-window`; one `evidence source=…` line per fetched page gives role, requested and resolved URL, status, markers checked and found, body sha256 and fetch time. A 200 that redirected or lacks the declared `edition_marker` never counts as a published edition. `--source "<name>"` audits one source. `--propose` adds one `proposal_begin … proposal_end` block per source whose evidence shows a newer edition or stage: kind (`edition-rollover`, `stable-url-edition-refresh`, `stage-change`), old and proposed name, URL, edition year and stage, record handling (`rename-in-place`, `retire-old-and-add-new`, `update-in-place`), the evidence, the validation work this repository wires to the source, `proposal.mutation=none` and `proposal.confirmation_required=true`. Applying a proposal follows `docs/source-onboarding-runbook.md`. Exit `0` nothing to check, `1` at least one source needs the operator (every verdict but `current` and `not-yet-released`), `2` config or usage error, including a malformed `annual_edition`. |
| `doctor` | Deterministic read-only diagnosis of the current weekly state (what `scripts/doctor` execs), with allowlisted local repairs (`--repair`) and confirmation-gated recovery (`--recover`). |
| `prepare-codex-extraction` | Fetch configured source pages (`--live`) and produce a Codex extraction worklist. |
| `finalize-codex-run` | Generate a replay config from a completed Codex extraction run. |
| `render-review` | Render the feedback-ready local weekly review from run artifacts. Every render leaves out the Candidate History cards an active Hard Rule matches, the cards whose every source is a Withdrawn Source, and the undecided cards of a book the editor already marked already known or rejected in another edition, and lists them in `hidden-from-review.json` (#1025). `--config` names the weekly configuration whose retired sources apply; it defaults to `configs/current-weekly-pilot-live.json` when that file exists, and otherwise no source is withdrawn. The weekly run, the doctor re-render, `generate-descriptions` and the PW history backfill render with their own configuration. A configuration that cannot be read fails the render (`error=review_config_invalid`). |
| `complete-isbns` | Fill missing Candidate History ISBNs (#979) from the source's own book link, the captured list page, the book's own page, then an Open Library edition in the source language whose title and author surname agree (and whose year, when the book's publication year is known). `--scope undecided` (default) takes every book without an editor decision, `--scope current` this run's candidates; `--offline` uses only the first two methods. Without `--apply` it writes nothing and exits 3. A learned ISBN is fill-only and discovery-only (#531) with `isbn_provenance`; a network miss records `isbn_lookup_date` and is not asked again for 28 days, while an unreachable or malformed catalogue answer records nothing. It refuses to write (exit 6) during a weekly run or while another write holds the report directory's lock, and writes Candidate History merged fill-only into the file as it is then. Launcher: `./scripts/complete-isbns`. |
| `complete-originals` | Record the grounded original edition of a book found only on non-English lists (#968). It reads the first three evidence pages already used for the book's description and looks for a translator credit, which is extra evidence only. Every selected book, with or without a credit, goes to a Codex CLI call that proposes the original title and its ISO 639-1 language, in bounded batches of 20, and Open Library must confirm it. A confirmed original in another language proves the translation (`is_translation: yes`), with the credit page or else the Open Library page as evidence; a book with no credit and no confirmed original stays `is_translation: unknown`. Only a confirmed original is stored as `original_edition`; an unconfirmed proposal is stored as `not_found`, and a failed proposal call or an unreachable catalogue leaves the record untouched for the next run. A confirmed English original also sets `english_title`/`english_title_type`/`english_title_provenance` as a sourced published title through the title-evidence guard, never replacing an existing one. A record whose original is confirmed, or whose lookup is younger than 28 days, is skipped. The stage spends at most 15 minutes and writes `original-edition-completion.json`. `--scope undecided` (default) takes every book without an editor decision, `--scope current` this run's candidates. Without `--apply` it writes nothing and exits 3. It refuses to write (exit 6) during a weekly run or while another write holds the report directory's lock, and writes Candidate History merged fill-only into the file as it is then, then the current and latest candidate snapshots. `--config` names the weekly config whose `todostuslibros_lookups` switch lets a Spanish-list book with an ISBN be looked up on todostuslibros.com (#1140) when no library gave its original language; with `--apply` the lookups count against the portal's weekly ceiling and are kept in `todostuslibros-lookups.json`, and a preview uses only kept answers. The output adds `answered_by_portal`, `portal_stated` and a `portal` line of request counts. Launcher: `./scripts/complete-originals` (passes the live config). |
| `portal-first-load` | The one first load of the booksellers' portal todostuslibros.com (#1140): asks about every book without an editor decision, found only on non-English lists, one of them Spanish, with an ISBN the portal ledger does not hold, within the first-load ceiling of 500 lookups in total, 1.5 seconds apart. `--config` (required) must switch `todostuslibros_lookups` on, else exit 2 with `reason_code=portal_disabled`. `--limit N` asks at most N ISBNs in this call. Without `--apply` it asks and writes nothing and exits 3; with it, it writes only `todostuslibros-lookups.json` in the state directory, calls no model, refuses during a weekly run (exit 6) and prints counts only. Launcher: `./scripts/portal-first-load`. |
| `complete-russian-editions` | Record whether a Russian-language edition of each undecided book exists (#1181), as learned fields of their own: `russian_edition` (`status` `confirmed`/`unconfirmed`/`no_find`), `russian_edition_lookup_date`, `russian_author_name`, `russian_edition_attempt_date` and `russian_edition_livelib_search_date`; it never reads or writes `russian_edition_status` or the Russian-edition Verification record. LiveLib is searched by the book's original (else English) title without subtitle or author and only locates: its book pages are never fetched. The Cyrillic author name prefers the published spelling: the kept LiveLib result, else the stored name when it corresponds to the book's author (one that does not is ignored and replaced), else the Russian label of one Wikidata search (asked only when the first two give nothing); a LiveLib spelling is stored as `russian_author_name` only when the book ends `confirmed` or `unconfirmed` with it, a Wikidata one whenever it is learned; the Russian State Library (`author:(Surname Given)`, and also `author:(Surname Given) AND title:(<Russian title>)` when LiveLib located one; both must answer for the State Library to count as answered) and LitRes are then asked by that name, and once more with a second known spelling when neither lists anything; an edition is kept only when a credited author (the State Library's whole author, given name included) corresponds both to that name and to the book's own author. Names correspond when their surnames do and, when both carry a given name, their given names do too: across scripts on the same consonant skeleton (Джон is John, never Robert), between two Cyrillic names by their first letter; a LiveLib result is kept only when its author corresponds to the book's own author. A model answers, per book in batches of 10, which numbered Russian title is the same work, and fixed guards decide: only a State Library or LitRes edition in Russian (a LitRes edition in Russian may have a Latin title; a State Library record must state Russian or no language, and have a Cyrillic title), by the same author, not dated before the original and not a summary, retelling or abridgement (checked on the State Library title with its bracketed notes) is `confirmed` (`high` when both catalogues show it or the State Library record states a translation, else `medium`); a LiveLib result alone is `unconfirmed`. Requests carry the client name `AutoScout Russian-edition check` and are spaced (LiveLib 6 seconds, Wikidata 7, the others 1); a source that fails or blocks 3 times in a row is not asked again in the pass. LiveLib refuses a client after about 30 searches, so `--max-livelib-searches` (default 25) caps the LiveLib searches of a pass, spent in the order the books come; a repeat of a search already answered is not counted, and a book past the ceiling is counted under LiveLib as `not_asked`. LiveLib is optional for completeness: a book with a Cyrillic author name from any source is complete when every enabled catalogue answered for it, and a book without one when LiveLib and Wikidata, each when enabled, answered for it. Either way at least one edition search (LiveLib, the State Library or LitRes) must have answered for the book. A book that is not complete gets no record and no lookup date unless it is confirmed. A State Library answer that reports hits but has no readable record counts as a failure, and so does a LiveLib page whose result blocks yield no result. `match_unavailable` counts every book put to the model that got no valid answer (left out, or a number outside its list); an explicit none is an answer. LitRes gives no edition year; a confirmed LitRes edition takes its ISBN and publisher from one more LitRes request for its detail. A confirmed record is never replaced; any other is asked again after 28 days. Decided books are skipped; books of the current candidates come first, then the rest; within each, books never attempted; then, among attempted books, those never searched on LiveLib, then the oldest LiveLib search, then the oldest attempt (every book tried gets `russian_edition_attempt_date`, completed or not, and every LiveLib search actually sent gets `russian_edition_livelib_search_date`, not one skipped at the ceiling or answered from the cache; neither makes a book settled). `--max-books` (default 150) caps the books looked up; the pass spends at most 30 minutes; `--skip-source livelib|wikidata|rsl|litres` turns a source off; skipping both `rsl` and `litres` is refused (exit 2, `reason_code=no_catalogue_source`). Only what the pass itself learned is merged into Candidate History, never over a later lookup, attempt or LiveLib search date. Writes `russian-edition-completion.json` after what it learned; when that file cannot be written the command still exits 0 and prints `warning=report_file_not_written` with one Russian line. `--scope undecided` (default) or `current`. Without `--apply` it writes nothing and exits 3. It refuses to write (exit 6) during a weekly run or while another write holds the report directory's lock; the weekly run is checked again under the lock immediately before the first write, and a run that started during the lookups leaves everything unwritten (`reason_code=weekly_run_in_progress`). The command does not read the weekly config. The weekly run runs the same pass as its `russian-edition-completion` stage, after work-age completion, on every undecided book, only when the weekly config sets `russian_edition_completion: true`; `russian_edition_max_books`, `russian_edition_max_livelib_searches`, `russian_edition_budget_seconds` and `russian_edition_skip_sources` override its limits there, and an unusable value is a config validation error (#1181). Launcher: `./scripts/complete-russian-editions`. |
| `complete-work-age` | Record how old each book's work is (#1138): the year it was first published anywhere and how many editions Open Library knows, as one `work_age` block (`status` `found`/`not_found`/`not_checked`, `first_publish_year`, `edition_count`, `basis` `original_edition`/`isbn`/`title_author`, `source`, `source_url`, `lookup_date`). A book with a confirmed original edition is looked up by the original's title; any other book by its ISBN and then by title and author. A work must agree on title and author through the original-edition confirmation's strict match; a doubtful match is stored `not_found` with no year or count. An unreachable catalogue leaves the record untouched, and after 3 unavailable answers in a row the pass stops asking and counts the remaining books as `skipped_catalogue_unavailable`. A `not_found` book is asked again after 28 days; a found work only when an original was confirmed after it. Requests are spaced 2 seconds apart; no model is called. Writes `work-age-completion.json`. `--scope all` (default) takes every book in Candidate History whatever its editor status, `--scope undecided` every book without an editor decision, `--scope current` this run's candidates; the command spends at most an hour, the weekly stage 10 minutes. Without `--apply` it writes nothing and exits 3. It refuses to write (exit 6) during a weekly run or while another write holds the report directory's lock. Launcher: `./scripts/complete-work-age`. |
| `source-proposal` | `add` / `accept` / `reject` / `reconsider` a Source Proposal (the gate before any source is configured; see `docs/source-onboarding-runbook.md`). |
| `source-lifecycle` | Keep, promote, or retire configured sources after a source review. |
| `record-feedback` | Record confirmed editor feedback for one candidate (two-phase preview/confirm). |
| `record-feedback-batch` | Record a returned file of editor decisions in one confirmed batch (two-phase preview/confirm with a payload digest). See below. |
| `record-visibility` | Record a confirmed hide/remove or restore/show action for one candidate. |
| `export-decision-corpus` | Read-only on state: `--state-dir <dir> [--output-dir <dir>] [--ref <commit>]` writes the dated decision corpus (what `scripts/export-decision-corpus` execs). Prints the export directory, the record, dated and undated counts with each undated reason, and the run time. |
| `ranking-benchmark` | Read-only: `--output-dir <dir> --state-dir <dir> [--json] [--max-positives-moved-down-share <0..1>]` prints the ranking benchmark (what `scripts/ranking-benchmark` execs). |
| `work-age-report` | Read-only on state: `--output-dir <dir> --state-dir <dir> --report-file <file>` writes the work-age report (what `scripts/work-age-report` execs). A report file inside the state directory is refused. |
| `editorial-profile` | `add` / `list` / `revoke` / `set-terms` Editorial Taste Profile entries. |
| `ingest-feedback` | Stage returned editor feedback from a Sheet CSV export (fallback path). |
| `prepare-description-backfill` / `apply-description-backfill` | Prepare and apply metadata-only Russian Content Description backfill. The prepare command lists blank, fallback, and `codex-generated` stub descriptions (#912). The apply command replaces a description no source confirmed (`codex-generated` or `agent-unverified`) without `--allow-overwrite-valid`; every other valid description still needs the flag (#791). After Candidate History is written, apply projects the description facts onto `scouting-candidates.json` and `latest-scouting-candidates.json` in the state directory when they exist, so `render-review` alone shows the new text (#912). A backfilled description renders «Описание по исследованию, источники не проверены автоматически», never a grounded status, because its research sources are not checked pages. |
| `adjudicate-descriptions` | Preview description adjudication from Candidate History, and with `--apply` write accepted descriptions through the existing description apply gate. Candidate selection reuses the backfill work-item helper; without `--apply` it changes no state. Each candidate starts with one adapter call; a timed-out call gets one exact retry at the end while a full call still fits the stage budget (#865). `--adapter replay` (the default) is the deterministic offline adapter the fixture run uses; `--adapter codex` runs the live Codex CLI with web search enabled - one candidate per call, isolated temporary directory, hard timeout, and schema-validated responses. A timeout, a non-zero exit, an unparsable response, a wrong identity-key echo, or a schema-invalid response is a bounded per-candidate failure: it never stops the command and never produces a description. The `codex` adapter runs the model and the reasoning effort currently selected in the editor's Codex config, so AutoScout adjudicates with the same model, thinking as hard, as the editor works with; both are resolved once per run, and the CLI defaults apply when neither resolves. One call is bounded at 240 seconds, above the 135 seconds measured for the hardest candidate at high reasoning effort. The rest of the user config stays out, so an unattended batch does not fire the editor's `notify` hook once per candidate. The `codex` adapter spends network and model quota per candidate and must be chosen explicitly. The per-candidate loop runs on a bounded pool (`--max-workers`, default 3, this stage's own constant) sharing one fetcher, so per-host spacing stays intact; the sort and the apply-gate pass stay sequential, so the preview is byte-identical whatever order candidates complete in. The stage takes a budget (`--stage-budget-seconds`, default 5400): admission stops when the remaining time cannot fit one more 240-second call, and unattempted candidates are reported with `not_attempted_stage_budget`, not dropped. Ninety minutes covers the 92-candidate worklist measured on 2026-09-03 at 3 workers, and absorbs a slower mean; one hour fit that measurement with less slack than a single call. Work items carry bounded grounded-stage URL hints (`search_hints`, at most 3, starting points only and never evidence; loaded best-effort from the grounded stage artifact in the output directory). Besides candidates with no description, the command selects every unsourced stub (`codex-generated` text, any length) on every run: a verified description replaces the stub, and a failed attempt leaves it in place (#788). With `--deep-pass` a candidate the first call leaves at the placeholder gets one more call that searches by ISBN and original-language title, publisher and bookshop pages first; the weekly run always enables it, and the stage report then carries `deep_pass` counts. Citation verification (#791): a cited page is this book when it prints the candidate's ISBN in either length, or prints the cited title and its author; a page that states no author of its own may print the author surname anywhere in its visible text outside related-books sections, and a page that does state an author is never overridden by its text. A failed re-fetch is retried once. Citation owners are derived with `configs/source-owner-policy.json`, the rule the grounded stage uses (#1123): every host in its shared-owner class (storefronts, Google Books, Goodreads) shares one owner, any other host, a publisher's own site included, is owned by its registrable domain after the equivalence table, and a citation on a denylisted host is dropped as `source_denylisted`. When the agent answers resolved with `high` confidence and no citation counts, the description is still applied with decision `apply_unverified` and provenance `agent-unverified`, but only where the record holds no description or a `codex-generated` stub, and never when a cited page named a different author. The review marks it «⚠ Описание не подтверждено источниками», coverage counts it as unsourced, and it is not selected again on later runs. Not-yet-published books (#791): the response carries one closed `unresolved_kind`, and the agent sets it to `not_yet_published` for a book that is announced, untitled, or offered for rights. Code believes that verdict only when the record has no ISBN that may prove identity and no publication year before the run's year; the row then falls back with reason `work_not_yet_published` and the record gets `description_not_yet_published_date`. For 14 days neither description stage selects the record (the grounded stage reports skip reason `awaiting_publication`); `--only` still searches it at once. Any other answer, or an applied description, clears the date. The review keeps the placeholder and shows «Книга ещё не опубликована — описание появится позже», and coverage reports such rows as `not_yet_published_count`, inside `fallback_count`, and leaves them out of `coverage_denominator` (the `N` of the headline «X из N») and of `fallback_by_source` (#944). |
| `merge-candidate-identity` | Apply a guarded, replayable candidate identity correction. |
| `probable-duplicates` | Read-only: list probable duplicate Candidate identities in `--state-dir` for a person to decide; never merges. See below. |
| `resolve-review-rows` | Read-only: match returned review rows (`--rows` as a `.json` list, an editor `.csv`, or a `.md` review) to durable Candidate identities in `--state-dir`. See below. |
| `source-novelty` | Read-only: match the entries sampled from a candidate source page (`--sample`) against Candidate History in `--state-dir` and print a novelty verdict. See below. |
| `source-yield-report` | Read-only: per source, the editor's decisions on the books it reported, with the sources that meet or approach the proposed retirement rule marked. See below. |
| `migrate-candidate-history` | Plan or apply the deterministic offline Candidate History identity migration. |
| `shard-candidate-history` | Convert a state directory's Candidate History from `candidate-history.json` to one file per book, once (#1073). `--dry-run` writes nothing; `--force` replaces shards the directory already has. See "Candidate History on disk" below. Launcher: `./scripts/shard-candidate-history`. |
| `state-database` | `rebuild` builds the SQLite state database of `--state-dir` from its JSON; `export` writes the database back into the state directory as JSON (#1074); `cutover` makes SQLite serve the state directory for every command and `rollback` returns it to JSON (#1093). See "The SQLite backend" below. Launcher: `./scripts/state-database rebuild|export|cutover|rollback`. |
| `audit-editorial-notes` | Build a deterministic offline editorial audit from Candidate History and the Editorial Taste Profile. It is read-only (no collection, research, workflow, issue creation, migration, merge, ranking, source, or profile change) and writes only `editorial-audit.json/md` to `--output-dir` (default `data/editorial-audit`, outside canonical state). Bare refusals stay unspecified with no inference; heuristic triage is labeled as human-review-required. |

### Scheduled runs on the run server (#1057)

The run server starts the scan every night at 02:30 Asia/Jerusalem and the Kirkus pass at 01:30, from the units in `deploy/server/` (install steps in `docs/operator/run-server.md`).
Both launchers take `--scheduled` there; without it every command behaves as described above.

`run-weekly --scheduled` takes the run lock before it reads or writes anything.
When the lock is held (`guard_held`, or `stale_guard_with_live_process`) by a process that is verifiably alive, the start ends with the outcome `skipped/run-in-progress`: exit 0, the line «Запуск по расписанию пропущен: поиск уже идёт.», the morning report with the verdict «пропущен», and no file written.
A lock whose process is dead, or whose owner cannot be read, is not a run in progress: an old one is taken over, and one younger than an hour refuses the run at `repository-preflight` with exit 1, a journal line and a «остановлен» report, as for a manual run. A manual run that meets a held lock is refused as before.
There is no "already ran today" check: an earlier run on the same date does not cancel the scheduled one.
The second attempt of the night is the exception (#1160), below.
A scheduled run has no `open-review` stage, so a clean one ends as `completed/no-attention` with no "did not open" line.
When it ends `intervention-required`, it also prints «Остановлено на этапе <stage>, причина: <reason>.».

#### The second attempt of the night (#1160)

`run-weekly --scheduled` with `AUTOSCOUT_SECOND_ATTEMPT=1` in its environment is the later start of the night, from `autoscout-run-retry.timer` at 04:30 Asia/Jerusalem; the wrapper sets the variable for `run-weekly` alone. It is a variable, not an option, so code without #1160 (a checkout not yet updated, or the last passing code of #1159) runs an ordinary scheduled start rather than refusing an unknown option; a manual run ignores it. It stays in the environment of the restarts on updated code (#1117, #1130) and of the start on the last passing code (#1159); the code gate's check gets no `AUTOSCOUT_*` variable.
After taking the run lock, it reads `workflow-log.json` for the entries stamped with the date's operation id, the evidence a same-date retry resumes from, and HEAD's copy of that file:

- A `terminal` entry `completed/no-attention` or `completed/attention-required` for that operation, present in HEAD's copy (committed, not only in the working tree): the outcome is `skipped/already-completed`, exit 0, the line «Повторная попытка не нужна: поиск за <дата> уже завершён.», no report and no file written.
- A live run holding the lock: `skipped/run-in-progress` as above, but with the line «Повторная попытка пропущена: поиск уже идёт.» and no report.
- Anything else runs the scheduled search as the first attempt does: no stamped entry (the first attempt never started, met a live run, or died before its first record), stamped entries with no terminal (it was interrupted), a completed terminal only in the working tree (it died while publishing its log; the modified log alone is set aside by the preflight as in #1057, or exempted as a same-operation artifact), or `intervention-required` at any stage and reason (the stage and reason named are those of the last failed entry, `repository-finalize` included). Being the same operation, it resumes as any same-date retry does, and it refuses whatever the next night would refuse.

Only stamped entries count: a legacy unstamped log or a log of another date is no run of this date. When the source portfolio of the config changes between the two attempts, the operation id changes too, and the second attempt runs again.
Its morning report, sent by the same rule as every scheduled run's, carries « (повторная попытка)» at the end of the subject, a second body line naming how the first attempt ended («Это повторная попытка: первый запуск этой даты не состоялся или был прерван до первой записи в журнал.», «… был прерван и не завершился.», «… был прерван, когда сохранял итог.», or «… не завершён. Остановлено на этапе <stage>, причина: <reason>.»; «Это повторная попытка этой ночи.» when the log exists but is not a readable list). A start restarted on updated code takes the first attempt from `AUTOSCOUT_SECOND_ATTEMPT_PRIOR_RUN`, set by the start that restarted it (`unknown` when that start could not read it), because that start's preflight may have set the failed log aside. The report also has the journal command of `autoscout-run-retry.service`.

#### The gate on new code (#1124)

After its repository update and before `operation-start`, `run-weekly --scheduled` checks that the checked-out code version passed `./scripts/check` on this machine.
A code version is the committed tree without its top-level `runs/` entry, so commits that change only `runs/` (the run's own state commits) keep it; any other change makes a new version.
A version without a recorded pass is checked. First, when the checkout's `.venv` is behind the pins of `pyproject.toml` (the runtime dependencies, `dev`, and every other extra with at least one package installed or remembered from an earlier check in `data/code-gate/extras.json`; a requirement with an environment marker counts only when installed), the gate runs `.venv/bin/python -m pip install -e ".[dev,<extras in use>]"` with a 10-minute limit in its own process group and the same environment as the check (the package listings before and after also run without the `AUTOSCOUT_*` variables), writes pip's output and every changed package to `data/code-gate/last-install.log`, and returns the changed packages as `environment_update` (#1130); a failed install, or one after which the `.venv` is still behind, is `server_venv_update_failed`, one over the limit `server_venv_update_timed_out`, and neither runs the check. Then `./scripts/check` runs with `TMPDIR` set to a new folder under `AUTOSCOUT_CODE_GATE_TMPDIR` (default `~/.cache/autoscout/code-gate/`), removed afterwards, with every `AUTOSCOUT_*` variable left out of its environment and a 30-minute limit.
Its output is written to `data/code-gate/last-check.log`; a pass is recorded in `data/code-gate/passed.json` (commit, code fingerprint, time, and the non-`dev` extras the `.venv` used, #1159). Failures are not recorded, so the next scheduled run checks again.
A pass is logged as a `code-gate` entry with status `passed`, the short commit, whether this start ran the check (`checked`), when the version passed (`passed_at`) and, when this run updated the `.venv`, `environment_update` (`updated_at` and `packages`, each `package`, `before`, `after`; `""` is absent). A pass after an update is recorded with that update; a start that updated the `.venv` restarts like a start that pulled code (at most twice: once for pulled code or an update, and once more only for an update made by the restarted start), and the restarted start (marked by `AUTOSCOUT_RESTARTED_AFTER_ENVIRONMENT_UPDATE`) logs the recorded update; a pass recorded on an earlier night is never reported again. The morning report names the changed packages. The check runs in its own process group; on timeout the whole group is killed.
A failure ends the run `intervention-required` at stage `code-gate` with reason `new_code_failed_check`, `server_venv_out_of_date` (the check refused a `.venv` behind the dev pins in `pyproject.toml`, which the gate could not judge, for example because its packages could not be listed or `pyproject.toml` could not be read; without a `.venv` the check refuses the system Python and the reason is `new_code_failed_check`), `server_venv_update_failed`, `server_venv_update_timed_out`, `new_code_check_timed_out`, `new_code_check_not_started`, `checkout_has_uncommitted_changes` (tracked files differ from the commit, so the check would not test what the record names; untracked files are not counted) or `code_version_unknown` and the short commit in the entry's `detail`, before the State Store is opened (only the run's own log and evidence have been read); the lock is released and the morning report names the reason and the commit.
The gate runs before the restart on updated code (#1117), so a run never restarts on code that failed.

#### A failed gate runs the last passing code (#1159)

When the gate fails with `new_code_failed_check`, `new_code_check_timed_out`, `server_venv_out_of_date`, `server_venv_update_failed` or `server_venv_update_timed_out`, the scheduled run judges whether the commit recorded in `data/code-gate/passed.json` can run against the checkout as it is. It can when all of these hold:

- that commit's package declares `FALLBACK_PROTOCOL` in `autoscout/code_gate_fallback.py` (a pass recorded by code before #1159 cannot);
- every path that differs between that commit and `HEAD` is under `autoscout/` (but not `autoscout/state_store/`), under `runs/`, under `tests/`, `docs/`, `scripts/`, `deploy/`, `.github/`, `.claude/` or `.agents/`, or is `pyproject.toml`, a top-level Markdown file, `LICENSE`, `skills-lock.json` or `.gitguardian.yml`;
- the checkout's `.venv` meets that commit's runtime pins (its dependencies, every extra in use, remembered in `extras.json` or recorded with the pass, without `dev`), its packages listed with `python -I` so an inherited `PYTHONPATH` does not count. A requirement with an environment marker whose package is missing stops the fallback unless its marker is false, evaluated only when the deciding process itself runs on that `.venv`.

Then the run writes nothing more and releases the lock (outcome `restart/last-passed-code`), writes that commit's `autoscout/` package to `data/code-gate/last-passed/` with `git archive` (with `snapshot.json` naming the commit), and starts the same command again with the checkout's `.venv/bin/python` (the interpreter whose packages met the pins, whatever Python decided), `PYTHONPATH` set to the snapshot, `PYTHONSAFEPATH=1`, `AUTOSCOUT_REPOSITORY_ROOT` set to the checkout and `AUTOSCOUT_CODE_GATE_FALLBACK` holding the hand-over (the protocol, both commits, the gate's reason). Every module reads the checkout through `AUTOSCOUT_REPOSITORY_ROOT`, so the configs, the State Store database, the state directory and the Git publication are the checkout's. The checkout itself is never moved.
The started-again run checks nothing; after its repository update it confirms that `HEAD` is still the judged commit, that `passed.json` and `snapshot.json` name the commit it runs, that its own package was imported from the snapshot and that it runs on the checkout's `.venv`, and logs `code-gate` with status `ran_last_passed`, `commit` (the failed one, short), `ran_commit` (short), `reason` (the gate's), `passed_at` and, when the failed gate updated the `.venv` before its check, its `environment_update`, which the morning report names as on any night. If any of that does not hold, it stops at `code-gate` with the gate's reason and `fallback_refused: fallback_not_confirmed`.
When the run cannot fall back, it stops as before, and the failed entry's `detail` adds `fallback_refused` (`checkout_moved_during_check`, when `HEAD` is no longer the commit the gate checked; `last_pass_predates_fallback`, `last_pass_unavailable`, `new_commit_changes_files_the_run_reads`, `new_commit_changes_state_store`, `venv_does_not_meet_last_pass_pins`) and, for a changed path, up to ten `fallback_refused_paths`; with no recorded pass the entry is as before.
The morning report of a night on the last passing code is «требует внимания» at least and says, without commits, that the night ran on the earlier version and why the new one did not pass; a stopped night says why the earlier version did not run either. The doctor adds the same sentence after a night that ran on the earlier version.
A manual `run-weekly` runs no check and is never blocked; it logs `code-gate` with status `passed` or `not_checked` and, for `not_checked`, prints one warning line.
`collect-kirkus --scheduled` on a code version without a recorded pass makes no request, prints one line and exits 0, and never installs; a manual pass is not gated.
`./scripts/code-gate --run` runs the same update and check and prints each changed package; a failed install exits 1 and names `data/code-gate/last-install.log`.

Every Kirkus pass, whoever starts it, holds an operating-system lock (`flock`) on `kirkus-collection-pass.lock` beside its result for the whole pass; a pass that dies releases it with its process.
`collect-kirkus --scheduled` makes no pass when another pass holds that lock, or when the Kirkus attempt record already carries its date, whatever its status. It then prints one line and exits 0. A scheduled pass never touches Git.
A manual `collect-kirkus` that finds another pass running reports `kirkus_pass_in_progress` and exits 2 without touching the result.

Every scheduled `run-weekly` sends exactly one morning report when it ends, whatever the outcome (operator's decision of 2026-11-14). Manual runs and every `collect-kirkus` send none; a failed scheduled pass prints its stage and reason and keeps its exit code.
The subject is «AutoScout: ночной поиск <дата> — <вердикт>», the verdict following the outcome: `completed/no-attention` успешно, `completed/attention-required` требует внимания, `skipped/run-in-progress` пропущен, anything else (including an unexpected exception and a config exit) остановлен. A second attempt adds « (повторная попытка)» and sends nothing when it skips (#1160).
The body has the verdict sentence; for a completed run the new and updated book counts and the Russian description coverage; the sources with problems and their category, with each failed or skipped source's failure streak (see "Failure streaks per source" below); the number of description problems; for a stopped run the stage and reason token, and for a failed `code-gate` the short commit; the night's Kirkus pass from its attempt record (collected, cut short, failed with its reason, started and not finished, not run today); and the journal command.
It never names a book, an author, a path or raw error output; a reason that is not a plain token is sent as `unrecognized_reason`.
It is sent by SMTP, configured only by these environment variables, which the units load from `~/.config/autoscout/notify.env` (mode 0600):

| Variable | Meaning |
|---|---|
| `AUTOSCOUT_NOTIFY_SMTP_HOST` | SMTP server |
| `AUTOSCOUT_NOTIFY_SMTP_PORT` | Port; default 587 with STARTTLS, 465 for TLS from the start |
| `AUTOSCOUT_NOTIFY_SMTP_USER` | Login, optional |
| `AUTOSCOUT_NOTIFY_SMTP_PASSWORD` | Password, optional |
| `AUTOSCOUT_NOTIFY_FROM` | Sender address |
| `AUTOSCOUT_NOTIFY_TO` | Recipients, comma-separated |

Without the host, the sender or a recipient the report is printed after one line saying it is not configured, so the journal still holds it.
A delivery error prints one line with the error type and never changes the exit code.

Every run, manual or scheduled, handles the log an earlier failed run left behind.
When the dirty paths are exactly the unstaged run log alone, or with an unstaged `run-summary.json` of the same directory that differs from `HEAD` only in its top-level `toolchain_preflight` key (what a Stage 0 failure writes), the preflight copies them to the Git-ignored `data/workflow-log-residue/` under one shared stamp (the 30 newest sets are kept), restores them only when every copy succeeded, and the new run's log records one `log-residue` entry with status `set_aside` and the file names.
A staged log or summary, a summary with any other change, a summary that does not parse, or any other modified path still blocks with `dirty_working_tree`, and `./scripts/doctor` judges the residue with the same rule.

A run that failed late, after it saved its state, leaves more behind: its reports, its artifacts and the State Store's files (#1161). The next run, manual or scheduled, carries them and publishes them; it restores, removes and moves nothing. Under the run lock it first proves the leftovers are that run's, and otherwise blocks with `dirty_working_tree` as before, touching nothing. Modification times are not used. All of this must hold:

- the checkout is the expected one: its root, remote and branch, with no merge or rebase in progress (this also holds for the #1057 set-aside);
- the output directory is the state directory, as on the run server;
- every dirty path is inside it and is an unstaged change, an untracked file, or an unstaged deletion of a file the run itself deletes (a book's Candidate History file, or `search-optimization-review.json` or `.md`, which the pipeline removes when that review is off or fails); nothing is staged, no path is a symbolic link, and `state-backend.json` is unchanged;
- the run log is among them, changed (or new, when `HEAD` has no run log yet), and the two logs, compared entry by entry, show one failed run: the working-tree log is entries `HEAD`'s run log holds followed by entries it does not hold; those unpublished entries all carry one operation id, include a successful `repository-preflight` and a successful `pipeline` (that run saved its candidate state), and every `terminal` `HEAD` holds for that operation is older than that preflight. The operation may be the one about to run: a later start on the same date, such as a second attempt that night, is carried on the same terms, also after an earlier run of that date completed and published;
- under SQLite the database's export is `current` or `behind`, so editor decisions exported after the failed run are fine and a hand edit of a state file blocks.

Once that is proven, the preflight copies the failed run's `workflow-log.json` to `data/workflow-log-residue/workflow-log-<stamp>.json` for diagnosis (kept and pruned as above) and marks the run directory as awaiting publication: one small file under the Git-ignored `data/late-failure-awaiting-publication/`, naming the directory and the failed operation. While the marker exists, every dirty path inside the directory that a run may carry, by the rule above, is allowed by the preflight and the update, like a validated artifact allowlist; the State Store must still stand behind its files. The marker does not depend on the run log, so a start that then fails early (the code gate, the update, a temporary blocker elsewhere) or restarts on updated code leaves it, and the next start goes on. Only a finalization that published the directory removes it. A dirty path outside the directory, a staged change, or a deletion the run never makes still blocks with the marker.

A run that got past its pipeline has rewritten its source runs with their final statuses, so a later start that date never takes the same-operation resume path; the guard that stops a start after an interrupted Kirkus capture (`interrupted_kirkus_capture_requires_recovery`) no longer applies when the latest start of the operation logged a successful `pipeline`. If a resume allowlist applies anyway, the proof reads every dirty path, and the allowlist and the carried paths combine.

Carried tracked files stay modified through the update. If `origin` has a newer commit that changes one of them, the fast-forward is refused and the run stops at `repository-update`, as it did before; the marker stays and nothing is lost.

A file a person dropped into the run directory is carried and committed with the rest, never deleted. The run overwrites what it regenerates, and its finalization stages every path in the directory, new and deleted ones included; before that, the health check runs as always, and the credential scan also reads every carried file (`artifact-secret-scan` stops the publication on a hit, and the marker stays). Under SQLite nothing touches the database or its export, so the doctor finds no `state-store-finding`, and the run's own `state-export` writes what the database holds beyond the export.

Every start that carries leftovers records one `late-failure-leftovers` entry with status `carried`, the earlier run's date (`earlier_operation_date`) and the number of carried paths (`carried`), also when the update or the code gate then stops it; it names no path. A marker that cannot be written stops the run with `late_failure_leftovers_not_recorded`.

### Failure streaks per source (#1125)

Every weekly run with a state directory counts, per source, the attempts in a row that failed, in the State Store collection `source_failure_streaks` (`source-failure-streaks.json`, one record per source name: `consecutive_failures`, `streak_started`, `last_failure_date`, `last_success_date`, `last_reason_code`).
The stage `source-failure-streaks` runs after the review is rendered and before the health check, and puts the run's view under `source_failure_streaks` in `run-summary.json`; run-health, the run's outcome, the doctor and the morning report all read that view.
The stage never stops a run: when the count cannot be recorded it logs `status=not_recorded`, the run has no view, and every failed or skipped source asks for attention as before.

The counting rule:

- An attempt is a source the run tried to collect, whether it reached replay (`source-runs.json`) or stopped at capture (the capture ledger of `workflow-log.json`); a failed problem the run found itself is a failed attempt of its source whatever the ledgers say: an extraction that left the source not ready while replay served an earlier artifact (`reused`), and a Kirkus, PM or PW pass that failed while its source was served from the previous result (reason code `source_pass_failed`). A source that ran out of the run's capture time (`capture_deadline`) did not deliver either, so that counts as a failed attempt too. `collected`, `precollected` and `codex-captured` are a success; `failed` is a failure, and so is every `skipped` run but a deliberate one (a blocked page, a captcha, an HTTP 403 or a missing credential, with or without a problem code).
- A source the run did not try keeps its streak: `not-due`, `reused`, `inactive`, `ignored`, and the deliberate skips: a source whose `access_status` says to skip it, Kirkus left to its background pass, `ranking_list_not_yet_refreshed` and `between_editions`. A weekly, monthly or annual source therefore never reaches the threshold by being idle.
- At most one failure counts per source per run date. A second failed run on the same date (a manual run after the nightly one, a resumed operation) updates the reason code but not the count.
- A success resets the count to 0. A run in which a source succeeds after a streak at or over the threshold reports the recovery, and so does another run on the same date (a resumed operation, a manual run); a later date does not.
- A widespread failure is never noise: when at least two sources failed and they are more than half of the sources the run attempted, every failed or skipped source asks for attention whatever its streak (the view's `widespread`, with `attempted` and `failed_attempts`). That is a broken run, not one bad source; on 2026-11-10 nearly every source failed in one run.

The threshold is the weekly config's top-level `source_failure_streak_threshold`, a whole number of attempts, 1 or more; it is 3 when absent, and any other value fails validation.
A failed or skipped source below the threshold is listed but does not by itself make the run `completed/attention-required`; a source at or over it does, and so does a widespread failure; both are named first.
Every other cause of attention is unchanged, including incomplete coverage, an unusual count and any problem the Kirkus, PM or PW pass reports itself: a missed PW issue or PM email batch does not come back on a later night (#1117), so a pass problem asks for attention every time. Those sources' own source runs are counted like any other; the view names the sources whose pass reported a problem (`pass_problem_sources`), together with any of them whose collection is older than its age limit (`kirkus_stale_collection`, `pm_stale_collection`, `pw_stale_collection`: issues or emails are being missed now), and any of them asks for attention in the doctor and run-health too, under «Сбои отдельного прохода сбора (Kirkus, PM, PW)», whatever its streak and whether or not its source run succeeded.
A rerun of the pipeline after editor feedback (`run-mvp`) keeps the view of the last collection; a weekly run removes it as soon as its own pipeline has run, and writes its own in the `source-failure-streaks` stage, so a run that stops in between, or has no state directory, or cannot record the count, shows no earlier run's view. `run-health` decides from the view alone when there is one, because the view also holds the sources that stopped at capture, which `source-runs.json` does not.

`run-health` prints the view as `source_streak_threshold=<n>`, one `source_streak=<source>|broken|listed|not_counted|<count>|<last success date or none>|<reason code>` per failed or skipped source, and `source_recovered=<source>|<failed attempts>` per recovery; `recurring_attention_required` follows the same rule as the run.
The doctor's exit code for a completed run follows it too: `2` (`source-failure`) when a source is at or over the threshold or the failure is widespread, and `0` (`healthy`, with the listed sources and recoveries in the verdict and in `facts`) when every failed or skipped source is below it. A run whose `run-summary.json` has no view, made before #1125 or without a state directory, keeps the old rule: any failed or skipped source is `source-failure`, exit `2`.
The doctor and the morning report say, per source, the reason in the review's fixed Russian words (the report adds the reason code), the streak and the date of the last success:
«Не работают 3 и более попыток подряд: <источник> (не удалось собрать список; неудачных попыток подряд: 3, последний успех 2026-11-13).», «Сбои, пока не требующие внимания: …» and «Снова работают: <источник> (до этого неудачных попыток подряд: 4).»; a widespread failure opens with «Не удалось собрать N из M источников этого запуска: похоже на общий сбой, а не на отдельные источники.» and lists the sources under «Другие сбои этого запуска».

### Editor decision log (#1053)

Every Candidate History record keeps each confirmed editor decision in `editor_decisions`, oldest first.
`editor_status` and `editor_note` still show the latest decision, which is always the last entry.
An entry is flat:

```json
{"status": "reject", "note": "<the editor's note, verbatim>", "decision_date": "2026-11-14", "description_digest": "<16 hex characters>"}
```

`description_digest` is the first 16 hex characters of the SHA-256 of the description the review showed when the editor decided (a nonblank Candidate History `content_description` first, then the latest candidate's, as the review chooses), `""` when the review showed none: no description, the fallback placeholder, or a description that fails validation.
`record-feedback --confirmed-by-editor`, `record-feedback-batch --confirmed-by-editor` and `ingest-feedback` stamp `Decision Date` and `Description Digest` on the returned row, so the row keeps them in `archived-returned-editor-rows.json` and the entry keeps the moment of confirmation even when `run-mvp` consumes the row later.
A changed decision adds an entry.
A consumption logs all rows for one book together, and a retried consumption adds nothing when the log already ends with one entry per row that agrees with the row's date, digest, and any status and note it carries.
A decision made before the log existed is never dated: an unrelated save leaves the record without a log, and when a new decision replaces it the old one becomes an entry with `decision_date` and `description_digest` set to `null`.
A merge keeps every entry of every merged record, equal entries included, and a merge with an `editor_decision_resolution` logs the chosen decision with the merge date after the decisions it replaces.
The preview phase writes nothing, and the review does not read the log.

### State Store backend (#1068)

Canonical state is read and written through the State Store (ADR 0023).
The backends are `json`, the default until cutover, and `sqlite` (#1074): config validation refuses any other name, and a weekly run given one stops at `operation-start` with `state_backend_unknown` before it collects.

A state directory cut over to SQLite decides its own backend (#1093).
It holds the committed file `state-backend.json` with exactly `{"backend": "sqlite"}`, and every command that opens it uses SQLite, with or without `--config` and the environment variable.
A selection of `json` there, from `AUTOSCOUT_STATE_BACKEND`, the weekly config's `state_backend` or a caller, is refused for reads and writes alike, with an error that names `./scripts/state-database rollback`: the JSON files in a cut-over directory are an export that can lag the database, so a JSON read could show stale state and a JSON write would bypass the database.
The JSON backend itself refuses to write a marked directory, except as the SQLite backend's export.
A weekly run whose config or environment selects `json` on a cut-over directory stops at `operation-start` with `state_backend_conflict` before it collects.
A marker that is a symlink, unreadable, or anything but `{"backend": "sqlite"}` is refused rather than ignored.
The marker names no database path, because the database lives outside Git at a path that differs between machines.

A state directory without `state-backend.json` is served as before #1093: the environment variable `AUTOSCOUT_STATE_BACKEND`, else the weekly config's top-level `state_backend`, else `json`.
An explicit selection of `sqlite` there is allowed (tests and rehearsals use it), but only the commands given that selection see it; a cut-over is what makes every command agree.
Every weekly run records the backend and what chose it (`environment`, `config`, `default`, or `state_directory` for the marker) under `state_backend` in the `operation-start` entry of `workflow-log.json`.
That entry names the backend the whole run uses: the run opens the store once there and passes it to the pipeline, the post-description refresh, the editorial-fit shadow stage and the review (#1069).
`run-mvp` and `render-review` choose the backend the same way from the config they are given.
The ISBN, original-edition and PM-logline stages and both description stages get the same store (#1071).
A completion pass writes Candidate History and the latest Scouting Candidates in one store transaction that reads history again, so a value written meanwhile is never replaced.
The standalone commands, the doctor's review repair and the operator tools open their store from the state directory or config they are given.
Only `autoscout/state_store/` names the canonical state files; `tests/test_state_store_stake.py` fails for any other module and lists the few that still have to, each with the reason.

### Candidate History on disk (#1073)

The JSON backend keeps Candidate History in one of two layouts, and the layout follows what is in the state directory:

- **Single file**: `candidate-history.json`, every record in one key-sorted list. A state directory that has this file and no `candidate-history/` directory stays in this layout and is written exactly as before.
- **One file per book**: `candidate-history/<xx>/<candidate_id>.json`, where `<candidate_id>` is `candidate-` plus 24 lowercase hex characters and `<xx>` its first two hex characters. Each file holds one record as key-sorted JSON with the same formatting as every state file. Every other state directory uses this layout, a new or empty one included.

A state directory that holds both is refused with an error that names `./scripts/shard-candidate-history`.
In the sharded layout a record without a valid `candidate_id`, or two records with the same one, is refused and nothing is written.
A record that did not change keeps its file untouched; a removed record's file is deleted, and so is a directory it leaves empty.
Reads return records in the same order as the single file (by identity key).
A State Store transaction stays all or nothing across book files written, book files deleted and the other collections: a failure on the way restores every file it replaced or deleted.
Every other collection stays one file.

When the run's output directory is its state directory (the weekly launcher) and the state is sharded, the run writes no separate `candidate-history.json` copy: the copy and the canonical history would be the same thing.
With a separate output directory, or on single-file state, the copy is written as before.
A run without a state directory reads its history from the copy, or from the shards when the output directory holds them.

`shard-candidate-history --state-dir <dir>` converts one state directory, explicitly and once; nothing converts a directory on its own.
It reads `candidate-history.json`, validates every record, writes the shards into a staging directory inside the state directory, reads them back and checks that the records equal the originals, and only then moves the shards into place and removes the single file.
A failure at any step leaves the state directory as it was.
It refuses a directory without `candidate-history.json`, and one that already has `candidate-history/` unless `--force` is given.
`--dry-run` validates and prints the counts and writes nothing.
Without `--dry-run` it refuses with exit `6` during a weekly run, like every state mutation.
It prints `candidate_history_conversion=dry_run|converted|refused`, then `record_count`, `shard_count`, `directory_count`, `largest_directory_count`, `total_shard_bytes`, `single_file_bytes`, `single_file_bytes_reproduced` (whether the records read back serialize to the file's exact bytes) and `seconds`; a refusal prints `error=` and exits `2`.
The operator steps, with the check and the rollback, are in `docs/state-store-cutover-runbook.md` (Part 1) and, in short, in `docs/operator/run-server.md`, "Converting Candidate History to one file per book".

Code that reads Candidate History as Git or a run directory holds it knows both layouts: the Git envelope commits every changed, added and deleted book file; the description benchmark and the archive marker read either layout from earlier commits; the editorial audit names the file or the shard directory with a digest over every book file; the artifact parity gate compares shards as the single file they hold; and every path under a `candidate-history/` directory is protected from artifact writers, through a symlink too.

### The SQLite backend (#1074)

In a state directory cut over to SQLite, or with `sqlite` selected explicitly, canonical state lives in one SQLite database file, through the standard library's `sqlite3` only.
The database is never committed: Git keeps its JSON export, which has exactly the bytes the JSON backend would keep for the same state, in the layout the state directory already has (one file per book for a new or empty one).
The weekly run writes the export before the Git envelope commits (`state-export` in `workflow-log.json`), and every state mutation listed under "Editor mutations during a weekly run" writes it when it finishes; on the JSON backend neither does anything.
An export writes only the files whose bytes change and deletes the file of a removed book; when it knows the last export into the state directory, it looks only at the books changed since.
A state directory changed since the last export by anything but the export is refused, because writing the database over it would lose that change.

The database is `data/state-store/<state directory>.sqlite3` by default (git-ignored; `runs/current-weekly-pilot-russian` becomes `runs--current-weekly-pilot-russian.sqlite3`), or the file the weekly config's `state_database` key or the environment variable `AUTOSCOUT_STATE_DATABASE` names; a relative path is taken from the repository root.
The environment variable `AUTOSCOUT_STATE_DATABASE_DIR` moves the default files to another directory (the test suite under `--state-backend sqlite` gives each test its own); a directory inside the repository other than `data/state-store/` is refused.
A run without a state directory keeps its state as the JSON files beside its report whatever backend the environment or the config selects, because those select the backend of state directories; an output directory cut over to SQLite opens as the state directory it is.
The schema is versioned: version 2 added the table of `source_failure_streaks` (#1125). The first store that opens an older database migrates it; until then the doctor's and run-health's read-only look reads it as it is, the newer collection empty, so its export is still current.
A database on a network file system or in a cloud-synced folder (iCloud, Dropbox, OneDrive, Google Drive and the like) is refused when that can be detected, and so is a database that belongs to another state directory or has a newer schema.
A state directory that holds JSON state but has no database is refused rather than read as empty: build the database first.
So is a cut-over state directory whose database does not exist on this machine (a checkout that pulled the state but never built the database), with an error that names `./scripts/state-database rebuild` (and a backend conflict with `./scripts/state-database rollback`); the CLI prints that refusal as one `error=` line, exits `2`, shows no traceback and writes nothing, whichever command opened the state directory (#1098). A command that maps the refusal itself keeps its output and exit code: `probable-duplicates`, `resolve-review-rows`, `source-novelty` and `audit-editorial-notes` (JSON `input_error`, `2`), `source-yield-report` (`error:`, `2`), `migrate-candidate-history` (`error=candidate_history_migration:`, `1`, or `2` with `--apply`), `adjudicate-descriptions` and `generate-descriptions` (their own prefix, `1`), `state-database` and `shard-candidate-history` (`…=refused`, `2`), the weekly run (`state_store_unavailable` and `state_backend_conflict` at `operation-start`) and the doctor and `run-health` (a State Store finding). The existing exit codes stay: `3` is a preview and `6` a weekly run in progress, which is checked before the state directory is opened; the database is found at its default path or where `AUTOSCOUT_STATE_DATABASE` points, so a database kept elsewhere must be named by that variable wherever commands run.
The database runs in WAL mode with a 30-second busy timeout, foreign keys on and `synchronous=FULL`, so a second writer waits for the first instead of failing.
It keeps every record exactly, including records without a `candidate_id` or sharing one; only the export to one file per book needs a valid, unique `candidate_id`.
The decision log of each record (#1053) is its own table, readable through the `decision_log` view (Candidate ID, position, status, note, decision date, description digest); the export puts it back inside the record.

`state-database rebuild --state-dir <dir>` reads every collection with the JSON backend's validation, in either layout, checks every record, builds the database in a staging file and only then moves it into place, so a refused record creates no database.
It refuses an existing database unless `--overwrite` is given, and refuses state files whose bytes are not the form the State Store writes (the first export would rewrite them) unless `--allow-non-canonical` is given.
`state-database export --state-dir <dir>` writes the export; `--force` writes the database over a state directory changed outside it.
`state-database cutover --state-dir <dir>` makes SQLite serve the state directory for every command (#1093): it refuses a missing database (build it first) and a directory changed since the last export, writes the export, and only then writes `state-backend.json`.
A rebuild alone never cuts over, so a rebuild can be rehearsed on live state.
`state-database rollback --state-dir <dir>` returns the directory to the JSON backend in one step: it writes the export, so the JSON holds everything the database holds, and then removes `state-backend.json`; `--force` writes the database over a directory changed outside it, as for `export`.
It refuses when the database does not exist, because the JSON could not be brought up to date, and does nothing on a directory that was not cut over.
The database file is kept; from the next JSON write on it is stale, and a later cutover rebuilds it with `rebuild --overwrite`.
Neither command touches Git: the marker, or its removal, is committed like any other state change.
`--database <file>` names the database; otherwise `--config <weekly config>`, `AUTOSCOUT_STATE_DATABASE` or the default chooses it.
All four refuse with exit `6` during a weekly run.
They print `state_database_rebuild=built|refused`, `state_database_export=exported|unchanged|no_database|refused`, `state_database_cutover=cut_over|refused` or `state_database_rollback=rolled_back|not_cut_over|refused`, then the report (`record_count`, `collection_counts.*`, `database_bytes`, `non_canonical` for a rebuild; `layout`, `mode`, `files_written`, `files_deleted`, `change_seq` for an export, and `export=` with the export's status for a cutover or rollback) and `seconds`; a refusal prints `error=` and exits `2`.
The launcher `./scripts/state-database rebuild|export|cutover|rollback` runs them on the weekly state with the weekly config.
The cutover on the run server, step by step, with the checks and the rollback, is `docs/state-store-cutover-runbook.md` (Part 2).

Source onboarding (`add-source`) publishes the accepted Source Proposals through the State Store on both backends (#1093).
Under SQLite the database replacement and its export into the state directory happen in one database transaction that stays open until the onboarding commit is made, so a failed commit rolls the database back while the onboarding restores the files; the commit holds exactly the bytes it fingerprinted, which are the export's.
Onboarding refuses (`state_store_refused`) a state directory whose export is not current with the database, so its commit carries the proposals and nothing else from the state directory.
Its staged validation copy of the repository leaves out `state-backend.json` and keeps its state as the JSON it copied, because the database is not copied.

The doctor and `run-health --state-dir` report the backend, the database's path class, its schema version and whether the export matches the database, read-only (#1076).
The comparison is by content: every exported file against what the database would export.
A mismatch is the doctor's `state-store-finding` with one recommended command (`export`; for files changed outside the export, `rebuild --overwrite` when they equal Git's HEAD or `git restore` when they differ from it, never `export --force` (#1098); or `rebuild` when the database is missing).
The weekly run repeats the comparison right after writing the export and stops with `reason=state_export_mismatch` when the files do not match.

### Editor mutations during a weekly run

Canonical state mutations refuse a live or ambiguously owned repository
workflow guard with exit **6**, `reason_code=weekly_run_in_progress`, and
«Идёт еженедельный поиск. Изменение не сохранено — повторите после его завершения.»
Nothing is written. Previews remain available; repeat the same confirmed
command after the run finishes, without looping or removing the lock.

This covers `record-feedback`, `record-feedback-batch`, `record-visibility`, `ingest-feedback`,
`editorial-profile add/revoke/set-terms`, `source-proposal add/accept/reject/reconsider`,
`merge-candidate-identity`, confirmed `probable-duplicates --dismiss`,
`run-mvp` with `--state-dir`, `generate-descriptions`, `apply-description-backfill`,
`apply-english-title-backfill --confirm`, `refresh-russian-editions --apply`,
`adjudicate-descriptions --apply`, `migrate-candidate-history --apply`,
`shard-candidate-history` without `--dry-run`, `state-database`,
and confirmed `add-source`
(including an explicit proposal file or the default state directory).
Only apply paths are guarded for commands that also offer previews.
No repository, no lock, a provably dead PID, or a lock owned by the calling
process leaves existing behavior unchanged. Dead locks are left for doctor repair.

`probable-duplicates --state-dir <dir>` groups Candidate History by normalized work title and pairs records whose authors are compatible (#855).
Compatible means sharing a surname token (the tokens after the given name, without single-letter initials, credentials, and name particles; a one-word name is its own surname token) or one side naming no author; placeholder author text names nobody.
Two different ISBNs make a pair `possible_editions`; otherwise it is `probable_duplicate`.
Two records with different titles also pair when their authors share a surname and one normalized title is the token prefix or suffix of the other, keeping at least 3 tokens (signal `title_containment`, #916), or when one record's sourced published English title (`english_title_type: sourced_published`) is the other's title (signal `translated_title`, always `possible_editions`); a generated English rendering never links records.
Two records with the same valid sourced published English title also pair (`shared_english_title`), as does a valid confirmed original title matching the other record (`original_title`); both are `possible_editions`.
Same-title records whose authors share no surname are not merge suggestions (often two books): they are listed apart in `author_disagreements` (with `author_disagreement_count`), each with both record summaries and the signal `no_shared_surname`, no correction draft, and no effect on the skip counts.
Each pair reports both records' identity key, durable ID, title, author display, editor status and note, first and last seen dates, sources, and ISBN, its `signals` (`shared_surname`, `same_given_name`, `nickname` from a closed map, `initial`, `different_given_name`, `author_missing`, `isbn_match`, `different_isbns`), `editor_decisions_conflict` (the records disagree on editor status, note, or saved visibility), `conflicting_fields` (every field `merge-candidate-identity` refuses until it is resolved, read from Candidate History and from the latest and scouting candidates and `candidate-visibility.json`, including `content_description`, `description_provenance`, `grounded_description_attempts`, and `visibility`; the report and the merge share one check), and a `correction_draft` in the `merge-candidate-identity --correction-file` shape whose canonical record is only a suggestion, with the rule that chose it in `suggested_canonical_basis`.
Pairs whose durable IDs already redirect to one survivor are skipped, and so are pairs listed in the derived `probable-duplicate-dismissals.json`.
`--dismiss <key> --dismiss <key> --reason "<editor words>"` previews one dismissal with zero mutation (exit `3`); the same call with `--confirmed-by-editor` records it (exit `0`). Input errors exit `2`.
The report writes nothing and has no Git, network, or model side effects; output is sorted and deterministic.

`source-novelty --sample <file> --state-dir <dir> [--run-date YYYY-MM-DD]` checks whether a candidate source lists books AutoScout has not met (#896).
The sample is `{"version": 1, "source_name", "url", "language", "entries": [...]}` with at most 200 entries. Each entry carries `title`, `authors` and `nonfiction`, and may carry `original_title`, `isbn`, `publication_year` and `translated_from`. Unknown keys are refused.
Each entry gets one class, decided in this order:

1. `invalid`: no title, or no author that names a person.
2. `duplicate`: an earlier entry in the sample is the same work, by the rules of `known`.
3. `known`: an ISBN match, or the same normalized title with a shared author. The entry's title or `original_title` is compared with the record's `title` or `english_title`.
4. `out_of_scope`: not nonfiction.
5. `translation`: `translated_from` is set.
6. `stale`: published before the year preceding the run year.
7. `known_author`: an author matches, but no work does.
8. `new`: everything else.

The verdict is `promising` at 5 or more new entries that make up at least 30% of the valid entries, which exclude `invalid` and `duplicate` ones. It is `weak` at one or more new entries, and `no_novelty` otherwise.
The report also counts the editor decisions (positive, already_known, reject, undecided) on the known books.
A state directory without Candidate History is an input error, since every book would read as new.
It writes nothing and has no Git, network, or model side effects. It exits `0` with a report and `2` on an input error.

`source-yield-report [--state-dir <dir>] [--config <file>]` prints one plain-text table per group of sources and changes nothing (#1055): it reads Candidate History through the State Store and the portfolio file, and writes no file, retires no source and edits no configuration. `--state-dir` defaults to `runs/current-weekly-pilot-russian`, `--config` to `configs/current-weekly-portfolio.json`. Exit `0` on a report, `2` on an input error.
A book counts for every source that reported it (the sources in its `source_evidence` and in `earlier_source_names`). Only decided books enter the yield counts. Useful is `interested`, `maybe_watch` or `needs_deeper_analysis`; `already_known` is shown and is not useful. The useful share is useful over decided. "Only here" counts the source's books, decided or not, that no other source reported. First and last book are first-seen dates.
Active sources that meet the proposed rule (at least 50 decided books, at most 2% useful) are marked `meets rule`; those with at least 40 decided and at most 5% useful are marked `close`. The thresholds are the named values at the top of `autoscout/source_yield_report.py` and still wait for the editor's approval. Yearly prize lists, retired sources and sources that appear in Candidate History but not in the portfolio are listed in their own sections with no verdict: the issue's test for a yearly list, no useful book across two editions, needs the editions of one prize linked, and each edition is a separate configured source with no recorded link.

`resolve-review-rows` resolves each returned row in a fixed order: durable Candidate ID, full or short (a redirect recorded by a merge counts as the same identity), then exact identity key (an alias-table key included), then title plus author (every person in the author display must agree), then title alone.
Every review shows the durable ID: the Markdown `ID` column and the HTML card carry its short form, the last eight hex characters, lengthened until unique across Candidate History and the IDs a merge retired; the HTML card keeps the full ID in `data-candidate-id`.
Each row gets one result, `resolved`, `ambiguous` (every alternative with its title, author display, identity key, durable ID, and editor status), or `not_found`, with `matched_by` naming the step that matched.
A step that finds more than one identity is `ambiguous`; so is a row whose durable ID and identity key name different identities.
A title-plus-author match also lists any other identity with the same title under `same_title_alternatives`.
The command prints JSON and writes nothing; exit `0` every row resolved, `1` at least one row needs the editor, `2` an input error.

`record-feedback-batch --decisions <file> --state-dir <dir>` records a returned review file in one two-phase batch (#1153); single-book `record-feedback` stays for conversational feedback.
The file is a JSON list of objects with `identity_key` or `candidate_id` (or both), `status`, `note` (the editor's words verbatim; empty or absent records no note), and the optional dimensions `appeal`, `rights`, `quality_problem` and `info_needed` with the values the `record-feedback` flags take. Any other key is refused.
Every row resolves as `resolve-review-rows` resolves its durable ID and identity key; a book known only from the latest scouting candidates is not found.
Without `--confirmed-by-editor` it writes nothing and exits `3`: `confirmation_required=true`, `batch_rows=`, one `status_count_<status>=` per status, `batch_digest=` (SHA-256 of each row's resolved identity key, status, note and dimensions, in file order), then per row `row=`, `matched_by=`, any `warnings=`, and the lines the single preview prints.
A book decided in more than one row keeps every row, as recorded one by one, and each such row carries `same_book_in_rows=<the other row numbers, joined by +>` in its `warnings=`.
With `--confirmed-by-editor --digest <batch_digest>` it resolves the file again inside one State Store transaction and stages every row with one write of the pending rows, then exports once; it prints one `recorded_feedback` line per row and `staged_rows=N`.
The staged rows are the rows `record-feedback --confirmed-by-editor` stages for the same decisions one by one, in file order.
Every check runs before anything is written, so a refused call creates nothing, not even the state directory or its database.

Exit codes and lines:

- `0`: every row staged.
- `3`: the preview.
- `2`, one `error=` line: the decisions file cannot be read (missing or unreadable, not UTF-8, invalid JSON, text that is not valid Unicode, not a list of objects, or an empty list), as `resolve-review-rows` exits `2` on an input error.
- `1`, `batch_refused=true`, `refused_rows=`, `staged_rows=0`: a row does not resolve (`unresolved row=N status=ambiguous|not_found`, with one `alternative row=N` line per alternative), resolves to a candidate without an identity key (`unstageable row=N reason=candidate_without_identity_key`), or would be refused by the single command (`invalid_row row=N …`: an unsupported status, an unsupported dimension value such as `invalid_row row=N Unsupported rights_availability value: …`, a bare refusal with a dimension, a missing identifier, a non-text value, an unknown key).
- `1`, `error=…`, `batch_refused=true`, `staged_rows=0`: `error=batch_digest_required` (no `--digest`), `error=batch_digest_mismatch previewed=… current=…` (the file or its resolution changed since the preview), `error=batch_already_staged` (the pending rows already end with exactly these rows, whatever day they were confirmed: a repeated batch).
- `1` with `staged_rows=N` above 0, no `batch_refused=true`, and `state_export=failed` on stderr: the rows were staged and only the JSON export after the command failed. Do not blindly repeat the command. Run `./scripts/doctor` and follow its one State Store action (`docs/operator/doctor.md`, "State Store (#1076)"); that action may rebuild the database from the files (`./scripts/state-database rebuild --overwrite`) and drop the staged rows. Then run the preview and the confirmed command again: `error=batch_already_staged` means the rows survived and nothing more is needed; otherwise the batch is confirmed again as usual.
- The export runs after every confirmed call, so a refused call (`batch_refused=true`, `staged_rows=0`) can print `state_export=failed` too; it staged nothing.

The repeat guard (`batch_already_staged`) covers only rows still pending at the end of the pending rows: once `run-mvp` has consumed them, or another row was staged after them, the same batch confirmed again stages its rows again.

`merge-candidate-identity --correction-file <file>` reads one correction artifact with `source_identity_keys` and a `canonical` block (`identity_key`, `title`, `primary_authors`).
`--dry-run` computes the whole plan with zero mutation and prints the canonical identity, title and author display, every merged source identity key on its own line, the merged history record count, the reference rewrite count, the preserved editor status and note, and the canonical description.
The editor note and the description are JSON-quoted, so they stay verbatim and on one line.
Conflicting durable content is never resolved silently.
When the merged records carry differing generated Russian descriptions, the artifact resolves the conflict explicitly:

```json
"description_resolution": {
  "canonical_source_identity_key": "<one of the merged identity keys>",
  "observed_descriptions": {
    "<identity key>": "<the exact content_description the editor was shown>"
  },
  "observed_description_provenance": {
    "<identity key>": { "<the exact description_provenance object the editor was shown>": "..." }
  }
}
```

The named record's `content_description` and `description_provenance` become canonical verbatim, with the provenance retargeted to the canonical identity key so it keeps describing the exact text it was generated for.
The text and the provenance are both pinned, because both are part of what the editor approved.
`observed_descriptions` must name every merged record that currently has a description, repeated verbatim apart from surrounding whitespace.
`observed_description_provenance` must name every merged record that currently has a `description_provenance`, repeated field for field.
A missing, extra, stale, or incompatible entry fails the correction before any state file is written.
A chosen provenance whose own `content_description` names different text is refused.
The dry run then also prints `description_resolution=explicit`, the chosen source identity key, the retained provenance and evidence basis, and every discarded source identity key; a merge that needs no resolution prints `description_resolution=none`.
The merged record records the choice in `description_merge_resolution`, naming the chosen source identity key and every source identity key whose description or provenance the merge discarded.

Different editor decisions on the merged records are resolved the same way (#917):

```json
"editor_decision_resolution": {
  "canonical_source_identity_key": "<one of the merged identity keys>",
  "observed_editor_decisions": {
    "<identity key>": {"editor_status": "<status>", "editor_note": "<the exact note the editor was shown>"}
  }
}
```

`observed_editor_decisions` must name every merged record that has a decision (`editor_status`, `editor_note`, and `feedback_dimensions` when present), repeated verbatim apart from surrounding whitespace, and no record without one.
A missing, extra, stale, or unknown entry, or a chosen record without a decision, fails the correction before any state file is written.
Choosing a decision without an `editor_status` or `editor_note` over one that has it is refused too, because the review would fall back to the discarded value in `editor-facing.csv`.
The chosen record's decision becomes canonical; every other decision is kept verbatim in the merged record's `editor_decision_merge_resolution` (`discarded_decisions`), never dropped.
Snapshot rows that carry a decision show the chosen one, a later merge keeps the decisions earlier merges set aside, and `memo_queue_marker` and `memo-queue.json` follow the chosen status (a work no longer marked `needs_deeper_analysis` leaves the queue).
Editor feedback still pending in `pending-returned-editor-rows.json` for a merged identity, named directly or through an alias of its durable ID, refuses the resolution with zero writes; consume it with `run-mvp` first.
A `suppressed-candidates.json` entry is dropped when the chosen status is neither `reject` nor `already_known`.
The dry run then prints `editor_decision_resolution=explicit`, the chosen source identity key, and one `editor_decision_discarded=` JSON line per decision it does not keep; a merge without the block prints exactly what it printed before.
After any merge, a current or latest snapshot row labelled `new` is relabelled against the oldest merged record first seen before it, exactly as the pipeline labels a row against its prior history, so a row folded into an earlier work no longer appears among new books.

The merge keeps the durable Candidate ID of the canonical identity and redirects every Candidate ID it retires onto that survivor in `candidate-identity-aliases.json`, through the same append-only redirect the approved durable-merge path uses.
Stored aliases are never rewritten, so a later capture under an old title resolves through the redirect to the surviving record and still finds the editor's decision.
The dry run prints `survivor_candidate_id` and one `retired_candidate_id` line per retired ID.
Every surviving reference to the canonical identity is projected onto the merged history record, so a single retained row cannot keep an old title, durable ID, or description under the new identity key; a row that never had a field does not gain one.
The description-attempt logs of all merged records are combined. Two different attempts that claim one `operation_id` are a conflict: with a `description_resolution` the attempt of the record it names in `canonical_source_identity_key` is kept, and every other attempt for that operation moves to `description_merge_resolution.discarded_description_attempts` with its `source_identity_key` (#1023). The dry run prints one `description_discarded_attempt=` JSON line per attempt set aside, and a later merge keeps the attempts earlier merges set aside. Snapshot rows in the latest and scouting candidates show the merged history record's attempt log. Without a `description_resolution`, or when the chosen record has no attempt of its own for the conflicting operation or carries two different ones, the correction still refuses. A `description_resolution` must choose a record that has a description, so two records that both lack one and conflict only in their attempts still cannot be merged.

A reference never keeps provenance for text it no longer shows: when the chosen description has no provenance, the retained reference loses its own instead of describing the new text with the old evidence.

Planning and publication run under one lock on the state directory, not on the planned file set, so two corrections that plan different files still exclude each other.
A correction therefore always plans from state that no other correction is midway through replacing.
A dry run takes the same lock, so a preview never straddles another correction's publication.
Every planned file is then published as one bundle: all of them are staged and backed up before the first replacement, and a failure part way through restores every file byte for byte.
A merge that fails therefore leaves state exactly as it was, and the same correction can be run again once the cause is fixed.
Write failures are reported as `error=` with exit code 1, like refused corrections.

`migrate-candidate-history --durable-merge-approval <file>` previews or applies one explicitly approved pairwise durable-ID merge through the same migration write path.
The approval file names `canonical_identity_key`, `survivor_candidate_id`, `retired_candidate_ids`, and `expected_members`.
`--emit-merge-approval <file>` writes that approval template for the single pending merge on a dry run.
The dry run shows every affected ID and key, the survivor and why it was chosen, preserved editor decisions, conflicts, and stored versus effective alias effects, and writes nothing.
The `--apply` run refuses invalid, stale, or changed approvals before writing anything.
Stored aliases keep pointing at the retired IDs they were assigned to, while effective resolution follows the recorded redirect to the survivor.
Conflicting editor decisions are reported and never silently resolved.
A repeated approved merge changes nothing the second time.

`editorial-profile add --exception-term <term>` (repeatable, topic entries only) keeps a candidate that matches the term outside the entry, even when a `--match-term` matches it too.
In both options, a word ending in `*` matches every word that starts with it (stem of at least four letters), and a term wrapped in double quotes matches its words only when they are adjacent and in order.

`editorial-profile set-terms` supplies explicit matching terms for one existing active topic entry that has no terms yet.
The command is two-phase like `add`.
The preview run without `--confirmed-by-editor` prints the entry facts, the deduplicated requested terms, the effect, and current candidate matches, mutates nothing, and returns exit code 3.
The confirmed run updates the same entry in place, preserving its `verbatim_quote` and `provenance` byte for byte, and returns exit code 0 when the terms are applied.
Exit code 1 reports profile or entry eligibility failures such as a missing, revoked, or non-topic entry, or an entry that already carries explicit terms.
Exit code 2 reports invalid term input or CLI usage errors.

Operational how-tos (refresh cadence, readiness review, diagnostics, and the monthly source discovery nudge) live in `docs/operator/capture-and-extraction.md`, `docs/operator/doctor.md` and `docs/operator/sources.md`, indexed in `docs/current-weekly-operator-workflow.md`; source onboarding follows `docs/source-onboarding-runbook.md`.

Source-gap discovery is signal-family-only.
It reports no country, market, or language analysis.
`./scripts/add-source discover` needs no URL and accepts optional `--signal-family`.
The Search Optimization Review is optional.
Without `--review`, discovery reads `runs/current-weekly-pilot-russian/search-optimization-review.json` only when it exists.
When it is absent, configured sources are labelled `not-reviewed` and no allocations are used.
Use `--review <path>` as an explicit override with strict file and schema validation.
Discovery resolves `weekly_portfolio` references relative to the supplied `--portfolio` path before validation and analysis, including overlay active state.
Unclassified sources are surfaced once in the top-level `sources_without_signal_family` list with deterministic reasons.
Offline discovery returns a report with top-level status `success` and exit code 0 on success.
Status `insufficient_metadata` means family metadata is not sufficient for exact coverage, so weakness conclusions, candidate proposals, and probing are suppressed, and it also returns exit code 0.
Input and configuration failures use status `input_error`; probe failures, including structured live-probe exit code 2, use status `probe_error` and exit code 2.
Unsupported discovery options such as `--url` are input errors and return exit code 2.
Offline discovery stops after its report and never probes.
Run `./scripts/add-source discover --probe` only when the editor explicitly asks to probe the remaining candidates.
The live mode computes coverage, configured URL filtering, and ledger exclusions before invoking the existing `./scripts/probe-source` launcher.
It passes the stable `--keep-captures tmp/source-candidate-captures`, `--ledger data/source-candidate-ledger.json`, and `--feasibility-dir data/source-feasibility-drafts` arguments.
Retained captures stay outside Git publication in the ignored `tmp/source-candidate-captures` directory.
Live mode may write only the existing probe ledger and Markdown sidecar, retained accepted captures, and feasibility drafts.
Protected configuration and weekly/editorial state remain unchanged.
It propagates exit code 0 when any candidate accepts, exit code 1 when no candidate accepts and a reject or malformed result exists, exit code 2 for discovery, probe, command, input, or storage errors, and exit code 3 when all candidates defer.
If no candidates remain after filtering, live mode returns exit code 0 with a clear no-candidates status and makes no network call.
The network authorization rationale is exactly «Для проверки доступности предложенных источников нужен сетевой доступ. Разрешить запуск?».
After either reporting or probing, onboarding must wait until the editor explicitly chooses exactly one candidate, and that candidate must be accepted.

## Run artifacts

A pipeline run writes these files to its output directory.

- `tmp/mvp-run/editor-facing.csv` - the first editor-facing weekly report interface.
- `tmp/mvp-run/source-mentions.json` - configured-source discovery output, one metadata-focused mention per eligible source signal.
- `tmp/mvp-run/scouting-candidates.json` - work-level Scouting Candidates after eligibility screening, Duplicate Candidate merging, signal aggregation, Primary Signal selection, and A/B/C/D priority labeling.
- `tmp/mvp-run/internal-exclusions.json` - internally excluded records with identity, reason, evidence, and exclusion date.
- `tmp/mvp-run/candidate-history.json` - persisted candidate history with editor status, raw editor note, and tracking markers.
- `<output-dir>/editorial-fit-shadow/scores-<run-date>.json` - advisory `fit-v2` shadow scores, one record per editor-facing candidate with the answer distributions, the composite, and a reason code for every failed call (Issue #725).
- `<output-dir>/editorial-fit-shadow/backlog-scores-<run-date>.json` - advisory `fit-v2` scores of undecided books of earlier runs that had no score; read by the review only, never by the shadow report or the ranking benchmark.
  `report.json` and `report.md` beside it compare the scores with the editor's later decisions.
  Nothing reads these files back into the pipeline.
- `<state-dir>/candidate-visibility.json` - reversible, audited editor Hide/Remove and Restore/Show decisions; hidden candidates remain in Candidate History.
  This is state, not a run-output artifact.
- `tmp/mvp-run/proposed-corrections.json` - returned-sheet edits to generated facts that need explicit acceptance before changing candidate facts.
- `tmp/mvp-run/memo-queue.json` - future memo/research queue markers for `needs_deeper_analysis` decisions.
- `tmp/mvp-run/suppressed-candidates.json` - rejected or already-known candidates hidden from the weekly CSV when no Material Change occurred.
- `tmp/mvp-run/hidden-by-rule.json` - candidates hidden from the editor-facing review by active Editorial Taste Profile hard rules.
  Each entry records the identity key, title, authors, the rule ID that triggered the hide, and the editor's verbatim quote.
  It covers the candidates of this run only.
- `tmp/mvp-run/hidden-from-review.json` - every card `render-review` left out of the cumulative review (#1025), current or from Candidate History.
  Each entry records the identity key, durable Candidate ID, title, authors, editor status, `hidden_by` (`hard_rule`, `retired_source` or `decided_other_edition`), the reason, and the rule ID with the editor's verbatim quote, the `retired_sources` names, or the `decided_records` whose decision settled the book.
  The file appears with the first withheld card and is rewritten on every later render, to an empty list when none is withheld.
  A card is withheld for retirement only when every source in its evidence and in its `earlier_source_names` (the sources of earlier runs, kept when a weekly snapshot replaces the evidence) is withdrawn.
  A source withdraws its cards when its configuration entry is retired (`active: false`, `lifecycle_status: "retired"`) and carries `retired_cards_hidden: true`.
- `tmp/mvp-run/report-qa.json` - report-level technical QA such as maintainer review, kept separate from candidate editor status.
- `tmp/mvp-run/russian-edition-checks.json` - configured Russian-edition check runs plus one auditable candidate-level Russian verification record for each discovered candidate when checks are configured.
- `tmp/mvp-run/russian-review-queue.csv` - a focused manual review queue for candidates whose automated Russian-edition status is `unclear`.
- `tmp/mvp-run/run-summary.json` - skipped, failed, and ignored source counts, and the per-outcome `capture_source_outcome_counts` (including `not-due`); raw candidate count; excluded candidate count; editor-facing count; `row_cap_dropped_count` (number of eligible editor-facing rows discarded by the `pilot_row_max` cap); `hidden_by_rule_count` (number of candidates hidden by active hard rules); `update_delta_counts` (breakdown of `new` / `upgraded` / `changed` / `unchanged` among visible candidates); shortfall reason; and major warnings.
- `tmp/mvp-run/source-metrics.json` - per-source contribution metrics over a trailing window: unique finds, corroboration, and editor yield.
- `tmp/mvp-run/source-review.md` - the rendered source-review table for a keep / retire / promote conversation.
  Nothing is retired automatically from these metrics.
- `tmp/mvp-run/source-proposals.json` - Source Proposals awaiting an explicit accept or reject.
  A proposal never becomes a configured source on its own.
- `tmp/mvp-run/derived-rule-suggestions.json` and `tmp/mvp-run/derived-rule-suggestions.md` - rejection patterns observed in editor feedback.
  They are inert until the editor promotes one through the Editorial Taste Profile commands.
- `tmp/mvp-run/internal-pre-suppression-candidates.json` - the candidate set before suppression is applied, kept so a suppression decision can be audited.
- `tmp/mvp-run/source-runs.json` - source-level status, counts, skipped or failed reasons, and safe debug metadata.
  A `not-due` row is an annual or monthly source the weekly run did not collect tonight; it carries `next_due_date`, `carried_mention_count`, `carried_from` (`recorded-list`, `previous-output` or `nothing`) and `carried_list_date`.
- `scheduled-source-lists.json` in the state directory - the last list each annual or monthly source reported, keyed by source name with its `collected_on` date (#1057).
  Only a collection that reported mentions replaces a source's list; a failed or empty one keeps it.
  The pipeline carries a not-due source's books from this list.

The MVP skeleton only reads configured sources and fixture data.
Sources marked `paid`, `blocked`, `fragile`, `unavailable`, `manual import`, or otherwise unsuitable are reported as Skipped Sources and are not collected from.
Sources with `active: false` are recorded as inactive and are not collected from.

Configured discovery families that can create source mentions are:

- `award`
- `bestseller/ranking`
- `media/curated list`
- `reader-interest`
- `audiobook ranking`
- `rights/deal`

General web search and topic search cannot create source mentions or Scouting Candidates.
They may be represented later for enrichment or checking after a configured source has already discovered a candidate.

## Source configuration

Each configured source supports:

- `name`
- `family`
- `url`
- `active`
- `access_status`
- `extraction_method`
- `check_frequency`
- `notes`

The weekly run collects a source whose `check_frequency` is `annual` or `monthly` only on the collection weekday (#1057).
The weekday is Sunday unless the top-level `collection_weekday` names another (`monday` … `sunday`); any other value fails validation.
A standalone `prepare-codex-extraction` and `check-sources` collect every active source whatever the day.

For this bootstrap slice, open fixture sources may include `fixture_candidates`.
No live network discovery is performed yet.

BookPage, NPR, and Nonfiction.fr have source-specific collector boundaries.
BookPage supports deterministic metadata-only inputs with `extraction_method` values `bookpage-recorded-response` and `bookpage-source-page`.
NPR supports deterministic source-page fixtures with `extraction_method: "npr-source-page"`.
Nonfiction.fr uses `nonfiction-fr-rss-replay` for fixtures and `nonfiction-fr-rss-live` for opt-in collection: it reads only unseen RSS GUIDs, visits same-domain review pages, keeps only complete title/author/publisher/year metadata, and persists GUID/date state without article or review prose.
These source-page modes parse source-shaped fixtures into Source Mentions while default tests stay offline.
Opt-in live collection uses `enable_live: true`; it is off by default and bounded to the configured source URL.

Recorded trial sources may instead reference deterministic metadata captured from real configured source pages:

- `recorded_mentions_file`
- `recorded_mentions_files`

Recorded Source Mentions live outside source configuration.
They are validated before report generation and must include title, primary author(s), source URL, source/check date, signal type, Signal Strength, and Evidence Note.
Recorded Russian-edition statuses, when present, must be `not found in Russian`, `found in Russian`, or `unclear`.
Recorded Visible Risk statuses, when present, must be `no visible risk`, `text check required`, or `clear visible risk`.
A legacy recorded `not found in Russian` value is normalized to `unclear` when it has no verified positive evidence or complete no-match verification.
`content_description`, when present, must be original Russian scouting prose written by AutoScout or a human reviewer, regardless of the source language; it must not store copied publisher blurbs, jacket copy, reviews, article bodies, book samples, or long excerpts.

Each source mention includes:

- `title`
- `primary_authors`
- `candidate_category`
- `publisher_fit`
- `russian_edition_status`
- `russian_edition_url`
- `visible_risk_status`
- `visible_risk_category`
- `visible_risk_comment`
- `public_data_status`
- `short_description`
- `content_description`
- `risk_evidence_url`
- `risk_signal_scope`
- `publication_status`
- `english_title`
- `english_author_display`
- `english_annotation`
- `search_translation`
- `author_transliteration_note`
- `source_url`
- `source_name`
- `source_family`
- `source_check_date`
- `signal_type`
- `signal_strength`
- `evidence_note`

Audiobook rankings create source mentions only when the fixture item maps to an underlying `book` or `work`.
Rights/deal mentions from open sources are preserved when available, but missing open rights/deal mentions must not be interpreted as proof that no rights activity exists.

## Candidate eligibility and deduplication

The MVP converts source mentions into work-level Scouting Candidates.
Obvious Duplicate Candidates are merged by normalized title and primary author while preserving all source evidence.
An extracted author value that is placeholder or refusal text (for example «Не указан в HTML», `not stated`, `unknown author`) is not admitted as an author (#852).
The match is against the whole value only, from the closed list in `autoscout/author_admission.py`, so a real name containing a listed word is unaffected.
A row left with no author follows the source's author-optional rule: an author-optional source admits it with no author, any other source drops it with reason `placeholder_author` in the source run's `drop_reasons`.
Admission also turns each author value into the people it names (#854), before any identity key exists: a spaced slash separates people (#756); a value with two or more commas lists people (`Joseph Grenny, Kerry Patterson, Ron McMillan`); contributor tails (`and others`, `et al.`, `u. a.`, `y otros`, `e altri`, `и др.`) are dropped; honorifics and credentials (`Dr.`, `Dr. med.`, `med.`, `Prof.`, `MD`, `M.D.`, `PhD`, `Ph.D.`) are stripped, and a value that is only a credential names nobody (an extracted row naming nobody drops with reason `no_person_author` unless the source is author-optional).
One comma and no tail stays one surname-first name (`Bildner, Jim`), as does a value whose extra comma only sets off a credential or a generational suffix (`Connick, Harry, Jr.`); one comma before a tail lists people only when both sides are several words long.
Corporate co-credits such as `Higher Ground` stay as given.
`normalize_author_display` and the identity key formula are unchanged, so Candidate History keys do not move; `scripts/replay-history-identity-keys` re-admits every history record's stored title and authors and lists each record whose recomputed key differs from its stored key (read-only).
Every admitted person must be named somewhere in the capture the row was extracted from (#850), for every source: the raw HTML (visible text, attribute values, embedded script data), any other retained capture field such as a structured text body, or the entry's evidence URL slug.
A person matches as a whole normalized phrase, accent-free and case-insensitive, in printed, given-first, or surname-first order; a row naming a person the capture does not drops with reason `author_not_in_capture`.
The check runs after the source contract and the evidence URL check, so their drop reasons come first.

Multiple links from the same source family are preserved as evidence but count as one Independent Signal.
Multiple source families increase the Independent Signal count.
The Primary Signal is the strongest signal by qualitative Signal Strength; tied strengths keep the signal that caused entry first.
Editor-facing rows include a Signal Summary and an A/B/C/D Priority Label derived from Independent Signal count, Signal Strength, Publisher Fit, Russian-edition status, and Visible Risk status.
An incomplete Russian-edition verification is `unclear` and cannot support the strongest priority label that requires a verified negative.

The editor-facing CSV is a stable external contract with exactly these columns, in order: `Title`, `Author`, `Annotation`, `Description`, `Description status`, `Signal count`, `Visible-risk status`, `Risk comment`, `English title`, `English author display`, `English annotation`, `Check date`, `Source`, `Source URL`, `Publisher / year`, `Category / topic`, `Why it entered the list`, `Signal summary`, `Priority label`, `Russian-edition status`, `Russian-edition URL`, `Links`, `Update delta`, `Lifecycle`, `Review context`, `Taste profile notes`, `Editor status`, `Editor note`, `Candidate ID`.
`Candidate ID` holds the identity key (`title|author display`), which changes when the author display changes.
A `Durable Candidate ID` column follows it with the durable `candidate-…` ID (#851); a CSV written before it is still read as valid.
Optional description and prominence columns come after it, only when a row uses them.
It contains one Weekly Report Row per visible Scouting Candidate.
`Review context` carries cumulative suppression, rule, and exclusion explanations; it is separate from the bibliographic `Annotation`.
English helper fields are editor-facing aids for Non-English Candidates and do not overwrite original candidate metadata or become confirmed edition metadata.
The `Annotation` column is synthesized from short metadata, Evidence Notes, signal context, and public-data status; the `Description` column contains original Russian `content_description` scouting prose when available, regardless of source language; it must remain bounded for spreadsheet use and must not store copied source bodies, full reviews, publisher descriptions, or book samples.
Richer Russian content descriptions are useful for every visible book, English and non-English; Non-English Candidates may include original-market context when available while Search Translation and Author Transliteration Note values remain search notes only.
Operational health, skipped/failed sources, shortfall reasons, and internal exclusion reasons stay in JSON run artifacts rather than extra CSV columns.

The `Update delta` column preserves the raw deterministic update classification: `new` (never seen before), `upgraded` (priority label improved or new Independent Signal family), `changed` (a definitive Russian-edition status or Visible Risk status changed, or another non-priority-and-signal material change occurred), or `unchanged` (no material change from the previously seen state).
Operational normalization from a legacy Russian-edition negative to `unclear` does not create a material status change or resurface a suppressed candidate.
The separate `Lifecycle` column is the editor-facing vocabulary: `New`, `Updated`, or `Previously seen`; it does not overwrite `Update delta`.

The `Taste profile notes` column surfaces explanations from the Editorial Taste Profile's soft preferences that nudged a candidate's priority label.
It is empty when no soft preferences matched or when no profile is configured.
It records the specific preference entry ID, direction (positive/negative), and the editor's verbatim quote for each applied nudge.
Clear Visible Risk hard floors (D) are never overridden by soft preferences.

Russian-edition MVP checks are scoped to configured retailer checks for `Ozon`, `Labirint`, `LitRes`, `Chitai-gorod`, and `Book24`, plus opt-in `russian-library-catalog` checks such as RSL.
Russian publisher-site checks are logged as deferred and do not affect MVP Russian-edition status.
Each Russian-edition check accepts the boolean `active` key.
`active: false` disables the provider, so the check is not run and remains operationally incomplete rather than contributing no-match evidence.
An explicitly declared unknown retailer or provider remains in required coverage with the outcome `unimplemented`.
Russian publisher-site checks remain deferred and are not part of required coverage.
Status values are `not found in Russian`, `found in Russian`, and `unclear`.
Found in Russian requires same-work Russian-language evidence with a matching URL; imported foreign-language listings do not count.
Ambiguous Russian matches resolve to `unclear` and are written to `russian-review-queue.csv`.
An empty, unscoped, blocked, failed, or unimplemented provider result is operationally incomplete and resolves to `unclear`; it must not be presented as `not found in Russian`.
A definitive `not found in Russian` result requires complete configured-provider coverage and a candidate-scoped no-match outcome.
RSL catalog evidence is bibliographic evidence, not current commercial availability, and complements rather than replaces retailer checks.
Generated Search Translation and Author Transliteration Note values are stored only as search notes and query attempts; they do not overwrite candidate display metadata.
Run summary includes Russian-check skipped, failed, unclear, and verification-breakdown counts.

Each candidate-level `russian_edition_verification` record stores these operational fields:

- `check_coverage`: `complete`, `partial`, `not_run`, or `failed`.
- `provider_outcome`: `matched`, `no_match`, `ambiguous`, `blocked`, `unimplemented`, or `failed`; null means that no single closed outcome exists.
- Provider execution status: `checked`, `not_run`, `skipped`, `blocked`, `failed`, or `unimplemented`.
- `confidence`: `high`, `medium`, or `low`.
- `checked_sources`, `checked_at`, `query_timestamp`, `evidence_urls`, `providers`, and a bounded `reason`.
- Each `providers` entry records the provider name, source type, status/outcome, bounded reason, result count, check time, evidence URLs, and metadata-only evidence.

A provider execution status of `not_run` means that no candidate-scoped provider execution supplied evidence.
A provider execution status of `skipped` means that configuration prevented the check from running.
The aggregate `provider_outcome` is null when incomplete provider execution prevents one closed outcome.
Empty fixtures and configured provider results that do not cover the candidate are not no-match results.
Positive evidence and an explicit complete candidate-scoped no-match remain unchanged.

Visible-risk MVP screening uses public metadata and Evidence Notes only.
It is limited to Russia/politics, war/anti-war/Russian Armed Forces, WWII/USSR/Nazi comparison, non-traditional relationships/gender transition/rejection of childbearing, and drugs/microdosing/psychoactive substances.
Status values are `no visible risk`, `text check required`, and `clear visible risk`.
`No visible risk` means no configured MVP signal was found in available public materials; it is not legal clearance.
Weak or unverified author/context signals use `text check required`.
Credible book-content signals with public evidence stay in the CSV with a lower priority label and a concise review comment.

Returned editor feedback can set only editor feedback fields authoritatively: `interested`, `maybe_watch`, `reject`, `already_known`, and `needs_deeper_analysis`, plus the raw editor note.
Raw notes are preserved exactly.
Returned edits to generated facts such as title, author, Russian-edition status, Visible Risk status, or priority are stored as proposed corrections unless explicitly accepted by history.
`interested` and `maybe_watch` preserve tracking without creating memo work; `needs_deeper_analysis` creates a future memo queue marker.
`reject` and `already_known` suppress normal redisplay unless a Material Change occurs, such as a new Independent Signal family, stronger Signal Strength, Russian status change, Visible Risk status change, accepted correction affecting identity/status, or explicit reset.
Same-family duplicate links and minor metadata enrichment are not Material Changes.

Hard exclusions such as fiction, children/YA, topic-only finds, narrow manuals, narrow academic books without mass-market signal, and `found in Russian` records are written to `internal-exclusions.json` instead of the editor-facing CSV.
Borderline adult nonfiction remains visible with lower `publisher_fit` rather than being hard-excluded.

Normal records and logs should store URLs, concise metadata, Evidence Notes, status, counts, and safe debug metadata.
They should not store full page bodies, article text, full reviews, copied book samples, or long excerpts.


## PW Digital Library issue pass (#954)

For PDFs already downloaded to this machine, `./scripts/stage-pw-pdfs`
accepts repeatable `--archive <zip>`, `--year <year>`, and `--output <file>`.
It works offline and parses the Nonfiction Reviews with the same page parser
as the subscriber pass. Use an output path under the Git-ignored
`data/pw-precollection/`. It records bibliographic rows, issue dates, counts,
and parser rejection reasons; it retains no PDF or review prose. A PDF without
a resolvable issue date or Nonfiction Reviews rows is counted as skipped. The
staged file is separate from `pw-issue-collection.json`; staging does not make
books appear in the next weekly review. Review the issue and rejection counts
before planning a controlled import, since a year of reviews can overwhelm a
single weekly run.

`./scripts/describe-pw-pdfs` prepares original Russian descriptions from
those same local PDFs before a weekly run. Pass the archives again with
`--staged data/pw-precollection/local-backfill-2026.json`,
`--output data/pw-precollection/pw-local-descriptions-2026.json`,
`--port <local-port>`, and `--model <local-model>`; `--limit N` bounds one
batch, and repeating the command resumes accepted descriptions. Pass
`--history-file runs/current-weekly-pilot-russian` (a state directory, in
either Candidate History layout, or a single history file)
to skip books that already have a usable description, and
`--exclude-subsection "Food & Cooking"` to skip the current PW source's
excluded subsection. The model
must serve an OpenAI-compatible chat-completions endpoint on this machine.
The adapter connects only to `127.0.0.1`, makes no search or remote model
call, and refuses to start when no local server listens. Only the review for
one ISBN is sent per request. The PDF and review text remain transient;
the Git-ignored result holds the validated original description, exact book
identity, model name, and a review SHA-256 digest. Unusable answers, copied
eight-word spans, and descriptions below the existing depth floor are not
stored. The live PW collector reads this optional result, verifies its book
identity against the bibliographic collection, and offers matching text to
the pipeline as a grounded description. A book already carrying a valid
description keeps it. The product rule changed at the editor's request on
2026-11-09: PW may supply a locally prepared original description, while raw
or unapproved review text remains forbidden in Source Mentions and state.

A rejected draft goes back to the model with the reason, for at most three
attempts. `--language en` writes English descriptions with a second self-check
call. `--provider deepseek --allow-review-text-upload` replaces the local model
with the DeepSeek API (`--port` is then unused; the key comes from
`DEEPSEEK_API_KEY` or the login keychain item `autoscout-deepseek-api-key`).
That provider sends review text off this machine under the exception recorded
in `docs/agents/contracts/pw-precollection.md`; a rejected key or an empty
balance stops the run. With either provider, a run in which the model
never answers (five failed books before any answer, or every book of a smaller
batch) stops with `pw_model_never_answered` instead of reporting a finished run.

`./scripts/import-staged-pw --staged <file> --collection <file>` previews the
next four unimported issues by default. `--max-new-issues N` bounds a batch
to 1–10 issues; `--apply` atomically adds only that batch to the Git-ignored
PW bibliographic collection. Already collected issue dates are never changed.
The preview reports ISBN-set disagreements with them; the known 2026-10-12
disagreement remains a separate correction task. Review the batch count and
prepared-description coverage before applying and starting the weekly run.

To add the local archive straight to the main Candidate History, without any
weekly collection, use `./scripts/backfill-pw-history --staged
data/pw-precollection/local-backfill-2026.json --descriptions
data/pw-precollection/pw-local-descriptions-2026.json`. This is an offline,
read-only preview. It uses the configured PW subsection exclusions, the same
Source Mention and candidate builders as the normal pipeline, and reports new
books, books already in history, unresolved ISBN identity conflicts, and
missing descriptions. A conflicting ISBN is deferred, including one marked
`isbn_discovery_only` in existing history; it is never used to merge identities
silently. `--apply` adds new books with validated original descriptions and
fills missing descriptions on clearly matched existing books. Books without a
prepared description remain pending, so repeated applies can follow bounded
local-model batches. It assigns historical issue dates as first/last seen for
new records, appends PW citations to matched existing records, preserves their
editor decisions, and rerenders the cumulative
review. It does not
capture sources, run a model, change the PW seen index, or alter current weekly
candidate snapshots. Its new descriptions and source metadata enter the
canonical state only for books whose descriptions are ready. The import never
merges existing identity records or replaces a valid or editor-corrected
description.

Each record the backfill adds keeps an `archive_backfill` marker: the batch
(`pw-archive-<year>`) and the day it was applied. The review shows the marked
books that have no editor decision as their own block, for example
«Архив PW 2026», placed among the review's dates after the books added on the
day the batch was applied: one group in the HTML timeline («Все» and «Без
решения») with the number left to decide, the block's name in the «Архив» column of the Markdown, and an
`Archive batch` column in `editor-facing.csv`. The «Добавлено» column and the
review's dates use the day the batch was applied, so one batch is never spread
over the dates of its issues; the card keeps the issue date on its «Архив»
line. A weekly run leaves the marker and the block alone. Once the editor
decides on a book it moves under the date the batch was applied and keeps the
marker. Hard Rules and editor hides apply as for any other card.

A batch applied before the marker existed is marked once from Git with
`./scripts/mark-archive-backfill --before-ref <rev> --after-ref <rev> --batch
<name> --applied-date <YYYY-MM-DD>`. The batch is every identity key Candidate
History holds at `--after-ref` and not at `--before-ref`. The default is a
read-only preview with counts; `--apply` writes only the marker and rerenders
the review, and refuses with `weekly_run_in_progress` (exit 6) during a weekly
run. A record that was already in history and only gained evidence from the
archive is counted and left unmarked: it entered through a weekly run on its
own date. The 2026 PW archive was marked with `--before-ref 5e9d5ef6^1
--after-ref 5e9d5ef6 --batch pw-archive-2026 --applied-date 2026-11-11`.

Install the optional PDF reader in the project's environment with
`.venv/bin/python -m pip install -e '.[pw]'`, then run
`./scripts/collect-pw-issue`. The core install and other commands need no
PDF reader. Missing pypdf returns `pw_pdf_reader_missing`.

The pass reuses the PM read-only IMAP adapter and credentials
(`AUTOSCOUT_PM_IMAP_USER` / `AUTOSCOUT_PM_IMAP_PASSWORD`, or the existing
`~/.config/autoscout/pm-mailbox.env`). Its declaration is
`configs/pw-issue-pdf.json`, folder `PW`. Direct deliveries and manual
forwards from the declared editor are accepted; forwards require PW's
original sender in the body. The editor's address is not in the repository:
set `AUTOSCOUT_PW_FORWARDING_SENDER` in the environment or the key file, and
without it only the vendor's sender is read. Each pass collects every issue not yet
collected whose email is within the declared age, oldest first and at most
`max_issues_per_pass` (4); the rest are reported as `issues_pending` for the
next pass. Copies of one issue are read once. BookLife's email, sent from the
same address under the same subject, is told apart by its From name and ignored;
when an issue email links to more than one issue, links to issues already
collected are dropped (#1117). `./scripts/collect-pw-issue
--backfill` ignores the age and reads the whole PW label, once, for the first
run (#963). "Not yet collected" is AutoScout's own record of issue dates:
the mailbox stays read-only and Gmail's unread flag is not used.

Each issue is saved before the next is downloaded, so one failed issue keeps
the others. A failure that a retry cannot change (`pw_pdf_too_large`,
`pw_issue_date_mismatch`, `pw_issue_link_missing_or_ambiguous`,
`pw_original_sender_missing`, `pw_page_unreadable`) is recorded under `skipped_issues` and not
fetched again; delete its entry to retry it. A `pw_pdf_too_large` skip records
the cap it failed under and is retried by itself once `max_pdf_bytes` is raised. A
`pw_issue_link_missing_or_ambiguous` skip records the link rule it failed under
(`link_rule`) and is retried by itself once the rule changes. Other failures, including an
email that cannot be read, fail that issue alone: it is listed under
`retry_issues`, a queue retried after the new issues with the longest-waiting
issue first; an issue that fails again moves to the back, so it holds back
neither new issues nor other retries, and it stays reachable after its email
ages out of the window.
`pw_pdf_reader_missing` stops the pass. So does `pw_subscriber_link_rejected`,
PW refusing the subscriber link or redirecting it to its sign-in page: every
further issue would be refused the same way, and each attempt opens one more
PW device session, of which the account allows only a few (#956). The issues
not attempted, whether the pass stopped early or reached its per-pass cap, are
listed under `untried_issues`: they keep their place among the new issues and
stay reachable after their emails age out of the window. A malformed link in the email itself is
`pw_issue_link_missing_or_ambiguous`, never a refusal. The status is
`collected`, `partial` (some issues failed, or the pass stopped early, named in
`stop_reason`), `no_new_issue` or `failed` (every attempted issue failed, exit
code 2). Only one pass runs at a time: a second refuses at once with
`pw_pass_in_progress` and writes nothing.

`./scripts/run-weekly` starts the same pass itself before the source check, on
every run (#956), so the launcher is for a refresh outside a weekly run. A week
with no new issue email opens no subscriber link. The weekly pass starts no new
issue after 15 minutes (`PW_PASS_RUNTIME_BUDGET_SECONDS`); one already started
finishes, and the rest stay pending. No outcome of it stops the run.

Only archive issue links are selected, and redirects use an in-memory
cookie jar and a fixed HTTPS host allowlist. Login redirects, rejected
subscriber links and issue-date mismatches stop the pass without a
workaround. The PDF has a 400 MB cap (`max_pdf_bytes`, at most 500 MB; older issues reach
about 270 MB) and is deleted from its temporary
directory after success or failure.

An explicit Nonfiction running header starts a bounded section. A section
opener page prints only the generic `Reviews` logo and no running header; it
belongs to the section of the next headed page at most two pages ahead, and
stays unclassified when another opener or a paid page comes first. Pages
continue within a section until a different section header, a new generic
`Reviews` opener, or a paid/BookLife section. Entries may cross page breaks
within the bound. Some issues (12 and 19 Oct 2026) print no running header on
any review page; there an opener's block, up to the next opener, paid page or
running header, is Nonfiction when it prints a Nonfiction-only subsection
heading (such as Food & Cooking or Religion & Spirituality) and no Fiction or
Children's heading. A page the PDF reader cannot extract is read as empty when
it lies outside the Nonfiction section. Inside it, right before it, or where its
opener may have been (judged by the same opener rules), a review may be lost or
cut in half, so that issue fails as `pw_page_unreadable`.
It is recorded once and not fetched again; removing its entry from
`skipped_issues` retries it, for example after a pypdf upgrade. Only all-capital lines count as subsection evidence,
as PW prints them, so a book titled "Fiction" is not one. Ambiguous entries are counted as rejections.
Paid/BookLife and self-published reviews are excluded.

Results and attempt counts live under Git-ignored `data/pw-precollection/`. Rows are keyed by checksum-validated
ISBN-13. Completed issue dates are not fetched twice. Full reviews,
subscriber links, tokens and cookies are never retained or printed.
Failures return exit code 2 with a named reason. Output contains counts
and issue dates, with no titles. This standalone pass does not yet introduce Scouting
Candidates (#955) or participate in the weekly workflow (#956).

