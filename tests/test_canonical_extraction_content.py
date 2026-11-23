"""Focused tests for the canonical extraction-content document (#467 phase 1)."""

from __future__ import annotations

import json

import pytest

from autoscout.canonical_extraction_content import CANONICAL_EXTRACTION_CONTENT_SCHEMA_VERSION
from autoscout.canonical_extraction_content import CanonicalExtractionContent
from autoscout.canonical_extraction_content import canonical_content_digest
from autoscout.canonical_extraction_content import canonical_json_bytes
from autoscout.canonical_extraction_content import canonical_link_values
from autoscout.canonical_extraction_content import canonical_searchable_text
from autoscout.canonical_extraction_content import canonical_source_identity
from autoscout.canonical_extraction_content import canonicalize_sanitized_capture
from autoscout.canonical_extraction_content import collapse_whitespace


def _capture(html: str, **overrides: object) -> dict[str, object]:
    capture: dict[str, object] = {
        "collection_mode": "codex-capture",
        "source_url": "https://www.example.com/lists/nonfiction/",
        "source_name": "Example Nonfiction List",
        "source_page": "https://www.example.com/lists/nonfiction/",
        "resolved_url": "https://www.example.com/lists/nonfiction/",
        "html": html,
        "reference_date": "2026-08-27",
    }
    capture.update(overrides)
    return capture


BASIC_HTML = (
    "<!doctype html>"
    "<html lang='en-US'><head><title>Weekly List</title>"
    "<style>body { color: red; }</style>"
    "<script>var tracker = 1;</script>"
    "<script type='application/ld+json'>"
    '{"@type": "ItemList", "name": "Nonfiction List", "itemListElement": [{"name": "Book One"}]}'
    "</script>"
    "</head><body>"
    "<!-- generated at 2026-08-27, must not appear -->"
    "<h1>Weekly   Nonfiction\tList</h1>"
    "<a href='https://Example.com/Producto/Libro-ABC/'>Libro ABC</a>"
    "<img src='/covers/libro-abc.jpg' alt='Libro ABC cover'>"
    "<p aria-label='Author: Ana Mentor'>Ana Mentor</p>"
    "</body></html>"
)


class DeterministicBytesTests:
    def test_same_capture_produces_identical_bytes(self) -> None:
        first = canonicalize_sanitized_capture(_capture(BASIC_HTML))
        second = canonicalize_sanitized_capture(_capture(BASIC_HTML))
        assert first.canonical_bytes == second.canonical_bytes
        assert first.sha256 == second.sha256

    def test_capture_key_order_does_not_change_bytes(self) -> None:
        capture = _capture(BASIC_HTML)
        reordered = {key: capture[key] for key in reversed(list(capture))}  # type: ignore[arg-type]
        assert canonical_content_digest(reordered) == canonical_content_digest(capture)

    def test_schema_version_is_recorded_in_the_bytes(self) -> None:
        content = canonicalize_sanitized_capture(_capture(BASIC_HTML))
        document = json.loads(content.canonical_bytes.decode("utf-8"))
        assert document["schema_version"] == CANONICAL_EXTRACTION_CONTENT_SCHEMA_VERSION
        assert f'"schema_version":{CANONICAL_EXTRACTION_CONTENT_SCHEMA_VERSION}'.encode() in content.canonical_bytes

    def test_bytes_are_valid_sorted_compact_json(self) -> None:
        content = canonicalize_sanitized_capture(_capture(BASIC_HTML))
        text = content.canonical_bytes.decode("utf-8")
        assert json.loads(text) == content.document
        canonical = json.dumps(json.loads(text), separators=(",", ":"), sort_keys=True, ensure_ascii=False)
        assert text == canonical


class ExclusionsTests:
    def test_comments_are_excluded(self) -> None:
        content = canonicalize_sanitized_capture(_capture(BASIC_HTML))
        assert "generated at 2026-08-27" not in content.visible_text

    def test_style_content_is_excluded(self) -> None:
        content = canonicalize_sanitized_capture(_capture(BASIC_HTML))
        assert "color: red" not in content.visible_text

    def test_executable_script_content_is_excluded(self) -> None:
        content = canonicalize_sanitized_capture(_capture(BASIC_HTML))
        assert "tracker" not in content.visible_text
        all_text = json.dumps(content.document, ensure_ascii=False)
        assert "var tracker = 1;" not in all_text

    def test_json_ld_structured_data_is_retained(self) -> None:
        content = canonicalize_sanitized_capture(_capture(BASIC_HTML))
        blocks = content.document["content"]["html"]["json_ld"]
        assert len(blocks) == 1
        assert blocks[0]["@type"] == "ItemList"
        assert blocks[0]["itemListElement"][0]["name"] == "Book One"
        assert "ItemList" in canonical_searchable_text(content)

    def test_whitespace_is_collapsed_deterministically(self) -> None:
        assert collapse_whitespace("  Ana\r\nMentor \t writes  ") == "Ana Mentor writes"
        content = canonicalize_sanitized_capture(_capture(BASIC_HTML))
        assert "Weekly Nonfiction List" in content.visible_text
        variant = canonicalize_sanitized_capture(_capture(BASIC_HTML.replace("\t", "    ")))
        assert variant.sha256 == content.sha256


