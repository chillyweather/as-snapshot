"""Issue #915: a record the source eligibility rules exclude is never described.

``Les futurs en partage`` by Élodie Exemple is a synthetic Nonfiction.fr
fixture that ``source_eligibility`` already marks ``invalid fixture record``.
The review never showed it, but it stayed in Candidate History, and every
weekly run kept spending description calls on it, because the description
stages select from history without that rule.

The excluded-candidate-history file is a per-run log and legitimately shares
identities with active history (a retailer-only memoir that later returns
from an eligible source), so the rule here is source eligibility, not
membership of that file.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from autoscout.content_description import DESCRIPTION_FALLBACK
from autoscout.content_description import prepare_backfill_work_items
from autoscout.description_repair import select_due_repairs
from autoscout.source_eligibility import historical_record_source_eligibility_exclusion_reason

THIN = "Короткое описание курса о химии."


def _record(identity_key: str, title: str, authors: list[str], source_name: str, **extra: Any) -> dict[str, Any]:
    return {
        "candidate_id": f"candidate-{identity_key.split('|')[0].replace(' ', '-')}",
        "identity_key": identity_key,
        "title": title,
        "primary_authors": authors,
        "source_evidence": [{"source_name": source_name, "source_family": "media/curated list"}],
        **extra,
    }


FIXTURE = _record(
    "les futurs en partage|élodie exemple",
    "Les futurs en partage",
    ["Élodie Exemple"],
    "Nonfiction.fr Critiques",
    content_description=DESCRIPTION_FALLBACK,
)
REAL = _record(
    "les hasards d une vie|jean cazeneuve",
    "Les hasards d'une vie",
    ["Jean Cazeneuve"],
    "Nonfiction.fr Critiques",
    content_description=DESCRIPTION_FALLBACK,
)


def _thin(record: dict[str, Any]) -> dict[str, Any]:
    return {
        **record,
        "content_description": THIN,
        "description_provenance": {"provenance": "grounded", "content_description": THIN},
    }


NON_BOOK = _thin(
    _record(
        "chemistry and our universe|ron b davis",
        "Chemistry and Our Universe",
        ["Ron B. Davis"],
        "Audible Bestselling Science & Engineering Audiobooks",
    )
)
REAL_THIN = _thin(
    _record(
        "the chemistry book|alex example",
        "The Chemistry Book",
        ["Alex Example"],
        "Audible Bestselling Science & Engineering Audiobooks",
    )
)


def test_the_records_are_what_the_eligibility_rules_say() -> None:
    assert historical_record_source_eligibility_exclusion_reason(FIXTURE) == "invalid fixture record"
    assert historical_record_source_eligibility_exclusion_reason(NON_BOOK) == "non-book material"
    assert historical_record_source_eligibility_exclusion_reason(REAL) == ""
    assert historical_record_source_eligibility_exclusion_reason(REAL_THIN) == ""


def test_backfill_work_items_skip_an_ineligible_record() -> None:
    keys = [item["identity_key"] for item in prepare_backfill_work_items([FIXTURE, REAL])]
    assert keys == [REAL["identity_key"]]


def test_the_adjudication_selection_skips_an_ineligible_record() -> None:
    items = prepare_backfill_work_items(
        [FIXTURE, REAL, NON_BOOK, REAL_THIN],
        include_depth_repair=True,
        include_unsourced_stubs=True,
        repair_as_of=date(2026, 11, 2),
    )
    keys = {item["identity_key"] for item in items}
    assert FIXTURE["identity_key"] not in keys and NON_BOOK["identity_key"] not in keys
    assert REAL["identity_key"] in keys and REAL_THIN["identity_key"] in keys


def test_repairs_skip_an_ineligible_record() -> None:
    partition = select_due_repairs([NON_BOOK, REAL_THIN], date(2026, 11, 2))
    selected = [item["identity_key"] for bucket in partition.values() for item in bucket]
    assert selected == [REAL_THIN["identity_key"]]
