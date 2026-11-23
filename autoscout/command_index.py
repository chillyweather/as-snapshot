"""Print every command the operator can run, grouped by task.

`./scripts/help` prints this list. Each entry says in one line what the
command does and what it touches, and `docs/cli-reference.md` holds the
details. `tests/test_command_index.py` fails when a launcher in `scripts/` or a
project slash command in `.claude/skills/` is missing from the list.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    usage: str
    summary: str
    effect: str


@dataclass(frozen=True)
class Group:
    title: str
    commands: tuple[Command, ...]


GROUPS: tuple[Group, ...] = (
    Group(
        "Weekly run",
        (
            Command(
                "./scripts/run-weekly",
                "Run the weekly scan and build the editor's review",
                "network, model, writes and pushes weekly state",
            ),
            Command(
                "./scripts/doctor",
                "Say why a run is blocked and what to do next",
                "read-only; --repair and --recover change state",
            ),
            Command(
                "./scripts/code-gate [--run]",
                "Say whether this checkout's code passed ./scripts/check on this machine",
                "read-only; --run runs the check and writes data/code-gate/",
            ),
            Command(
                "./scripts/check-sources",
                "Check that every configured source still answers",
                "network, read-only",
            ),
            Command(
                "./scripts/collect-kirkus",
                "Refresh Kirkus outside a weekly run, in about 30 minutes",
                "network, writes data/kirkus-precollection/",
            ),
            Command(
                "./scripts/collect-pw-issue",
                "Refresh Publishers Weekly issue reviews outside a weekly run",
                "network, writes data/pw-precollection/",
            ),
            Command(
                "./scripts/stage-pw-pdfs --archive <zip> --year <year> --output <file>",
                "Extract local Publishers Weekly Nonfiction Reviews for a later run",
                "offline, writes only the selected staging file",
            ),
            Command(
                "./scripts/describe-pw-pdfs --archive <zip> --staged <file> --output <file> --port <n> --model <name>",
                "Draft original Russian descriptions from local PW reviews",
                "loopback-only model unless --provider deepseek, writes only summaries and review digests",
            ),
            Command(
                "./scripts/import-staged-pw --staged <file> --collection <file>",
                "Preview or apply a bounded batch of staged PW issues",
                "offline, read-only preview; --apply writes only data/pw-precollection/",
            ),
            Command(
                "./scripts/backfill-pw-history --staged <file> --descriptions <file>",
                "Preview or add local PW books directly to Candidate History",
                "offline, read-only preview; --apply writes history, identity aliases and review",
            ),
            Command(
                "./scripts/shard-candidate-history [--dry-run]",
                "Convert the weekly state's Candidate History to one file per book, once",
                "offline; --dry-run is read-only, otherwise rewrites Candidate History",
            ),
            Command(
                "./scripts/state-database rebuild|export|cutover|rollback [--overwrite] [--force]",
                "Build the SQLite state database from the weekly state's JSON, export it back, or move the state to SQLite and back",
                "offline; rebuild writes only the database under data/state-store/, export writes the state's JSON, "
                "cutover and rollback also write or remove the state's backend marker",
            ),
            Command(
                "./scripts/mark-archive-backfill --before-ref <rev> --after-ref <rev> --batch <name> --applied-date <day>",
                "Preview or mark the history records an earlier archive backfill added, found from Git",
                "offline, read-only preview; --apply writes history and review",
            ),
            Command(
                "./scripts/collect-pm-deals",
                "Refresh Publishers Marketplace deals outside a weekly run",
                "network, read-only mailbox, writes data/pm-precollection/",
            ),
            Command(
                "./scripts/complete-isbns",
                "Fill missing ISBNs of books awaiting a decision (preview; --apply writes)",
                "network unless --offline; --apply writes Candidate History and candidate snapshots",
            ),
            Command(
                "./scripts/complete-originals",
                "Record confirmed original editions of translated books (preview; --apply writes)",
                "network (national libraries, Open Library, todostuslibros on --apply), model; "
                "--apply writes Candidate History, candidate snapshots and the portal ledger",
            ),
            Command(
                "./scripts/complete-russian-editions",
                "Record whether a Russian edition of each undecided book exists (preview; --apply writes)",
                "network (LiveLib search, Wikidata, Russian State Library, LitRes), model; "
                "--apply writes Candidate History and candidate snapshots",
            ),
            Command(
                "./scripts/portal-first-load [--limit N]",
                "Ask todostuslibros once about Spanish books already collected, up to 500 in all (preview; --apply asks)",
                "network on --apply, no model; --apply writes only the portal ledger in the state directory",
            ),
            Command(
                "./scripts/complete-work-age",
                "Record each book's first publication year and edition count (preview; --apply writes)",
                "network (Open Library); --apply writes Candidate History and candidate snapshots",
            ),
            Command(
                "./scripts/generate-descriptions",
                "Write missing Russian descriptions, resumably",
                "network, model, writes weekly state",
            ),
        ),
    ),
    Group(
        "Sources",
        (
            Command(
                "./scripts/night-scout [url ...] [note]",
                "Run /scout-sources overnight in Codex",
                "network, model, full access, pushes night-scout/* branches",
            ),
            Command(
                "./scripts/probe-source --url <url>",
                "Probe a candidate source page",
                "network, writes the probe ledger",
            ),
            Command(
                "./scripts/source-novelty --sample <file> --state-dir <dir>",
                "Count the books on a candidate source AutoScout has not met",
                "offline, read-only",
            ),
            Command(
                "./scripts/source-yield-report [--state-dir <dir>] [--config <file>]",
                "Show how many useful books each source delivered, and which meet the retirement rule",
                "offline, read-only",
            ),
            Command(
                "./scripts/add-source",
                "Preview adding a probed source; confirm to commit it",
                "offline; the confirmed call commits",
            ),
            Command(
                "./scripts/add-source discover",
                "Report signal families the portfolio covers weakly",
                "offline, read-only",
            ),
            Command(
                "./scripts/add-source monthly --operation-month YYYY-MM",
                "Publish the monthly source-discovery nudge",
                "offline, writes data/monthly-source-discovery/",
            ),
            Command(
                "./scripts/audit-annual-sources [--live] [--propose]",
                "Say whether an annual source's newer edition or award stage is out, and preview the change",
                "offline by default; --live reads the declared pages; never changes configuration",
            ),
            Command(
                "./scripts/check-isbn-author --isbn <isbn> --title <title>",
                "Find a ranking row's missing author by ISBN in the DNB",
                "network, read-only",
            ),
        ),
    ),
    Group(
        "Models",
        (
            Command(
                "./scripts/qualify-model --model <name>",
                "Test a Codex model for extraction against the pinned one",
                "model, writes data/model-qualification/",
            ),
            Command(
                "./scripts/qualify-description-model --candidate <model>:<effort>",
                "Compare Codex models for Russian descriptions",
                "model, writes data/description-qualification/",
            ),
            Command(
                "./scripts/export-decision-corpus",
                "Export the editor's dated decisions, rebuilt from Git history, for #821",
                "offline, read-only state, writes data/decision-corpus/",
            ),
            Command(
                "./scripts/editorial-fit-shadow report",
                "Compare shadow fit scores with the editor's decisions",
                "offline, read-only; score is live and billed",
            ),
            Command(
                "./scripts/ranking-benchmark",
                "Test each ordering against the current review order on the same books",
                "offline, read-only",
            ),
            Command(
                "./scripts/work-age-report",
                "Measure on decided books whether the age of the work would improve the review order",
                "offline, read-only state, writes data/work-age-report/",
            ),
            Command(
                "./scripts/publish-showcase [--push]",
                "Preview, or with --push publish, the public showcase copy",
                "offline preview; --push pushes to the public repo",
            ),
        ),
    ),
    Group(
        "Search adapter on the Nanode",
        (
            Command(
                "./scripts/nanode-adapter-status",
                "Show the adapter's state, or its logs with --logs",
                "network, read-only",
            ),
            Command(
                "./scripts/deploy-nanode-adapter",
                "Install or update the adapter on the Nanode",
                "network, changes the server",
            ),
            Command(
                "./scripts/nanode-adapter-set-key",
                "Put or rotate the Brave key on the Nanode",
                "network, changes the server",
            ),
            Command(
                "./scripts/nanode-adapter-direct",
                "Make the adapter reachable with no tunnel (once)",
                "network, changes the server",
            ),
            Command(
                "./scripts/nanode-adapter-tunnel",
                "Tunnel the laptop's port 8899 to the adapter",
                "network, runs until stopped",
            ),
            Command(
                "./scripts/grounded-search-adapter",
                "Run the search adapter on this machine",
                "network, runs until stopped",
            ),
        ),
    ),
    Group(
        "Development",
        (
            Command(
                "./scripts/check",
                "Run the full CI gate locally",
                "offline",
            ),
            Command(
                "./scripts/check --state-backend sqlite",
                "Run the gate with the whole suite on the SQLite State Store",
                "offline",
            ),
            Command(
                "./scripts/install-hooks",
                "Turn on the pre-push hook: a task branch is pushed only after the gate passed",
                "offline, changes this clone's Git config",
            ),
            Command(
                "./scripts/check-artifact-parity --stage <stage> <a> <b>",
                "Compare the artifacts of two runs",
                "offline, read-only",
            ),
            Command(
                "./scripts/replay-history-identity-keys",
                "List history records whose identity key would move",
                "offline, read-only",
            ),
            Command(
                "./scripts/help",
                "Print this list",
                "offline, read-only",
            ),
        ),
    ),
    Group(
        "Test fixtures",
        (
            Command(
                "./scripts/refresh-issue-533-author-corpus",
                "Regenerate the issue #533 author fixture",
                "offline, writes a test fixture",
            ),
        ),
    ),
    Group(
        "Slash commands in Claude Code",
        (
            Command(
                "/scout-sources [url ...] [note]",
                "Scout sources overnight, or onboard the given URLs",
                "network, pushes night-scout/* branches",
            ),
        ),
    ),
)

HEADER = (
    "AutoScout commands. Run them from the repository root.",
    "Details for each: docs/cli-reference.md",
)


def render() -> str:
    lines = list(HEADER)
    for group in GROUPS:
        lines.append("")
        lines.append(group.title)
        for command in group.commands:
            lines.append(f"  {command.usage}")
            lines.append(f"      {command.summary} ({command.effect})")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    print(render(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
