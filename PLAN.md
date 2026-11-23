# AutoScout — Plan & Progress

This is a plain-language checklist of what AutoScout does today and what is still planned.
It is written for Dima and the editor, not for the tool itself. The agent only refreshes it
from time to time — it is a status picture, **not** a set of instructions for the agent.

**What AutoScout is:** a weekly assistant that scans many book sources, filters out the noise,
and hands the editor a short, reviewable list of foreign adult-nonfiction books that might be
worth acquiring for the Russian market — with evidence for why each book is on the list.

`[x]` = working now &nbsp;&nbsp; `[~]` = partly working &nbsp;&nbsp; `[ ]` = planned / not built yet

---

## 1. Finding books (source scanning)

- [x] Read a configured list of sources (no random web crawling)
- [x] Source families that can surface a book: awards, bestseller/ranking, media & curated lists, reader-interest, audiobook rankings, rights/deal mentions
- [x] Skip paid / blocked / fragile sources on purpose, and record *why* they were skipped
- [x] Turn off a source without deleting it (`active: false`)
- [x] Specific source connectors: BookPage, NPR, RSL library catalog (Russian National Library)
- [x] "Replay" mode — run from saved real source pages, so a weekly run is repeatable and offline-safe
- [x] Award / reader / audiobook / rights sources onboarded as real examples (National Book Awards, Goodreads Choice, Libro.fm, Publishers Marketplace)
- [x] Audiobook rankings only count when they point to a real underlying book (not audio-only material)
- [x] Suggest brand-new sources it noticed ("Source Proposals") for Dima to accept or reject
- [ ] Live fetching of many sources on its own (since 2026-11-10 the run is scheduled on a home server, ADR 0021; live fetch exists only as an opt-in per source)
- [ ] Automatic source-health monitoring (warn when a source breaks or changes layout)
- [x] Per-source contribution metrics (unique finds, corroboration, editor yield) so we can *retire* weak sources with evidence, not just add new ones — see `reqs/07`
- [x] Periodic "source review" conversation: the agent shows the metrics table and proposes keep / retire / promote-from-probation (`source-lifecycle`)
- [x] New sources start on probation and are judged on the same metrics after a few weeks
- [ ] Non-English source waves (all wanted, in order): French → Spanish → Italian → Scandinavian/Dutch, onboarded like the German sources

## 2. Building the candidate list

