# Issue #449 diagnosis: the Codex no-progress timeout

This document records what the retained evidence says about the no-progress lease, and what it does not say.
It is a diagnosis, as issue #449 requires.
It proposes no repair, and it changes no runtime bound.

## Summary

The lease did not kill a working item.
In both occurrences the Codex session recorded nothing the lease would accept as progress for the whole window it measured: 181.562 seconds on 2026-08-14 and 180.293 seconds on 2026-09-03.
Neither window holds an event of any kind, recognized or ignored, apart from the delivered prompt at the start of the 2026-08-14 attempt.
The lease fired on real silence, not on work it could not classify.

There are two different sub-cases, not one.
On 2026-08-14 the item received its prompt and never produced an assistant turn.
On 2026-09-03 the item worked first, then went quiet.
A repair for one is not a repair for the other, so the diagnosis names which sub-case it found.

Both rollout files were found, and both are quoted below.
Neither was matched from the run artifacts, because no record says which rollout belongs to which attempt.
The matching had to be done by hand, from prompt content, event counts, and stage timestamps.
This branch retains the session identity and the per-attempt census so the next occurrence needs no such reconstruction.

## The two occurrences

| Occurrence | Recognized events | Sub-case |
| --- | --- | --- |
| Goodreads Choice Awards, 2026-08-14 | `{task_started: 1}` | `never_started_work` |
| Académie française Prix de l'essai, 2026-09-03 | `{task_started: 1, reasoning: 3, custom_tool_call: 2, token_count: 2}` | `stalled_mid_work` |

The Goodreads figures are the ones quoted in issue #449 from the live progress snapshot of that operation.
That snapshot is not in Git, because `progress-snapshot.json` is listed in `.gitignore`, so the 2026-08-14 counts cannot be re-derived from the repository.
They are re-derived below from the rollout instead, and they agree.

The Académie française figures come from `progress-snapshot.json` in the operator working copy and from the Codex rollout named below.
Both agree.

Both failures were transient.
The 2026-08-14 attempt on Goodreads is recorded in Git as `duration_seconds` 181.563, `exit_code` null, `status` `no_progress_timeout`.
The next operation, on 2026-08-15, extracted the same source in 159.387 seconds with `exit_code` 0 and status `validated`.
No commit touched the source portfolio between the two operations, so nothing about the source was repaired in between.
The 2026-08-14 stage aggregate is `{task_started: 33, task_complete: 32, reasoning: 502, custom_tool_call: 329}`, which is one item that started and never completed.
So neither source is uncollectable, and neither needs to become a Skipped Source.

## What the 2026-08-14 occurrence shows

This is the occurrence issue #449 was filed for.
Its rollout is `~/.codex/sessions/2026/08/14/rollout-2026-08-14T12-54-10-019fffb1-7fba-7b01-8533-51cc2da22692.jsonl`.

It is identified as the Goodreads attempt on four independent points.
Its prompt names `codex-capture-goodreads-choice-awards-2025-nonfiction-e28fc02b.json`.
It opened at `2026-08-14T09:54:10.996Z`, which is 1.5 seconds after the capture stage of that operation finished at `09:54:09.43`, so it belongs to the first batch of extraction submissions, and the run recorded Goodreads at `attempt_submission_sequence` 3 with three workers.
Its recognized census is `{task_started: 1}`, which is what the live snapshot quoted in issue #449 recorded.
The run recorded the attempt as 181.563 seconds with `exit_code` null.

The whole file is 8 records:

```
0  2026-08-14T09:54:10.995Z session_meta
1  2026-08-14T09:54:10.996Z event_msg/task_started
2  2026-08-14T09:54:13.888Z response_item/message        "## Memory  You have access to a memory folder with guidance from prior runs..."
3  2026-08-14T09:54:13.888Z response_item/message        "<recommended_plugins> ..."
4  2026-08-14T09:54:13.888Z world_state
5  2026-08-14T09:54:13.889Z turn_context
6  2026-08-14T09:54:13.942Z response_item/message        "Read capture.json and return only metadata-only JSON..."
7  2026-08-14T09:54:13.942Z event_msg/user_message       "Read capture.json and return only metadata-only JSON..."
```

The session opened, the injected context and the extraction prompt were delivered 2.9 seconds later, and then nothing at all was recorded for the remaining 178.616 seconds.
There is no reasoning record, no tool call, and no token count.
The model never produced an assistant turn.

The lease measures from the last event it accepts as progress, which here is `task_started` at 09:54:10.996Z, so the window it bounded is 181.562 seconds.
Both figures matter, and they are reported separately: `recognized_silence_seconds` is what the lease bounds, and `trailing_silence_seconds` is what a reader sees at the end of the file.

This is the answer to the question in issue #449.
The work never began, and the lease reported that correctly.
The failure is on the far side of prompt delivery: the item held its complete input, and no assistant turn came back.
Neither the capture nor the extraction prompt is implicated, because both are present in the rollout.
The rollout cannot say more than that.

Note for issue #414: the last injected content before the silence is the memory-folder instruction and the recommended-plugins list.
That is a correlation and not a cause.
The 2026-09-03 attempt received the same injection and worked normally, so the injection is not sufficient to stall an item.
It is recorded here because the `never_started_work` sub-case stalls exactly where session-start context is assembled.
The memory injection was decided separately, on grounding rather than on this stall: see `docs/adr/0017-extraction-disables-codex-memory.md`.

## What the 2026-09-03 occurrence shows

The failed attempt as the run recorded it, from the `codex` stage of `runs/current-weekly-pilot-russian/workflow-log.json`:

