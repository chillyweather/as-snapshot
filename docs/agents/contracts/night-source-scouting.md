# Night source scouting (#896)

The operator starts an unattended night of source scouting in Claude Code with `/scout-sources`, or in Codex with `./scripts/night-scout`.
The procedure is `.claude/skills/scout-sources/SKILL.md`; Codex reaches it through `.agents/skills/scout-sources`.
With URLs as arguments (`/scout-sources <url> ... [note]` or `./scripts/night-scout <url> ... [note]`), the run skips the search and spends the night onboarding those sources. The authorisation and gates below are the same.
That request authorises, with no further questions:

- web search
- probing with `./scripts/probe-source`
- the offline novelty check `./scripts/source-novelty`
- new adapters or contract code with tests, written in the run's own worktrees
- onboarding issues
- pushed `night-scout/*` branches and draft PRs

A missing contract family is work to do, not a reason to defer; every onboarding gate still applies.
The run never confirms, merges, mutates Source Proposals, or writes weekly or editorial state.
For a night-scouted source, the operator's explicit morning confirmation authorises its confirmed `./scripts/add-source` call in place of the editor's.

`./scripts/night-scout` runs Codex with live web search, full access and no approval prompts, because nobody is there to answer one and the Codex workspace sandbox keeps `.git` read-only.
The single tool-level sandbox-permission requests of the Codex contract in `AGENTS.md` therefore do not apply inside that run; the skill's boundaries take their place.
Codex never starts the skill on its own (`agents/openai.yaml` disallows implicit invocation), and an interactive Codex session would stop at the first approval prompt, so the launcher is the only Codex entry point.

`./scripts/source-novelty --sample <sample.json> --state-dir runs/current-weekly-pilot-russian` is offline and read-only.
It matches sampled entries against Candidate History and returns exit 0 with a verdict (`promising`, `weak`, `no_novelty`), or exit 2 on an input error.
