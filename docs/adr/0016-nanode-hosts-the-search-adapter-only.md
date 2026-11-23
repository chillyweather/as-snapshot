---
status: accepted
---

# The Nanode hosts the search adapter only, the pipeline stays on the operator's machine

ADR 0007 kept a Nanode as a secondary path that could hold state and run the pipeline.
That secondary path is now closed.
The Nanode hosts exactly one thing, the grounded search adapter, and the pipeline runs on the operator's machine, which is where ADR 0007 already put the primary path.

The reason to put anything on the Nanode was the Brave credential.
The adapter is the only component that holds it, and on a laptop the key was retyped for every run and the adapter died when a terminal closed.
Moving that one process fixes both, and it is the whole of what the move buys: `generate-descriptions` and the Codex synthesis still run on the operator's machine, so a run still needs that machine.

Moving the pipeline as well was considered and rejected.
It would need a start-and-watch surface, which is the persistent surface ADR 0009 rejected after the editor said she prefers to review by talking to the agent.
It also asks a 1 GB, 1 vCPU box with existing nginx and docker workloads to carry `MAX_WORKERS = 3` and Codex extraction across 28 sources, and it puts the weekly run on an unattended server with no maintainer, which ADR 0007 rejected in these words: a broken unattended server is worse than a missed manual run.

## How the adapter is reached, and why not the obvious way

The adapter binds the loopback interface only and has no authentication, both deliberately (`autoscout/grounded_search_adapter.py:45`, `:226`, `:244`).
`HTTPDiscoveryProvider` sends no custom header, and a secret in a URL would reach logs, run artifacts, and the live progress snapshot.
So a public port is not available: an unauthenticated public adapter spends the Brave quota for anyone who finds it.

The adapter is therefore reached over a tailnet, which authenticates the caller at the network layer.
A systemd user socket listens on the Nanode's tailnet address alone and hands each connection to `systemd-socket-proxyd`.
The adapter's loopback bind is unchanged, nothing listens on the public address, and no AutoScout code changed: `HTTPDiscoveryProvider` already accepts any operator-supplied HTTP URL template.

An SSH tunnel was the first design and remains as a fallback for an operator machine that is off the tailnet.
It was demoted because it needs a command running for the length of every run, and the constraint it satisfies is caller authentication, which a private network satisfies without per-run action.

## Consequences

- Every device on the tailnet can spend the Brave quota, because the adapter has no authentication of its own. A tailnet ACL is the control, and `docs/nanode-search-adapter-runbook.md` carries it.
- An operator machine that is off the tailnet needs the tunnel fallback. A run does not silently fall back to a local adapter, because `ExitOnForwardFailure` makes a taken port a loud failure.
- The Nanode is now a production dependency of the weekly run, not a documented fallback. It has one owner, one credential file, and one runbook. *(Narrowed by ADR 0021: since 2026-11-10 the scheduled run uses an adapter on the run server, and the Nanode adapter is the fallback and the laptop path.)*
- Provisioning a second operator machine no longer means handing over the Brave key. It means adding that machine to the tailnet. That is what makes #630 small.
