"""Source-agnostic page identity and exact-list grounding contracts."""

from autoscout.source_page_identity import entry_section_html
from autoscout.source_page_identity import exact_book_list_mention_is_grounded
from autoscout.source_page_identity import parse_heading_texts
from autoscout.source_page_identity import parse_hidden_text
from autoscout.source_page_identity import ranking_table_rows
from autoscout.source_page_identity import validate_page_shape

HTML = (
    "<h1>The Prize announces 2025 shortlist</h1>"
    '<ul><li><a href="https://example.test/books/one">Book One</a><span>Jane Author</span></li>'
    '<li><a href="https://example.test/books/two">Book Two</a><span>John Writer</span></li></ul>'
)


def test_page_heading_parses_nested_markup() -> None:
    markup = "<h1>The Prize <span>announces <strong>2025</strong></span> shortlist</h1>"
    assert parse_heading_texts(markup) == ["The Prize announces 2025 shortlist"]


def test_page_heading_excludes_display_none_text() -> None:
    markup = '<h1 style="display:none">Hidden heading</h1><h1>Visible heading</h1>'
    assert parse_heading_texts(markup) == ["Visible heading"]


def test_hidden_text_requires_explicit_opt_in_accessor() -> None:
    markup = (
        "<div hidden>ISBN: 978-1-234</div><script>const captcha = window.widget;</script>"
        "<style>.captcha { display: none; }</style><template>template text</template>"
        "<noscript>noscript text</noscript><p>Visible label</p>"
    )
    assert parse_heading_texts(markup) == []
    assert parse_hidden_text(markup) == "ISBN: 978-1-234"


def test_page_heading_excludes_all_supported_hidden_attributes_and_ancestors() -> None:
    hidden_attributes = (
        'style="display:none"',
        'style="display: none"',
        'style="display:none!important"',
        'style="display: none !important"',
        'style="visibility:hidden"',
        'style="visibility: hidden"',
        'style="visibility:hidden!important"',
        'style="visibility: hidden !important"',
        "hidden",
        'aria-hidden="true"',
    )
    for attribute in hidden_attributes:
        markup = f"<h1 {attribute}><span>Hidden heading</span></h1><h1>Visible heading</h1>"
        assert parse_heading_texts(markup) == ["Visible heading"]

    nested = (
        '<h1 hidden><span aria-hidden="false">Hidden ancestor</span></h1>'
        '<h1>Visible <span style="display: none">child</span> heading</h1>'
        '<h1><span aria-hidden="true">Only hidden child</span></h1>'
    )
    assert parse_heading_texts(nested) == ["Visible heading"]


def test_ranking_table_rows_excludes_hidden_cells_and_rows() -> None:
    markup = (
        "<table><tr><td>1</td><td>Visible book</td><td><span hidden>secret ISBN</span></td></tr>"
        "<tr hidden><td>2</td><td>Hidden book</td></tr>"
        '<tr><td style="visibility: hidden">3</td><td>Only visible cell</td></tr></table>'
    )
    rows = ranking_table_rows(markup)
    assert rows == [["1", "Visible book", ""], ["Only visible cell"]]
    assert all("secret" not in cell.casefold() for row in rows for cell in row)


def test_exact_book_list_mention_is_grounded_accepts_verified_entry() -> None:
    assert exact_book_list_mention_is_grounded(
        HTML,
        "https://example.test/list",
        title="Book One",
        authors=["Jane Author"],
        evidence_url="https://example.test/books/one",
    )


def test_exact_book_list_mention_is_grounded_rejects_fabricated_title() -> None:
    assert not exact_book_list_mention_is_grounded(
        HTML,
        "https://example.test/list",
        title="Fake Book",
        authors=["Jane Author"],
        evidence_url="https://example.test/books/one",
    )


def test_exact_book_list_mention_is_grounded_rejects_fabricated_author() -> None:
    assert not exact_book_list_mention_is_grounded(
        HTML,
        "https://example.test/list",
        title="Book One",
        authors=["Nobody Visible"],
        evidence_url="https://example.test/books/one",
    )


def test_exact_book_list_mention_is_grounded_rejects_cross_entry_author() -> None:
    assert not exact_book_list_mention_is_grounded(
        HTML,
        "https://example.test/list",
        title="Book One",
        authors=["John Writer"],
        evidence_url="https://example.test/books/one",
    )


def test_page_identity_uses_heading_only_not_table_markers() -> None:
    source = {"expected_page_heading": "The Prize announces 2025 shortlist"}
    page = {"html": "<h1>The Prize announces 2025 shortlist</h1><p>No ranking table is present.</p>"}
    assert validate_page_shape(source, page) == ""


def test_validate_page_shape_requires_exact_heading() -> None:
    source = {"expected_page_heading": "The Prize announces 2025 shortlist"}
    assert "expected page heading" in validate_page_shape(
        source, {"html": "<h1>The Prize announces 2024 shortlist</h1>"}
    )
    assert validate_page_shape(source, {"html": "<h1>The Prize announces 2025 shortlist</h1>"}) == ""


def test_forbidden_markers_fail_closed_when_identity_unreadable() -> None:
    """A declared forbidden-marker contract must not silently pass when page
    headings cannot be parsed (docs/architecture.md fail-closed rule).
    Forbidden headings are parsed independently across all visible h1-h6, so
    an unrelated visible_title_selector never narrows the check."""
    source = {"forbidden_visible_markers": ["The shortlist"]}
    assert "cannot be verified" in validate_page_shape(source, {"html": "<p>No headings at all.</p>"})
    selector_ignored = {
        "forbidden_visible_markers": ["The shortlist"],
        "visible_title_selector": ".missing-heading",
    }
    assert validate_page_shape(selector_ignored, {"html": "<h1>2026 Prize</h1><h2>The longlist</h2>"}) == ""
    assert "forbidden visible markers found" in validate_page_shape(
        selector_ignored, {"html": "<h1>2026 Prize</h1><h2>The shortlist</h2>"}
    )
    assert validate_page_shape(source, {"html": "<h1>2026 Prize</h1><h2>The longlist</h2>"}) == ""
    assert "forbidden visible markers found" in validate_page_shape(
        source, {"html": "<h1>2026 Prize</h1><h2>The longlist</h2><h2>The shortlist</h2>"}
    )