class NestedSurfacesTests:
    def test_identity_html_surface_is_canonicalized(self) -> None:
        capture = _capture(
            BASIC_HTML, identity_html="<div><h2>Example Nonfiction List</h2><span>Week of Aug 27</span></div>"
        )
        content = canonicalize_sanitized_capture(capture)
        assert "Example Nonfiction List" in content.document["content"]["identity_html"]["visible_text_runs"]

    def test_detail_pages_are_canonicalized_in_capture_order_with_metadata(self) -> None:
        capture = _capture(
            BASIC_HTML,
            detail_pages=[
                {
                    "source_page": "https://example.com/lists/nonfiction/",
                    "detail_url": "/books/libro-abc/",
                    "resolved_url": "https://example.com/books/libro-abc/",
                    "html": "<h1>Libro ABC</h1><a href='/buy/libro-abc'>Buy</a>",
                },
                {
                    "source_page": "https://example.com/lists/nonfiction/",
                    "detail_url": "/books/libro-two/",
                    "resolved_url": "https://example.com/books/libro-two/",
                    "html": "<h1>Libro Two</h1>",
                },
            ],
        )
        content = canonicalize_sanitized_capture(capture)
        details = content.document["content"]["detail_pages"]
        assert len(details) == 2
        assert details[0]["detail_url"] == "/books/libro-abc/"
        assert details[0]["resolved_url"] == "https://example.com/books/libro-abc/"
        assert details[0]["html"]["visible_text_runs"] == ["Libro ABC", "Buy"]
        assert details[1]["html"]["visible_text_runs"] == ["Libro Two"]
        assert any(link["value"] == "/buy/libro-abc" for link in details[0]["html"]["link_targets"])
        assert "Libro Two" in canonical_searchable_text(content)
        assert "/buy/libro-abc" in canonical_link_values(content)

    def test_nested_html_scripts_styles_and_comments_do_not_survive(self) -> None:
        capture = _capture(
            BASIC_HTML,
            detail_pages=[
                {
                    "detail_url": "/books/libro-abc/",
                    "html": (
                        "<!-- internal comment -->"
                        "<style>.hidden { display: none; }</style>"
                        "<script>var pixel = 1;</script>"
                        "<p>Libro ABC</p>"
                    ),
                }
            ],
        )
        content = canonicalize_sanitized_capture(capture)
        serialized = json.dumps(content.document, ensure_ascii=False)
        assert "internal comment" not in serialized
        assert "display: none" not in serialized
        assert "var pixel" not in serialized
        assert "Libro ABC" in canonical_searchable_text(content)

    def test_unknown_nested_field_with_html_key_is_canonicalized(self) -> None:
        capture = _capture(
            BASIC_HTML,
            future_sections=[{"section": "awards", "html": "<script>var x = 1;</script><p>Award text</p>"}],
        )
        content = canonicalize_sanitized_capture(capture)
        section = content.document["content"]["future_sections"][0]
        assert section["html"]["visible_text_runs"] == ["Award text"]
        assert "var x" not in json.dumps(content.document)

    def test_absent_nested_surfaces_are_omitted_not_empty(self) -> None:
        content = canonicalize_sanitized_capture(_capture(BASIC_HTML))
        assert "identity_html" not in content.document["content"]
        assert "detail_pages" not in content.document["content"]


class UnknownCaptureFieldTests:
    def test_unknown_capture_fields_survive_automatically(self) -> None:
        capture = _capture(BASIC_HTML, future_list_context={"section": "Top 10", "position": 3})
        content = canonicalize_sanitized_capture(capture)
        assert content.document["content"]["future_list_context"] == {"section": "Top 10", "position": 3}

    def test_reference_date_is_excluded_as_a_wall_clock_capture_stamp(self) -> None:
        # Regression test for the reference-date correction: two captures that
        # differ only by the reference_date wall-clock stamp must produce
        # byte-identical canonical bytes and identical digests, and the field
        # must be absent from the serialized bytes entirely.
        base = canonicalize_sanitized_capture(_capture(BASIC_HTML))
        changed = canonicalize_sanitized_capture(_capture(BASIC_HTML, reference_date="2026-09-03"))
        assert changed.canonical_bytes == base.canonical_bytes
        assert changed.sha256 == base.sha256
        assert b"reference_date" not in base.canonical_bytes
        assert "reference_date" not in base.document["content"]

    def test_reference_date_is_excluded_but_page_context_is_kept(self) -> None:
        # The exclusion removes only the wall-clock stamp; actual source and
        # list-page context still survives into the bytes.
        content = canonicalize_sanitized_capture(_capture(BASIC_HTML))
        assert content.document["source"]["source_page"] == "https://www.example.com/lists/nonfiction/"
        assert content.document["source"]["resolved_url"] == "https://www.example.com/lists/nonfiction/"
        assert content.document["content"]["html"]["visible_text_runs"]
        assert content.document["content"]["html"]["link_targets"]
        assert content.document["content"]["html"]["retained_attributes"]
        assert content.document["content"]["html"]["json_ld"]

    def test_transport_only_collection_mode_is_excluded_deliberately(self) -> None:
        content = canonicalize_sanitized_capture(_capture(BASIC_HTML))
        assert "collection_mode" not in content.document["content"]

    def test_unknown_string_fields_are_whitespace_collapsed(self) -> None:
        capture = _capture(BASIC_HTML, list_header="Weekly  \t picks ")
        content = canonicalize_sanitized_capture(capture)
        assert content.document["content"]["list_header"] == "Weekly picks"


