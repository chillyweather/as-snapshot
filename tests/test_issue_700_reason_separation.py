"""Issue #700 part A: separate the four collection failure reasons.

Eight sources failed run 4090c59a with one identical message, hiding four
unrelated conditions: a genuinely empty page, a flaky agent, a stale URL, and
a shell capture. Three are separated here. The fourth - a stale edition or
period pinned into an otherwise resolving URL - is not detectable from any
field this seam receives, so it is left to #680's rollover audit rather than
guessed at. These tests pin the separation at the pure decision seam
(`zero_contribution_outcome`) plus the two evidence threadings that make it
honest: capture bytes attached by the collector that holds the capture, and
retry evidence attached by the collection loop from the extraction outcomes.

What content probes cannot do is proven by the committed captures, not
asserted here: BookPage recovered 11 entries from 7,452 visible-text chars
and Die Zeit recovered 1 from 3,688, both *below* the Audiobooks shell's
4,672. Any text-volume or marker rule calls a recoverable page a shell, so
the seam uses only byte presence, URL shape, and retry evidence.
"""

from __future__ import annotations

from typing import Any


def _full_list_source(**overrides: Any) -> dict[str, Any]:
    source = {
        "name": "A Weekly Chart",
        "url": "https://example.test/list",
        "family": "bestseller/ranking",
    }
    source.update(overrides)
    return source


def test_a_source_with_no_url_says_so() -> None:
    """A source that configures no URL fetched nothing, so the reason must
    say that rather than blame the page.

    Defensive only: the portfolio carries a URL for every configured source
    and overlays merge on top, so no live source reaches this branch. It is
    explicitly *not* the PW Best Books / PublishNews condition - those
    resolve fine and are pinned to a stale edition and a stale week."""
    from autoscout.source_contribution import zero_contribution_outcome

    status, reason = zero_contribution_outcome(
        {"name": "A Weekly Chart"},
        extracted_count=0,
        valid_mention_count=0,
        admissible_mention_count=0,
    )

    assert status == "failed"
    assert "no URL configured" in reason


def test_a_site_root_url_is_noted_beside_the_attributed_reason() -> None:
    """A root path is reported as something to check, never as the cause.

    TodosTusLibros is wrongly pointed at a homepage, but Deutscher
    Sachbuchpreis reads its current edition from a stable landing page by
    design (#679 settled that changing the URL is not the fix) and both
    declare url_stability "stable". No configured field separates them, so
    naming the root shape as the cause would misattribute a real extraction
    failure on a correctly configured source."""
    from autoscout.source_contribution import zero_contribution_outcome

    for url in ("https://www.todostuslibros.com/", "https://www.todostuslibros.com"):
        status, reason = zero_contribution_outcome(
            _full_list_source(url=url),
            extracted_count=0,
            valid_mention_count=0,
            admissible_mention_count=0,
            debug_metadata={"capture_html_bytes": 629894},
        )

        assert status == "failed", url
        assert "extraction returned no entries" in reason, url
        assert "site root" in reason, url
        assert url in reason, url


def test_a_stable_landing_page_keeps_the_701_retry() -> None:
    """The regression this rule must not reintroduce.

    Refusing the retry for a root path would withdraw #701's recovery from
    two working sources - Deutscher Sachbuchpreis and Magnesia Litera, both
    deliberately configured at a landing page - to spare one misconfigured
    one. Pinned against the real portfolio so a future rule change cannot
    quietly narrow retry coverage again."""
    import json
    from pathlib import Path
    from urllib.parse import urlsplit

    from autoscout.source_contribution import should_retry_empty_extraction

    portfolio = json.loads(
        (Path(__file__).resolve().parents[1] / "configs" / "current-weekly-portfolio.json").read_text(encoding="utf-8")
    )
    roots = [
        source
        for source in portfolio["sources"]
        if source.get("active") and urlsplit(str(source.get("url", "")).strip()).path in ("", "/")
    ]

    assert roots, "expected the portfolio to still carry landing-page sources"
    for source in roots:
        assert should_retry_empty_extraction(source, extracted_count=0) is True, source["name"]


def test_a_byte_empty_capture_says_extraction_had_nothing_to_read() -> None:
    from autoscout.source_contribution import zero_contribution_outcome

    status, reason = zero_contribution_outcome(
        _full_list_source(),
        extracted_count=0,
        valid_mention_count=0,
        admissible_mention_count=0,
        debug_metadata={"capture_html_bytes": 0},
    )

    assert status == "failed"
    assert "no page content" in reason


