"""Unit tests for the pure HTML weekly-review builder."""

from __future__ import annotations

from autoscout.html_review import _link
from autoscout.html_review import build_html_review


def _row(**overrides):
    row = {
        "idx": 1,
        "identity_key": "book-one|alex-example",
        "title": "Book One",
        "authors": "Alex Example",
        "update_delta": "new",
        "priority": "A",
        "signal_count": "2",
        "russian": "not found in Russian",
        "russian_url": "",
        "risk": "no visible risk",
        "risk_comment": "",
        "editor_status": "",
        "editor_note": "",
        "description": "Описание книги.",
        "publisher": "Example Press",
        "publication_year": "2026",
        "why_entered": "Won a major award.",
        "signal_summary": "Award winner; two media lists.",
        "source": "National Book Awards",
        "source_url": "https://example.test/award",
        "taste_notes": "",
        "links": "",
    }
    row.update(overrides)
    return row


def _summary(**overrides):
    summary = {
        "delta_counts": {"new": 1, "upgraded": 0, "changed": 0, "unchanged": 0},
        "hidden_by_rule_count": 0,
        "editor_rows_count": 1,
        "total_count": 1,
    }
    summary.update(overrides)
    return summary


class BuildHtmlReviewTests:
    def test_is_a_full_self_contained_document(self):
        html = build_html_review([_row()], _summary())
        assert html.lstrip().startswith("<!doctype html>")
        assert "<html" in html and "</html>" in html
        # Inline styles and inline script only — no external assets.
        assert "<style>" in html
        assert "<script>" in html  # the view-only column sorter is inlined
        assert "src=" not in html  # no external scripts/images
        assert "cdn" not in html.lower()  # no CDN references

    def test_columns_are_sortable_headers(self):
        html = build_html_review([_row()], _summary())
        # Every column header is a clickable sorter with a key and type.
        for key in ("num", "title", "author", "priority", "signal", "source", "editor"):
            assert f'data-key="{key}"' in html, key
        assert 'data-key="russian"' not in html
        assert 'data-key="risk"' not in html
        assert ">Источник<" in html
        assert ">Риск<" not in html
        assert 'class="sortable"' in html

    def test_rows_carry_sortable_data_attributes(self):
        html = build_html_review(
            [_row(priority="A", signal_count="3", source="Die Zeit Sachbuch-Bestenliste", editor_status="interested")],
            _summary(),
        )
        assert 'data-priority="0"' in html  # A ranks first
        assert 'data-signal="3"' in html
        assert 'data-source="zeit"' in html
        assert 'data-editor="0"' in html  # interested ranks first

    def test_blank_ordinal_values_sort_last(self):
        html = build_html_review([_row(priority="", editor_status="")], _summary())
        assert 'data-priority="9"' in html
        assert 'data-editor="9"' in html

    def test_this_week_banner_reports_all_bucket_counts(self):
        html = build_html_review(
            [_row(update_delta="upgraded")],
            _summary(
                delta_counts={"new": 3, "upgraded": 2, "changed": 1, "unchanged": 7},
                hidden_by_rule_count=4,
            ),
        )
        assert "За эту неделю" in html
        for token in ("новых — 3", "улучшенных — 2", "изменённых — 1", "без изменений — 7", "скрыто правилами — 4"):
            assert token in html, token

    def test_banner_distinguishes_description_warnings_from_collection_failures(self):
        html = build_html_review(
            [_row(description="Описание требует дополнительной проверки")],
            _summary(
                cumulative_counts={
                    "visible_total_count": 1,
                    "description_coverage": {
                        "validated_count": 0,
                        "fallback_count": 1,
                        "coverage": "partial",
                        "total_candidates": 1,
                    },
                }
            ),
        )

        assert "Русские описания: проверено — 0 из 1, требуют дополнения — 1." in html
        assert "предупреждение о дополнении данных" in html
        assert "не сбой сбора" in html

    def test_groups_candidates_under_dated_sections_with_counts(self):
        # Product rule changed by #1057 (authorized by the operator in the
        # issue on 2026-11-13): the sections are the dates the books were
        # added, newest first, not the lifecycle groups.
        rows = [
            _row(idx=1, identity_key="a", title="Alpha", update_delta="new", first_seen_date="2026-05-21"),
            _row(idx=2, identity_key="b", title="Bravo", update_delta="unchanged", first_seen_date="2026-05-14"),
        ]
        html = build_html_review(
            rows,
            _summary(delta_counts={"new": 1, "upgraded": 0, "changed": 0, "unchanged": 1}, total_count=2),
        )
        assert '<h2>21 мая 2026 <span class="count">(1)</span></h2>' in html
        assert '<h2>14 мая 2026 <span class="count">(1)</span></h2>' in html
        for heading in ("Новые", "Обновлённые", "Ранее просмотренные", ">Улучшенные", ">Изменённые", ">Без изменений"):
            assert heading not in html, heading
        # The newer date comes first.
        assert html.index("Alpha") < html.index("Bravo")

    def test_empty_group_is_omitted(self):
        html = build_html_review(
            [_row(update_delta="new")],
            _summary(delta_counts={"new": 1, "upgraded": 0, "changed": 0, "unchanged": 0}),
        )
        # No candidates are updated or previously seen, so those sub-tables
        # should not render an (empty) section body.
        assert ">Updated" not in html
        assert ">Previously seen" not in html

    def test_editor_decision_drives_row_state_class(self):
        interested = build_html_review([_row(editor_status="interested")], _summary())
        assert 'class="cand cand--decision-1"' in interested

        rejected = build_html_review([_row(editor_status="reject")], _summary())
        assert 'class="cand cand--decision-3"' in rejected

    def test_blank_editor_status_yields_neutral_row(self):
        html = build_html_review([_row(editor_status="")], _summary())
        # The row element carries no state modifier class (the modifiers still
        # exist in the stylesheet, so check the applied class attribute).
        assert 'class="cand"' in html
        assert 'class="cand cand--' not in html
        # A neutral row still renders and still carries a priority badge.
        assert "badge--prio-A" in html

    def test_priority_renders_as_badge_without_risk_column(self):
        html = build_html_review(
            [_row(priority="D")],
            _summary(),
        )
        assert "badge--prio-D" in html
        assert ">Риск<" not in html

    def test_source_column_shows_country_flag_short_name_and_link(self):
        html = build_html_review(
            [
                _row(
                    source="Die Zeit Sachbuch-Bestenliste",
                    source_url="https://www.zeit.de/sachbuch-bestenliste",
                )
            ],
            _summary(),
        )
        assert "🇩🇪" in html
        assert "Zeit" in html
        assert 'href="https://www.zeit.de/sachbuch-bestenliste"' in html
        assert 'title="Die Zeit Sachbuch-Bestenliste"' in html

    def test_source_column_uses_apnews_domain_name_and_us_flag(self):
        html = build_html_review(
            [
                _row(
                    source="Current Weekly Nonfiction Agent Extraction",
                    source_url="https://apnews.com/article/example",
                )
            ],
            _summary(),
        )
        assert "🇺🇸" in html
        assert "APNews" in html
        assert "Weekly Agent" not in html
        assert 'href="https://apnews.com/article/example"' in html

    def test_source_column_uses_canadian_flag_for_audiobooks_com(self):
        html = build_html_review(
            [
                _row(
                    source="Audiobooks.com Nonfiction Audiobook Bestsellers",
                    source_url="https://www.audiobooks.com/browse/non-fiction",
                )
            ],
            _summary(),
        )
        assert "🇨🇦" in html
        assert "Audiobooks.com" in html

    def test_source_column_uses_netherlands_flag_for_cpnb(self):
        html = build_html_review(
            [
                _row(
                    source="CPNB Top Non-Fictie",
                    source_url="https://www.cpnb.nl/boeken/top-100",
                )
            ],
            _summary(),
        )
        assert "🇳🇱" in html
        assert "CPNB" in html

    def test_detail_panel_carries_rich_fields(self):
        long_title = "A Complete and Unabridged Book Title That Does Not Fit in the Summary Row"
        html = build_html_review(
            [
                _row(
                    title=long_title,
                    description="Rich description here.",
                    why_entered="Because reasons.",
                    signal_summary="Signal detail.",
                    taste_notes="matches your preference: narrative science",
                )
            ],
            _summary(),
        )
        assert "<details" in html
        for token in (
            long_title,
            "Example Press / 2026",
            "Rich description here.",
            "Because reasons.",
            "Signal detail.",
        ):
            assert token in html, token
        assert "Открыть страницу книги" not in html

    def test_taste_profile_notes_are_kept_out_of_the_card(self):
        # The note quotes the editor's own standing preference back at her in
        # full; it stays in the data and the CSV (operator's decision, 2026-11-16).
        note = "Soft preference taste-0003 (negative) nudged priority from C to D"
        html = build_html_review([_row(taste_notes=note)], _summary())
        assert note not in html

    def test_source_and_evidence_urls_become_links(self):
        html = build_html_review(
            [
                _row(
                    source_url="https://example.test/award",
                    russian_url="https://ozon.test/book",
                    links="https://example.test/book | https://example.test/list",
                )
            ],
            _summary(),
        )
        assert '<a href="https://example.test/award"' in html
        assert "https://ozon.test/book" not in html
        assert '<a href="https://example.test/book"' in html
        assert '<a href="https://example.test/list"' in html

    def test_detail_layout_wraps_long_urls_without_truncating_content(self):
        long_url = "https://example.test/" + ("unbroken-segment-" * 14)
        long_description = "A complete long description that must remain visible. " + ("Additional context. " * 20)
        assert len(long_url) > 200

        html = build_html_review(
            [_row(links=long_url, description=long_description)],
            _summary(),
        )

        assert "grid-template-columns: 9rem minmax(0, 1fr);" in html
        assert ".detail dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }" in html
        assert ".detail a { color: var(--teal-2); overflow-wrap: anywhere; }" in html
        assert ".ellip { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }" in html
        assert long_url in html
        assert long_description in html

    def test_russian_edition_and_decision_hint_are_hidden_from_html(self):
        row = _row(
            russian="not found in Russian",
            russian_url="https://ozon.test/private-edition",
            russian_verification={"check_coverage": "not_run"},
        )
        summary = _summary(russian_verification_breakdown={"not_run": 1})
        html = build_html_review([row], summary)

        assert "data-russian=" not in html
        assert "Русское издание" not in html
        assert "Проверка русского издания" not in html
        assert "Подсказка для решения" not in html
        assert "https://ozon.test/private-edition" not in html

    def test_detail_carries_verbatim_editor_note_without_technical_feedback_snippet(self):
        html = build_html_review(
            [_row(identity_key="book-one|alex", editor_status="reject", editor_note="Too niche for us.")],
            _summary(),
        )
        assert "Too niche for us." in html
        assert "record-feedback" not in html
        assert "book-one|alex" not in html

    def test_all_interpolated_values_are_html_escaped(self):
        html = build_html_review(
            [
                _row(
                    title="Bell & Co <script>alert(1)</script>",
                    editor_note='He said "no" & left',
                )
            ],
            _summary(),
        )
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html
        assert "Bell &amp; Co" in html
        assert "&quot;no&quot;" in html or "&#34;no&#34;" in html

    def test_unknown_delta_row_is_not_silently_dropped(self):
        # A candidate carrying an unexpected update_delta must still render
        # (folded into Previously seen) rather than vanish from the view.
        html = build_html_review(
            [_row(title="Orphan Row", update_delta="???")],
            _summary(delta_counts={"new": 0, "upgraded": 0, "changed": 0, "unchanged": 0}),
        )
        assert "Orphan Row" in html

    def test_cumulative_totals_do_not_replace_weekly_metrics(self):
        # Product rule changed by #1057 (authorized by the operator in the
        # issue on 2026-11-13): the review totals name the books added on the
        # latest run's date and the books marked updated, not the lifecycle
        # groups; the run's own metrics line stays as it was.
        html = build_html_review(
            [
                _row(idx=1, update_delta="unchanged", first_seen_date="2026-05-21"),
                _row(idx=2, update_delta="upgraded", first_seen_date="2026-05-01"),
                _row(idx=3, update_delta="changed", first_seen_date="2026-05-21"),
            ],
            _summary(
                run_date="2026-05-21",
                delta_counts={"new": 0, "upgraded": 0, "changed": 0, "unchanged": 1},
                hidden_by_rule_count=2,
                cumulative_counts={
                    "new_count": 4,
                    "updated_count": 3,
                    "current_unchanged_count": 5,
                    "previously_seen_count": 9,
                    "visible_total_count": 21,
                    "editor_hidden_count": 0,
                },
            ),
        )
        assert (
            "За эту неделю: новых — 0, улучшенных — 0, изменённых — 0, без изменений — 1, скрыто правилами — 2." in html
        )
        assert (
            "Всего: добавлено в последний запуск — 2, с отметкой «обновлено» — 2, видимых — 21, скрытых редактором — 0."
            in html
        )
        assert "ранее просмотренных" not in html

    def test_browser_default_keeps_shared_server_order(self):
        html = build_html_review(
            [
                _row(idx=1, title="Older historical", identity_key="older"),
                _row(idx=2, title="Recent historical", identity_key="recent", priority="A"),
            ],
            _summary(total_count=2),
        )
        assert html.index("Older historical") < html.index("Recent historical")
        assert 'sortTables([{ key: "priority"' not in html

    def test_output_is_deterministic(self):
        rows = [_row(idx=1, identity_key="a"), _row(idx=2, identity_key="b", update_delta="unchanged")]
        summary = _summary(delta_counts={"new": 1, "upgraded": 0, "changed": 0, "unchanged": 1}, total_count=2)
        assert build_html_review(rows, summary) == build_html_review(rows, summary)

    # ------------------------------------------------------------------
    # Added column
    # ------------------------------------------------------------------

    def test_added_column_visible_in_summary_row(self):
        html = build_html_review(
            [_row(first_seen_date="2026-04-15")],
            _summary(),
        )
        assert '<span class="num c-added">2026-04-15</span>' in html
        assert 'data-key="added"' in html

    def test_added_blank_displays_em_dash(self):
        html = build_html_review(
            [_row(first_seen_date="")],
            _summary(),
        )
        assert "—" in html

    def test_added_carries_sortable_data_attribute(self):
        html = build_html_review(
            [_row(first_seen_date="2026-04-15")],
            _summary(),
        )
        assert 'data-added="2026-04-15"' in html

    def test_added_blank_encodes_as_sentinel_for_sorting(self):
        html = build_html_review(
            [_row(first_seen_date="")],
            _summary(),
        )
        assert 'data-added="__blank__"' in html

    def test_added_header_is_a_plain_label(self):
        # Product rule changed by #1057 (authorized by the operator in the
        # issue on 2026-11-13): the sections are the dates, so the Added
        # header no longer opens a flat table sorted by date.
        html = build_html_review([_row()], _summary())
        assert '<span class="plain" data-key="added">Добавлено</span>' in html
        assert 'data-type="date"' not in html

    # ------------------------------------------------------------------
    # Updated only in detail
    # ------------------------------------------------------------------

    def test_updated_renders_in_detail_when_present(self):
        html = build_html_review(
            [_row(last_material_change_date="2026-06-15")],
            _summary(),
        )
        assert "Обновлено" in html
        assert "2026-06-15" in html

    def test_updated_absent_when_blank(self):
        html = build_html_review(
            [_row(last_material_change_date="")],
            _summary(),
        )
        # "Updated" should not appear in the detail section when blank.
        # The word "Updated" only appears in other contexts (banner, header).
        assert "<dt>Обновлено</dt>" not in html

    def test_no_updated_top_level_column(self):
        html = build_html_review(
            [_row()],
            _summary(),
        )
        # The column headers should not include an Updated column.
        assert 'data-key="updated"' not in html

    def test_sort_tie_break_uses_rendered_row_number_stably(self):
        """Equal values tie-break on rendered row order."""
        html = build_html_review(
            [_row()],
            _summary(),
        )
        assert "dataset.idKey" not in html
        assert "parseFloat(a.dataset.num)" in html

    def test_numeric_columns_support_descending_sort(self):
        """Numeric compare() applies dir for descending sort on Priority/Signal/Editor."""
        html = build_html_review(
            [_row()],
            _summary(),
        )
        # Numeric branch computes ndiff and applies dir.
        assert "ndiff = (parseFloat(av) || 0) - (parseFloat(bv) || 0)" in html
        assert 'dir === "desc" ? -ndiff : ndiff' in html
        html = build_html_review(
            [_row(identity_key="book-one|alex-example", first_seen_date="2026-04-15")],
            _summary(),
        )
        assert "data-id-key=" not in html
        assert "book-one|alex-example" not in html

    def test_columns_include_added(self):
        html = build_html_review(
            [_row()],
            _summary(),
        )
        assert 'data-key="added"' in html
        assert "Добавлено" in html