```json
{
  "attempt": 1,
  "attempt_submission_sequence": 20,
  "diagnostic": "codex_no_progress_timeout",
  "duration_seconds": 210.778,
  "exit_code": null,
  "source_name": "Académie française Prix de l’essai",
  "started": true,
  "status": "no_progress_timeout",
  "worker_slot": 1
}
```

The stage recorded `no_progress_quiet_seconds` of 180.0027, and this was the only boundary stop in the operation.

The rollout for that attempt is `~/.codex/sessions/2026/09/03/rollout-2026-09-03T11-10-56-01a06652-2c9f-7012-92a1-603050df2d65.jsonl`.
It holds 26 records.
The first is stamped `2026-09-03T08:10:56.715Z` and the last is stamped `2026-09-03T08:11:27.200Z`, which is a recorded span of 30.485 seconds.
The attempt ran for 210.778 seconds.
So 180.293 seconds of the attempt produced no record of any kind.

Read with the same classifier the production lease uses, those 26 records are 8 recognized events and 18 ignored events:

- recognized: `task_started` 1, `reasoning` 3, `custom_tool_call` 2, `token_count` 2
- ignored: `event_msg/item_completed` 7, `response_item/message` 6, `response_item/custom_tool_call_output` 2, `session_meta` 1, `world_state` 1, `turn_context` 1

The last six records, oldest first, are:

```
2026-09-03T08:11:13.783Z response_item/custom_tool_call    945B  jq -r '.html' capture.json | perl -0ne ... academie-laureate ...
2026-09-03T08:11:13.897Z event_msg/item_completed        15923B
2026-09-03T08:11:13.900Z response_item/custom_tool_call_output 5338B
2026-09-03T08:11:13.901Z event_msg/token_count             860B
2026-09-03T08:11:27.196Z event_msg/item_completed           394B
2026-09-03T08:11:27.200Z response_item/reasoning          4088B
```

The item read the retained capture, ran a bounded regular expression over the page HTML, received its output, reported its token use, and produced one more reasoning record 13.3 seconds later.
Then it stopped.

## Answers to the questions in issue #449

**Did the subprocess start and then stall, or did it never do useful work?**
Both happened, in different operations.
On 2026-08-14 the subprocess started, received its prompt, and never did any work.
On 2026-09-03 the subprocess did useful work, with two tool calls against the retained capture, three reasoning records, and two token reports, and then stalled.
The single status `no_progress_timeout` covers two different failures.

**Is the rollout file present for this attempt, and what does its tail hold after `task_started`?**
Both rollouts are present, and both tails are quoted above.
The 2026-08-14 tail holds the injected session context and the extraction prompt, and no model output at all.
The 2026-09-03 tail holds real extraction work, and then nothing.

**Was the item retried, and did the retry behave the same way?**
Neither item was retried inside its own operation.
Each recorded one attempt whose terminal class is `no_progress_timeout`, and each operation then served that source from its earlier validated artifact, which is why both runs reported reused evidence.
Across operations the answer is no: Goodreads validated on the next day, on 2026-08-15, in 159.387 seconds.

**Does this source differ from the other 28 in a way that matters?**
Not in a way this evidence shows, for either source.
Both captures were present, both prompts were delivered, and the Académie française attempt was working on its capture with ordinary tool calls.
Both sources produce evidence in ordinary operations: the Académie française source produced 55 mentions in the same 2026-09-03 operation from its reused artifact.

**Is Goodreads serving something the agent cannot act on, such as a consent wall or a bot check?**
No.
This question assumed extraction fetches the page, and it does not.
The capture stage had already retained the page, the extraction prompt tells the item to read `capture.json`, and the rollout contains no request to goodreads.com.
Whatever stalled the 2026-08-14 attempt happened before the model read anything.

## What the evidence does not say

It does not say why the session went quiet.
A silent stream is consistent with a stalled model response, with a lost connection, and with a subprocess that was alive but no longer producing.
The rollout cannot separate those, because a rollout only records what happened, not what failed to happen.

It does not support a change to `RECOGNIZED_EVENT_TYPES`.
The ignored-event census exists so that claim can be checked, and for both occurrences the check fails: no event of any type, recognized or ignored, arrived in either silent window.
This is User Story 2 of the issue, that a stalled item be told apart from a working item, and the census is what settles it.
On 2026-09-03 the most frequent ignored event is `event_msg/item_completed`, and it too stopped at 08:11:27.
On 2026-08-14 the last record of any kind is the delivered prompt.

It does not support a retry for this case.
A retry over an unknown cause buys 180 seconds of silence twice, exactly as issue #449 states.

## Reproducing this

The one-off tooling that read a run or a Codex rollout and classified the two sub-cases, and the committed diagnosis fixtures it produced, were removed in #1016.
They are available in Git history.

## What this branch retains for the next occurrence

`autoscout/codex_recovery.py` now records, for every attempt:

- the Codex session id and the resolved rollout path, so an attempt can be matched to the rollout that recorded it
- the recognized-event census and the quiet seconds, per attempt rather than only per stage
- a census of the events the lease ignored, so the "the lease could not see the work" hypothesis stays falsifiable
- a bounded descriptor tail, retained only for an attempt that did not validate

The tail holds no page content and no model prose.
Each record carries a timestamp, an event kind, a byte size, and, for a tool call, the tool name and a bounded redacted excerpt of the model-authored input.
Tool output is page-derived, so only its size is retained.

The 180-second no-progress lease, the 1,800-second absolute ceiling, the segment budget, the 7,200-second weekly ceiling, and `RECOGNIZED_EVENT_TYPES` are all unchanged.