def test_a_retry_that_stayed_empty_names_both_attempts() -> None:
    """The non-deterministic case, still empty after the #701 retry: the
    reason states both attempts ran against a populated capture, so the next
    reader knows a retry already happened and need not infer it."""
    from autoscout.source_contribution import zero_contribution_outcome

    status, reason = zero_contribution_outcome(
        _full_list_source(),
        extracted_count=0,
        valid_mention_count=0,
        admissible_mention_count=0,
        debug_metadata={
            "capture_html_bytes": 198094,
            "empty_extraction_retry": True,
            "empty_extraction_recovered": False,
        },
    )

    assert status == "failed"
    assert "both attempts" in reason
    assert "198094" in reason


def test_an_empty_extraction_without_retry_evidence_says_no_retry_ran() -> None:
    """Offline and replay paths never ran the extraction phase, so no retry
    evidence exists. The reason must not imply a retry happened."""
    from autoscout.source_contribution import zero_contribution_outcome

    status, reason = zero_contribution_outcome(
        _full_list_source(),
        extracted_count=0,
        valid_mention_count=0,
        admissible_mention_count=0,
        debug_metadata={"capture_html_bytes": 198094},
    )

    assert status == "failed"
    assert "no retry" in reason
    assert "both attempts" not in reason


def test_a_budget_refused_retry_reads_as_no_retry() -> None:
    """A retry the budget refused never executed, so it must read exactly as
    if no retry ran rather than as an attempted recovery."""
    from autoscout.source_contribution import zero_contribution_outcome

    status, reason = zero_contribution_outcome(
        _full_list_source(),
        extracted_count=0,
        valid_mention_count=0,
        admissible_mention_count=0,
        debug_metadata={
            "capture_html_bytes": 198094,
            "empty_extraction_retry_refused": True,
        },
    )

    assert status == "failed"
    assert "both attempts" not in reason


def test_declarations_still_beat_evidence() -> None:
    """An explicit operator declaration wins over every new branch: a source
    that may stand empty stays collected even with no URL, and an
    incremental source stays collected even with a site-root URL."""
    from autoscout.source_contribution import zero_contribution_outcome

    status, _ = zero_contribution_outcome(
        {"name": "Annual Award", "may_report_no_entries": True, "may_report_no_entries_reason": "between cycles"},
        extracted_count=0,
        valid_mention_count=0,
        admissible_mention_count=0,
        debug_metadata={"capture_html_bytes": 0},
    )
    assert status == "collected"

    status, _ = zero_contribution_outcome(
        {"name": "Feed", "seen_guids_file": "seen.json", "url": "https://example.test/"},
        extracted_count=0,
        valid_mention_count=0,
        admissible_mention_count=0,
    )
    assert status == "collected"


def test_each_condition_has_its_own_reason_string() -> None:
    """The regression as stated: no two of the four conditions may share a
    reason string."""
    from autoscout.source_contribution import zero_contribution_outcome

    def outcome(**kwargs: Any) -> str:
        return zero_contribution_outcome(
            kwargs.pop("source"),
            extracted_count=0,
            valid_mention_count=0,
            admissible_mention_count=0,
            debug_metadata=kwargs.pop("debug_metadata", None),
        )[1]

    reasons = {
        outcome(source={"name": "No URL"}),
        outcome(
            source=_full_list_source(url="https://example.test/"),
            debug_metadata={"capture_html_bytes": 198094},
        ),
        outcome(
            source=_full_list_source(),
            debug_metadata={"capture_html_bytes": 0},
        ),
        outcome(
            source=_full_list_source(),
            debug_metadata={
                "capture_html_bytes": 198094,
                "empty_extraction_retry": True,
                "empty_extraction_recovered": False,
            },
        ),
        outcome(
            source=_full_list_source(),
            debug_metadata={"capture_html_bytes": 198094},
        ),
    }

    assert len(reasons) == 5


def test_the_retry_does_not_fire_on_an_unfetchable_target() -> None:
    """A source with no URL fetched nothing and reproduces zero every time.

    A site-root URL is not refused: it may be a deliberate landing page, and
    refusing it would narrow #701 for working sources."""
    from autoscout.source_contribution import should_retry_empty_extraction

    assert should_retry_empty_extraction({"name": "No URL"}, extracted_count=0) is False
    assert should_retry_empty_extraction(_full_list_source(url="https://example.test/"), extracted_count=0) is True
    assert should_retry_empty_extraction(_full_list_source(), extracted_count=0) is True


def test_the_retry_sees_the_work_item_url() -> None:
    """Work items carry the URL top-level as `source_url`, outside
    `source_metadata`. Folding it in is what lets the retry fire in
    production; without the fold every item reads as URL-less and the #701
    retry silently never fires."""
    from autoscout.codex_recovery import _item_source_declaration
    from autoscout.source_contribution import should_retry_empty_extraction

    declaration = _item_source_declaration(
        {"source_name": "Chart", "source_url": "https://example.test/list", "source_metadata": {}}
    )
    assert should_retry_empty_extraction(declaration, extracted_count=0) is True

    assert should_retry_empty_extraction(_item_source_declaration({}), extracted_count=0) is False