def test_forbidden_markers_ignore_narrow_title_selector() -> None:
    """A selector matching only the page title must not hide a separate forbidden h2."""
    source = {
        "forbidden_visible_markers": ["The shortlist"],
        "visible_title_selector": ".page-title",
    }
    assert "forbidden visible markers found" in validate_page_shape(
        source, {"html": '<h1 class="page-title">2026 Prize</h1><h2>The shortlist</h2>'}
    )
    assert validate_page_shape(source, {"html": '<h1 class="page-title">2026 Prize</h1><h2>The longlist</h2>'}) == ""


def test_author_prefix_grounding_accepts_title_containing_colon() -> None:
    """Author: Title: Subtitle — first-colon partition preserves full title."""
    html = '<a href="https://example.test/books/one"><span>Jane Author:</span><span>Full Title: The Subtitle</span></a>'
    assert exact_book_list_mention_is_grounded(
        html,
        "https://example.test/",
        title="Full Title: The Subtitle",
        authors=["Jane Author"],
        evidence_url="https://example.test/books/one",
    )


SECTIONED_HTML = (
    "<h1>2026 Prize</h1>"
    "<section><header><h2>The shortlist</h2></header>"
    '<article><a href="https://example.test/books/one"><h2>Book One</h2><p>Jane Author</p></a></article>'
    "</section>"
    "<section><header><h2>The longlist</h2></header>"
    '<article><a href="https://example.test/books/two"><h2>Book Two</h2><p>John Writer</p></a></article>'
    "</section>"
)


def test_entry_section_html_returns_only_the_headed_section() -> None:
    section = entry_section_html(SECTIONED_HTML, "the  Shortlist")
    assert section is not None
    assert "Book One" in section
    assert "Book Two" not in section


def test_entry_section_html_ignores_a_book_card_title_that_repeats_the_heading() -> None:
    """A heading inside a link is a card title. A book called like the section
    must not turn its own card's section into the declared one."""
    markup = SECTIONED_HTML.replace("<h2>Book Two</h2>", "<h2>The shortlist</h2>")
    section = entry_section_html(markup, "The shortlist")
    assert section is not None
    assert "Book One" in section
    assert "John Writer" not in section


def test_entry_section_html_fails_closed_unless_exactly_one_section_matches() -> None:
    assert entry_section_html(SECTIONED_HTML, "The winner") is None
    assert entry_section_html(SECTIONED_HTML, "  ") is None
    repeated = SECTIONED_HTML.replace("The longlist", "The shortlist")
    assert entry_section_html(repeated, "The shortlist") is None
    unsectioned = SECTIONED_HTML.replace("<section>", "<div>").replace("</section>", "</div>")
    assert entry_section_html(unsectioned, "The shortlist") is None
    unclosed = SECTIONED_HTML.replace("</section>", "")
    assert entry_section_html(unclosed, "The shortlist") is None


def test_entry_section_html_binds_a_heading_to_its_innermost_section() -> None:
    markup = f"<section><h2>Year 2026</h2>{SECTIONED_HTML}</section>"
    outer = entry_section_html(markup, "Year 2026")
    assert outer is not None and "Book One" in outer and "Book Two" in outer
    inner = entry_section_html(markup, "The shortlist")
    assert inner is not None and "Book Two" not in inner


def test_page_shape_requires_the_declared_entry_section() -> None:
    source = {"entry_section_heading": "The shortlist"}
    assert validate_page_shape(source, {"html": SECTIONED_HTML}) == ""
    missing = SECTIONED_HTML.replace("The shortlist", "The finalists")
    assert "entry section 'The shortlist' not found exactly once" in validate_page_shape(source, {"html": missing})


def test_entry_section_html_never_selects_a_hidden_section() -> None:
    """The returned markup starts at the section tag, so an ancestor's
    visibility has to be decided here: a hidden section's cards must not come
    back looking visible."""
    for wrapper in ("<div hidden>{}</div>", '<div aria-hidden="true">{}</div>', '<div style="display:none">{}</div>'):
        hidden = SECTIONED_HTML.replace(
            "<section><header><h2>The shortlist</h2></header>",
            wrapper.split("{}")[0] + "<section><header><h2>The shortlist</h2></header>",
        ).replace("</section><section>", "</section>" + wrapper.split("{}")[1] + "<section>", 1)
        assert "Book One" in hidden
        assert entry_section_html(hidden, "The shortlist") is None
    own = SECTIONED_HTML.replace("<section>", "<section hidden>", 1)
    assert entry_section_html(own, "The shortlist") is None
    hidden_heading = SECTIONED_HTML.replace("<h2>The shortlist</h2>", "<h2 hidden>The shortlist</h2>")
    assert entry_section_html(hidden_heading, "The shortlist") is None


def test_entry_section_html_prefers_the_visible_section_over_a_hidden_twin() -> None:
    twin = '<div hidden><section><h2>The shortlist</h2><a href="/x">Hidden Book</a></section></div>'
    section = entry_section_html(twin + SECTIONED_HTML, "The shortlist")
    assert section is not None
    assert "Book One" in section and "Hidden Book" not in section