class SourceIdentityAndContextTests:
    def test_source_identity_block_preserved(self) -> None:
        content = canonicalize_sanitized_capture(_capture(BASIC_HTML))
        source = content.document["source"]
        assert source["source_name"] == "Example Nonfiction List"
        assert source["source_url"] == "https://www.example.com/lists/nonfiction/"
        assert source["source_page"] == "https://www.example.com/lists/nonfiction/"
        assert source["resolved_url"] == "https://www.example.com/lists/nonfiction/"

    def test_source_identity_reuses_normalize_source_identity_choice(self) -> None:
        # Documented deliberate choice: normalize_source_identity lowercases
        # scheme and host, strips www., removes trailing slashes, and retains
        # the character case of the path itself.
        assert (
            canonical_source_identity("https://WWW.Example.com/Producto/Libro-ABC/")
            == "https://example.com/Producto/Libro-ABC"
        )
        assert "Libro-ABC" in canonical_source_identity("https://www.example.com/Producto/Libro-ABC/")

    def test_case_sensitive_url_example_keeps_path_case_in_content(self) -> None:
        # The exact example required by the issue: the raw link value must
        # survive with its case; only the source identity normalization may
        # change scheme/host case, never content link values.
        html = "<a href='https://Example.com/Producto/Libro-ABC/'>Libro ABC</a>"
        content = canonicalize_sanitized_capture(_capture(html, source_url="https://Example.com/Producto/"))
        assert content.document["source"]["source_identity"] == "https://example.com/Producto"
        assert (
            content.document["content"]["html"]["link_targets"][0]["value"] == "https://Example.com/Producto/Libro-ABC/"
        )


class SkipDepthRobustnessTests:
    def test_stray_end_tag_cannot_close_another_skip_frame(self) -> None:
        html = "<style>body { color: red; }</style></style><p>Visible</p>"
        content = canonicalize_sanitized_capture(_capture(html))
        assert "Visible" in content.visible_text

    def test_unclosed_style_then_real_content_stays_excluded(self) -> None:
        html = "<style>.a {}<p>never visible"
        content = canonicalize_sanitized_capture(_capture(html))
        assert "never visible" not in content.visible_text

    def test_two_consecutive_scripts_both_skipped(self) -> None:
        html = "<script>var a = 1;</script><script>var b = 2;</script><p>Visible</p>"
        content = canonicalize_sanitized_capture(_capture(html))
        assert "var a" not in json.dumps(content.document)
        assert "var b" not in json.dumps(content.document)
        assert "Visible" in content.visible_text

    def test_json_ld_after_executable_script_is_retained(self) -> None:
        html = '<script>var a = 1;</script><script type=\'application/ld+json\'>{"name": "Book One"}</script>'
        content = canonicalize_sanitized_capture(_capture(html))
        assert content.document["content"]["html"]["json_ld"] == [{"name": "Book One"}]


class MissingFieldTests:
    def test_missing_html_field_is_rejected(self) -> None:
        capture = _capture(BASIC_HTML)
        del capture["html"]
        with pytest.raises(ValueError, match="html"):
            canonicalize_sanitized_capture(capture)

    def test_canonical_json_bytes_is_stable(self) -> None:
        document: dict[str, object] = {"b": 1, "a": ["x", "y"], "nested": {"z": True}}
        assert canonical_json_bytes(document) == b'{"a":["x","y"],"b":1,"nested":{"z":true}}'


class SurfaceIterationTests:
    def test_searchable_text_covers_every_surface_type(self) -> None:
        capture = _capture(
            BASIC_HTML,
            identity_html="<span>Identity text</span>",
            detail_pages=[{"html": "<p>Detail text</p>"}],
        )
        content: CanonicalExtractionContent = canonicalize_sanitized_capture(capture)
        text = canonical_searchable_text(content)
        assert "Weekly Nonfiction List" in text
        assert "Identity text" in text
        assert "Detail text" in text
        assert "Libro ABC cover" in text
        assert "Book One" in text

    def test_link_values_cover_every_surface_type(self) -> None:
        capture = _capture(
            BASIC_HTML,
            detail_pages=[{"html": "<a href='/detail-link'>x</a>"}],
        )
        content = canonicalize_sanitized_capture(capture)
        values = canonical_link_values(content)
        assert "https://Example.com/Producto/Libro-ABC/" in values
        assert "/detail-link" in values
