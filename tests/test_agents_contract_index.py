"""AGENTS.md indexes exactly the contract files under docs/agents/contracts/.

The size limit on AGENTS.md is enforced by the AGENTS.md size stake in
scripts/check, not here.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "agents" / "contracts"


def test_agents_md_indexes_exactly_the_contract_files() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    referenced = set(re.findall(r"docs/agents/contracts/([a-z0-9-]+\.md)", agents))
    present = {path.name for path in CONTRACTS.glob("*.md")}
    assert referenced == present
