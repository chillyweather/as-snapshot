---
status: accepted
---

# The run server hosts the search adapter, next to the pipeline

This supersedes ADR 0016 for the scheduled weekly run.
*(Extended by ADR 0022: the timer runs every night, with its own Kirkus timer and a morning report.)*

ADR 0016 put the grounded search adapter on the Nanode and kept the pipeline on the operator's laptop.
On 2026-11-10 the weekly run moved to a home server that is always on and runs it from a systemd timer.
The adapter now runs on that same server as a systemd user service, and the run reaches it at `http://127.0.0.1:8899`.

The three reasons ADR 0016 gave for the Nanode hold on the run server without it.
The Brave key does not live on a laptop: it sits in `~/.config/autoscout/search.env`, mode 0600, beside the mailbox and TypeSafe credentials the run already needs there.
The adapter does not die with a terminal: systemd restarts it and linger brings it back after a reboot.
The key still has one place and one owner.

Keeping the adapter on the Nanode would leave two machines and a tailnet link in the path of an unattended run.
A run whose endpoint does not answer degrades the description stage instead of failing, so that dependency fails quietly.
On the run server the adapter's own loopback bind is the whole access control, and no tailnet listener is needed.

## Consequences

- The Nanode is no longer a dependency of the scheduled run. Its adapter may stay up as a fallback and for a laptop run; the `nanode-adapter-*` scripts and `docs/nanode-search-adapter-runbook.md` still describe it.
- The adapter runs from the production checkout, so a pull updates it at its next restart. There is no separate deploy step.
- Brave requests leave from the run server's address, not the Nanode's.
- The reasoning in ADR 0016 against running the pipeline on the Nanode itself is unchanged: that box is too small. ADR 0007's rejection of an unattended server, which ADR 0016 quotes, is what the move of 2026-11-10 reversed, with the operator as its maintainer.
- The unit is `deploy/server/autoscout-search-adapter.service`, and `docs/operator/run-server.md` carries the install steps.