- [x] Convert raw mentions into one entry per actual book ("Scouting Candidate")
- [x] Merge duplicates of the same book/author even when titles, editions, or retailers differ
- [x] Count *independent* signals (several links from the same kind of source = one signal, not three)
- [x] Pick the strongest signal as the headline ("Primary Signal")
- [x] Give each book an A / B / C / D priority label
- [x] Drop clearly out-of-scope items (fiction, children's/YA, narrow manuals/textbooks, topic-only finds) into an internal exclusions file instead of the editor's list
- [x] Keep borderline nonfiction visible at lower priority rather than discarding it

## 3. Russian-market checks

- [x] Check whether a Russian-language edition already exists, on Ozon, Labirint, LitRes, Chitai-gorod, Book24
- [x] Optional library-catalog check (RSL) as bibliographic evidence (not a "for sale" signal)
- [x] Three honest statuses: *not found in Russian*, *found in Russian*, *unclear*
- [x] Send *unclear* cases to a separate manual review queue (`russian-review-queue.csv`)
- [x] Generate Russian search-query helpers (title translation, author transliteration) used **only** for searching — never shown as the real Russian title
- [ ] Russian publisher-site checks (currently logged as "deferred", does not affect status)

## 4. Risk screening (visible risk only)

- [x] Flag possible sensitive-topic risk from public data: Russia/politics, war/anti-war, WWII/USSR/Nazi comparisons, non-traditional relationships/gender/childbearing, drugs/psychoactive substances
- [x] Three statuses: *no visible risk*, *text check required*, *clear visible risk*
- [x] Risky books stay on the list (clearly marked and deprioritized) rather than being hidden
- [x] Clear language that this is **not** legal clearance and the full text was not read
- [ ] Full-text risk check with quotes / page references (needs the manuscript — later stage)

## 5. The weekly report (editor-facing)

- [x] One CSV with one row per book and a fixed, stable set of columns
- [x] Plain "Why it entered the list" explanation and a signal summary for each book
- [x] Original Russian description prose where available (no copied blurbs or reviews)
- [x] Helper fields for non-English books (English title / author / annotation) — aids only, never overwrite the real metadata
- [x] Operational details (skipped/failed sources, shortfalls, warnings) kept in separate run files, not cluttering the editor CSV
- [x] Pilot mode: caps the list to a set number of rows, plus a short plain-language review guide and a readiness check before sharing
- [x] "Update digest" view: every run labels each row new / upgraded / changed / unchanged, sorts new findings to the top, and opens the review with a "this week: N new, M upgraded, K hidden by your rules" line — see `reqs/07`
- [x] The editor reviews the list **in the agent chat** using the rendered local review, not a shared Google Sheet — see §8 and ADR 0009. The CSV stays downloadable; no Google Sheets push on the main path.

## 6. Learning from editor feedback

- [x] Editor statuses understood: *interested*, *maybe/watch*, *reject*, *already known*, *needs deeper analysis*
- [x] Editor notes preserved exactly as written
- [x] Ingest the edited sheet back in (`ingest-feedback`)
- [x] Editor gives feedback **in chat**; the agent records it via `record-feedback` into the same candidate history — retires the edited-CSV round-trip as the normal loop (`ingest-feedback` stays as a fallback). See §8 and ADR 0009.
- [x] Remember past decisions across weeks (candidate history)
- [x] Stop re-showing rejected / already-known books as if new
- [x] Bring a book back only on a real "Material Change" (new kind of signal, stronger signal, Russian-status change, risk-status change, or explicit reset)
- [x] Treat editor edits to *facts* (title, author, status) as proposed corrections, not silent overwrites
- [x] Keep "needs deeper analysis" requests in a memo queue for later
- [x] Keep Dima's technical QA separate from the editor's editorial feedback

Standing feedback — feedback that keeps working across weeks (decided in ADR 0010, spec in `reqs/07`):

- [x] Editorial Taste Profile: a readable state file of the editor's standing rules and preferences, applied on every run (`editorial-profile`)
- [x] Hard rules ("never this author / topic"): hide matching books, but every review says how many were hidden and the agent can show/undo them
- [x] Soft preferences ("more like this / fewer of those"): nudge the priority label one step, with the reason shown on the row
- [x] The agent translates natural chat ("enough of this author") into a per-book status or a profile rule, asks one scope question if unsure, and echoes back exactly what it recorded
- [x] Rejection-pattern statistics shown as *suggestions* the editor can turn into rules — nothing statistical changes ranking on its own
- [x] Three-level learning / technical-request / escalation policy (#248) formalised in `AGENTS.md` and operator workflow docs — see `docs/operator/escalation-and-handoff.md` § "Three-Level Learning, Technical-Request, and Escalation Policy"

## 7. Deeper analysis (planned — not built yet)

These are the editor's "make it smarter over time" wishes and the roadmap's later stages.

- [ ] Deep candidate memo: why is this book popular, author platform, real reader reasons
- [ ] Market-fit reasoning: "why it works there" vs "why it may not work in Russia"
- [ ] Competitor discovery from Russian marketplaces, with links
- [ ] Use reviews, tables of contents, and previews as richer evidence
- [ ] Full-manuscript / editorial-board analysis once text is available
- [ ] Editorial-board recommendation memo

## 8. Product / operations — the post-handoff operating model

After development ends, AutoScout is run by the **editor + a coding agent** — not a developer, and
not a web app. The agent is both the interface and the operator; the editor reviews and gives
feedback by talking to it. Decided in ADRs 0007–0009 and `reqs/05-editor-agent-operating-model.en.md`.

- [x] Editor runs a local coding agent that drives the existing commands (agent = UI + orchestrator)
- [x] Canonical history/feedback lives in a private **remote git repo** (also the backup and Dima's remote window into a broken state)
- [x] Feedback given in chat, recorded by the agent into the same candidate history (`record-feedback`) — retires the edited-CSV round-trip
- [x] Weekly list shown as a **local view the agent renders on demand** (no dashboard, no Google Sheet push)
- [x] Editor is the **authority**; the agent applies changes with broad autonomy, explaining consequences and offering options before risky ones
- [x] On breakage the agent emits a diagnostic and **Dima fixes async** via the remote repo (Dima is implementer/escape-hatch, not an approver)
- [x] Blessed agent commands/runbook so the editor's agent has a safe on-rails workflow
- [x] **Nanode** hosts the grounded search adapter, and nothing else (#537, ADR 0016); the fallback runner/host option is closed (#550)
- [x] Since 2026-11-10 the weekly scan also runs **from a timer on a home server** that Dima maintains, with the search adapter beside it (ADR 0021); the Nanode adapter is the fallback

Decided **against** (so we don't rebuild them later): a hosted dashboard / web app, pushing results
into Google Sheets, scheduled runs on a server without a maintainer, and user roles/permissions (single editor).
Backups and audit history come for free from the git remote.

---

## How to run it today

```bash
# Produce a weekly report
python -m autoscout run-mvp \
  --config configs/current-weekly-pilot.json \
  --output-dir runs/current-weekly-pilot-russian \
  --state-dir runs/current-weekly-pilot-russian

# Render the local review, then preview and confirm conversational feedback
python -m autoscout render-review --output-dir runs/current-weekly-pilot-russian --state-dir runs/current-weekly-pilot-russian
# Preview (zero mutation, code 3):
python -m autoscout record-feedback --identity-key "<Candidate ID>" --status reject --note "<verbatim editor note>" --state-dir runs/current-weekly-pilot-russian
# Apply after confirmation:
python -m autoscout record-feedback --identity-key "<Candidate ID>" --status reject --note "<verbatim editor note>" --state-dir runs/current-weekly-pilot-russian --confirmed-by-editor

# Fallback only: bring an edited CSV back in
python -m autoscout ingest-feedback --csv edited-sheet.csv --state-dir runs/current-weekly-pilot-russian

# Add, accept, or reject a suggested new source
python -m autoscout source-proposal add --source-url <url> --reason "<why>" --state-dir runs/current-weekly-pilot-russian
python -m autoscout source-proposal accept --id <id> --state-dir <dir> --config <config>
python -m autoscout source-proposal reject --id <id> --state-dir <dir> --reason "<why>"
```

The weekly report and supporting files land in the output directory; the editor-facing file is
`editor-facing.csv`. See `docs/current-weekly-operator-workflow.md` for the full weekly routine.