def test_capture_bytes_come_from_the_capture_itself() -> None:
    """The collector holds the capture page, so it attaches the byte count;
    the seam never guesses it. Real bytes from run 4090c59a."""
    import json
    from pathlib import Path

    from autoscout.source_collectors.base import capture_content_evidence

    fixture_dir = Path(__file__).resolve().parent / "fixtures" / "issue-700"
    capture = json.loads((fixture_dir / "audiobooks-shell-capture.json").read_text(encoding="utf-8"))

    assert capture_content_evidence(capture) == {"capture_html_bytes": 198094}
    assert capture_content_evidence({}) == {"capture_html_bytes": 0}
    assert capture_content_evidence({"html": None}) == {"capture_html_bytes": 0}


def test_real_captures_separate_along_url_shape() -> None:
    """Every zero here is extraction-attributed; the homepage shape rides
    along as a note. TodosTusLibros resolves to the site root, Audiobooks and
    Carnegie carry deep URLs, and all three keep the reason the seam can
    actually stand behind."""
    import json
    from pathlib import Path

    from autoscout.source_collectors.base import capture_content_evidence
    from autoscout.source_contribution import zero_contribution_outcome

    fixture_dir = Path(__file__).resolve().parent / "fixtures" / "issue-700"

    def classify(name: str, url: str, capture_file: str, extra: dict[str, Any] | None = None) -> str:
        capture = json.loads((fixture_dir / capture_file).read_text(encoding="utf-8"))
        evidence: dict[str, Any] = dict(capture_content_evidence(capture))
        evidence.update(extra or {})
        return zero_contribution_outcome(
            _full_list_source(name=name, url=url),
            extracted_count=0,
            valid_mention_count=0,
            admissible_mention_count=0,
            debug_metadata=evidence,
        )[1]

    todostuslibros = classify(
        "TodosTusLibros",
        "https://www.todostuslibros.com/",
        "todostuslibros-homepage-capture.json",
        {"empty_extraction_retry": True, "empty_extraction_recovered": False},
    )
    assert "both attempts" in todostuslibros
    assert "site root" in todostuslibros
    assert "both attempts" in classify(
        "Audiobooks",
        "https://www.audiobooks.com/browse/non-fiction",
        "audiobooks-shell-capture.json",
        {"empty_extraction_retry": True, "empty_extraction_recovered": False},
    )
    assert "no retry" in classify(
        "Carnegie",
        "https://www.ala.org/carnegie-medals/2026-winners",
        "carnegie-content-capture.json",
    )


def test_every_capture_holding_path_attaches_its_bytes() -> None:
    """The separation must exist in production, not only in fixtures.

    `capture_content_evidence` was first wired into the codex replay path
    alone, while every source that failed in #700 runs `agent-extraction-live`.
    On that path `capture_html_bytes` was absent, the size degraded to
    "unknown size", and the shell-capture reason was unreachable - the whole
    distinction existed only where the tests looked. Pinned by reading the
    source of each input path, because the gap is an omission and an omission
    cannot be caught by exercising the paths that do have it.
    """
    import inspect

    from autoscout.source_collectors import agent_extraction
    from autoscout.source_collectors import codex_extraction

    for function in (
        agent_extraction.live_agent_extraction_inputs,
        agent_extraction.configured_agent_extraction_inputs,
        codex_extraction.configured_codex_extraction_inputs,
    ):
        assert "capture_content_evidence" in inspect.getsource(function), function.__qualname__


def test_a_capture_that_was_never_configured_is_not_called_empty(tmp_path: Any) -> None:
    """A missing capture configuration is not a zero-byte capture.

    With no capture file configured the codex path synthesizes an empty page
    body. Attaching zero bytes for it would report "the capture held no page
    content (0 bytes)" for what is a configuration gap - the same conflation
    of unrelated conditions this issue exists to remove.
    """
    import json
    from pathlib import Path

    from autoscout.source_collectors.codex_extraction import configured_codex_extraction_inputs

    root = Path(tmp_path)
    extraction_path = root / "extraction.json"
    extraction_path.write_text(json.dumps({"entries": []}), encoding="utf-8")
    config_path = root / "config.json"

    _, _, debug = configured_codex_extraction_inputs(
        config_path,
        {
            "name": "No Capture Configured",
            "url": "https://example.test/list",
            "extraction_response_file": extraction_path.name,
        },
    )

    assert "capture_html_bytes" not in debug