NON_WEB_URLS = [
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "",
    "/relative/path",
    "example.test/book",
]


class LinkSchemeTests:
    def test_only_http_and_https_become_anchors(self):
        assert _link("https://example.test/book", "Book") == (
            '<a href="https://example.test/book" target="_blank" rel="noopener noreferrer">Book</a>'
        )
        assert _link("  HTTP://example.test/book  ").startswith('<a href="HTTP://example.test/book"')
        for url in NON_WEB_URLS:
            html = _link(url, "Label <b>")
            assert "<a" not in html and "href" not in html, url
        assert _link("javascript:alert(1)", "Open <i>") == "Open &lt;i&gt;"
        assert _link("javascript:alert(1)") == "javascript:alert(1)"
        assert _link("") == ""

    def test_review_page_never_renders_a_non_web_url_as_a_link(self):
        bad = "javascript:alert(1)"
        html = build_html_review(
            [
                _row(
                    source_url=bad,
                    russian_url="data:text/html,<script>alert(1)</script>",
                    links=f"{bad} | /relative/path | https://example.test/list",
                )
            ],
            _summary(),
        )
        assert '<a href="javascript' not in html.lower()
        assert '<a href="data:' not in html.lower()
        assert 'href="/relative/path"' not in html
        assert "<script>alert(1)</script>" not in html
        assert '<a href="https://example.test/list"' in html
