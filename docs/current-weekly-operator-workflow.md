# Current Weekly Operator Workflow

The operator workflow is split by topic into the files under `docs/operator/` (#1132).
Read the one file for the task at hand; each is complete on its topic.
New operator text goes into the file of its topic, and `./scripts/check` keeps every file under its line limit.

| Topic | Read it when |
|---|---|
| [The editor's one-request path](operator/one-request-path.md) | The editor asks for the weekly search; auto-completion after a run, retries, editor output and review format, earlier editions, identity correction, description enrichment. |
| [The run server and the State Store](operator/run-server.md) | Installing or changing the search adapter, the nightly timers, the gate on new code, or the review page on the server; converting Candidate History or switching the State Store backend. |
| [Doctor diagnostics](operator/doctor.md) | The editor asks why AutoScout is blocked or what state it is in; local repairs and authorized recovery. |
| [Escalation, editor authority and the handoff to Dima](operator/escalation-and-handoff.md) | Classifying an interaction as learning, a technical request or an escalation; what the editor may decide alone; diagnosing a failed run and handing it to Dima. |
| [Capture, replay and extraction](operator/capture-and-extraction.md) | Running the pipeline step by step; config files, live capture, Codex extraction, replay, artifact checks, the Codex/API boundary and API-backed extraction. |
| [Review, feedback and the Editorial Taste Profile](operator/review-and-feedback.md) | The editor returns a review or gives feedback on a book; previewing and confirming feedback and profile entries. |
| [Source proposals, probes and source health](operator/sources.md) | Proposing, probing or discovering sources; the quick source health check; the monthly source discovery nudge. |
| [Pre-collection passes and field completion](operator/pre-collection.md) | The Kirkus, Publishers Marketplace and Publishers Weekly passes; filling ISBNs, recording original editions and the age of each work. |
| [The booksellers' portal](operator/bookseller-portal.md) | CEGAL's permission for todostuslibros.com and its conditions, the weekly and first-load ceilings, the ledger and the switch. |
| [Editorial-fit scoring](operator/editorial-fit.md) | The fit score in the review, the shadow scoring stage and its report. |
