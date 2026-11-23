"""Pure builder for the polished, self-contained HTML weekly review.

The output is a single self-contained HTML document (inline CSS and inline
JavaScript, no external assets) so it opens correctly from disk in any browser
and is safe to commit or sync. It is read-only with respect to state: the
editor still records decisions in chat via ``record-feedback``. The only
JavaScript is view-only: the tabs, and a column sorter that re-orders the rows
of each date section. It writes nothing back, and the server-rendered sections
by date read the same when scripting is off.

``build_html_review`` is a pure function of its inputs — no clock, no
randomness — so identical inputs produce a byte-identical document and the
golden-run determinism guarantee holds.
"""

from __future__ import annotations

from html import escape
from typing import Any
from urllib.parse import quote
from urllib.parse import urlparse

from autoscout.content_description import coverage_denominator
from autoscout.content_description import pm_logline_count
from autoscout.content_description import verified_description_count
from autoscout.fit_scores import FIT_ORDER_NOTE
from autoscout.fit_scores import FIT_SCORE_COLUMN
from autoscout.fit_scores import fit_score_text
from autoscout.identity import normalize_work_title
from autoscout.original_edition_presentation import format_original_edition
from autoscout.review_dates import ARCHIVE_DECIDED_SENTENCE
from autoscout.review_dates import UNKNOWN_CHANGE_DATE_HEADING
from autoscout.review_dates import UNKNOWN_DATE_HEADING
from autoscout.review_dates import UPDATED_TAB_NOTE
from autoscout.review_dates import DateSection
from autoscout.review_dates import added_date
from autoscout.review_dates import added_on_run_date_count
from autoscout.review_dates import date_sections
from autoscout.review_dates import long_russian_date
from autoscout.review_dates import marked_updated_count
from autoscout.review_dates import updated_mark_text
from autoscout.review_dates import updated_sections
from autoscout.review_hiding import DECIDED_EDITION_HIDDEN_LABEL
from autoscout.review_presentation import review_context as presentation_review_context
from autoscout.review_presentation import risk_category_label
from autoscout.review_presentation import risk_scope_explanation
from autoscout.review_presentation import visible_risk_coverage_review_text
from autoscout.review_presentation import visible_risk_coverage_summary_text
from autoscout.russian_edition_presentation import format_russian_edition
from autoscout.russian_edition_presentation import russian_edition_mark_status
from autoscout.russian_edition_presentation import russian_edition_short_mark
from autoscout.source_page_identity import left_out_titles_text
from autoscout.source_run_status import summarize_source_runs
from autoscout.source_trial import TRIAL_SOURCES_FIELD
from autoscout.source_trial import trial_mark_text
from autoscout.title_evidence import clean_title
from autoscout.title_evidence import effective_title_kind
from autoscout.title_evidence import format_editor_english_author_display
from autoscout.title_evidence import format_editor_english_title
from autoscout.visible_risk_coverage import VISIBLE_RISK_SCREEN_COVERAGE_FIELD
from autoscout.work_age import format_work_age

# Embedded so the review remains portable when opened directly from disk.
_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="7" fill="#164e4a"/>'
    '<path d="M5 8c4-1 7 0 11 2 4-2 7-3 11-2v17c-4-1-7 0-11 2-4-2-7-3-11-2Z" '
    'fill="#fff4d6"/>'
    '<path d="M16 10v17" stroke="#164e4a" stroke-width="2"/>'
    "</svg>"
)
_FAVICON_URL = "data:image/svg+xml," + quote(_FAVICON_SVG, safe="")

# The cards of each tab are listed under the date they were added, newest
# first (#1057). The books of an archive backfill that still wait for a
# decision form one group per batch instead, placed among the dates on the day
# the batch was applied. Once decided, a book joins its date.
ARCHIVE_GROUP_KEY = "archive"
DATE_GROUP_KEY = "date"
LATEST_RUN_BADGE = "последний запуск"
# The one table the undecided tab shows when it is a continuous list (#1106).
FLAT_GROUP_KEY = "flat"
FLAT_UNDECIDED_HEADING = "Все книги без решения"
FLAT_UNDECIDED_NOTE = (
    "Один список без разделения по датам: сначала книги с самой высокой оценкой соответствия издательству; "
    "книги без оценки идут после них, новые сверху; книги с приоритетом D — в конце. Дата каждой книги — в колонке «Добавлено»."
)

# The page's tabs, in order (#1063): (tab key, label, note shown under the tabs).
# The "all" tab is the timeline of every book; "undecided" is the same markup
# filtered in the browser by the card's decision; "updated"
# holds copies of the recently changed books; rights leads have their own tab.
REVIEW_TABS: tuple[tuple[str, str, str], ...] = (
    ("all", "Все", ""),
    ("undecided", "Без решения", ""),
    ("updated", "Обновлённые", UPDATED_TAB_NOTE),
    (
        "leads",
        "Сделки и права",
        "Анонсы сделок и предложения прав, о которых больше ничего не известно. "
        "Эти книги ещё не вышли, поэтому у многих нет описания. "
        "Когда книгу упомянет другой источник, она перейдёт в общий список.",
    ),
)
TIMELINE_TAB = "all"
# The tabs that filter the timeline instead of having a panel of their own.
FILTER_TABS = frozenset({"undecided"})

# Editor decision -> (row-state modifier class, human label). Editor-decision
# styling is the dominant visual axis (decided in grilling): the row lights up
# once a decision exists; a blank status leaves the row neutral. Kept as one
# mapping so class and label never drift out of lockstep.
EDITOR_DECISION: dict[str, tuple[str, str]] = {
    "interested": ("decision-1", "Интересно"),
    "maybe_watch": ("decision-2", "Наблюдать"),
    "reject": ("decision-3", "Не подходит"),
    "already_known": ("decision-4", "Уже известно"),
    "needs_deeper_analysis": ("decision-5", "Нужен углублённый анализ"),
}

# Sort ranks for the ordinal columns, most-actionable first, so an ascending
# sort surfaces what the editor most wants to see (A-priority, urgent risk,
# decided candidates). Unknown/blank values sort last.
PRIORITY_RANK: dict[str, int] = {"A": 0, "B": 1, "C": 2, "D": 3}
EDITOR_RANK: dict[str, int] = {
    "interested": 0,
    "needs_deeper_analysis": 1,
    "maybe_watch": 2,
    "already_known": 3,
    "reject": 4,
}

# Scannable-row columns: (sort key, header label, sort type). The detail panel
# carries additional editor-facing evidence. Each header with a sort type sorts
# the rows of every date section by that column; the client-side sorter is
# view-only. The Added column is a plain label: the sections are the dates.
# The fit score column is shown only when a book of the review has a recorded
# score (#1105), so a review without one keeps its exact markup.
FIT_COLUMN = ("fit", FIT_SCORE_COLUMN, "num")
# Sorts after every scored book: the first click lists the highest score first.
_UNSCORED_SORT_VALUE = "9"
_FIT_COLUMN_AFTER = "priority"
COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("num", "#", "num"),
    ("title", "Название", "text"),
    ("author", "Автор", "text"),
    ("priority", "Приоритет", "num"),
    ("signal", "Сигналы", "num"),
    ("source", "Источник", "text"),
    ("added", "Добавлено", ""),
    ("editor", "Решение", "num"),
)

_STYLE = """
:root {
  color-scheme: light;
  --ink: #1a2326; --ink-2: #47545a; --ink-3: #62707a;
  --page: #edf1f0; --surface: #fff; --soft: #f5f8f7;
  --line: #d6dedb; --line-2: #e7ecea;
  --teal: #164e4a; --teal-2: #0f6b63; --teal-tint: #e2efec;
  /* Book titles are set in a serif, everything about the book in a sans.
     System faces only: the page must open from disk with no network. */
  --serif: Charter, "Sitka Text", Cambria, Georgia, "PT Serif", "Noto Serif", serif;
  --sans: "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, Roboto, "Helvetica Neue", Arial, sans-serif;
  --bar: 3rem;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 1.75rem 1.5rem 4rem;
  font: 15px/1.5 var(--sans);
  color: var(--ink); background: var(--page);
}
.wrap { max-width: 1180px; margin: 0 auto; }
h1 { font: 600 1.7rem/1.2 var(--serif); margin: 0 0 .9rem; }
a { color: var(--teal-2); }
:focus-visible { outline: 2px solid var(--teal-2); outline-offset: 2px; }
.banner {
  background: var(--surface); border: 1px solid var(--line); border-radius: 8px;
  padding: 1rem 1.25rem; margin: 0 0 1.25rem;
}
.banner .run-date { color: var(--ink-2); font-size: .875rem; }
.banner .week { font-size: 1.05rem; font-weight: 600; line-height: 1.45; margin-top: .15rem; }
/* Every caution sits in one amber panel, one line per problem with a hanging
   sign, so the editor can count the problems at a glance. Reused evidence
   (Issue #384), named stale and missing sources (Issue #825), description
   caveats, and partial ranking coverage share the same caution treatment:
   the report is usable, but its scope is narrower than the page promises. */
.banner .alerts {
  margin-top: .85rem; background: #fff8e6; border: 1px solid #f1dca4;
  border-left: 4px solid #d99b16; border-radius: 6px;
  color: #5c4206; font-size: .9rem; line-height: 1.45;
}
.banner .reuse, .banner .sources, .banner .incomplete { margin: 0; }
.banner .reuse { padding: .55rem .8rem; }
.banner .reuse:has(> .alert) { padding: 0; }
.banner .alerts > * + *, .banner .sources > .alert + .alert { border-top: 1px solid #f1dca4; }
.banner .alert {
  display: grid; grid-template-columns: 1.2rem minmax(0, 1fr); gap: .35rem;
  padding: .55rem .8rem;
}
.banner .alert .ico { color: #8a5800; }
.banner .more-alerts > summary { cursor: pointer; padding: .55rem .8rem; font-weight: 600; }
.banner .more-alerts[open] > summary { border-bottom: 1px solid #f1dca4; }
.banner .more-alerts > * + * { border-top: 1px solid #f1dca4; }
.banner .more-alerts > summary + * { border-top: 0; }
/* The counts of the run are reference material: one click away, so the first
   thing on the page is the books. */
.banner .totals { margin-top: .85rem; padding-top: .7rem; border-top: 1px solid var(--line-2); }
.banner .totals > summary { cursor: pointer; color: var(--teal-2); font-size: .9rem; font-weight: 600; }
.banner .totals-lines {
  color: var(--ink-2); font-size: .875rem; line-height: 1.5; max-width: 80ch;
  margin-top: .6rem; display: grid; gap: .5rem;
}
/* Tabs: each holds one kind of card, so the list to read stays short. */
.tabbar {
  position: sticky; top: 0; z-index: 3; min-height: var(--bar);
  display: flex; align-items: stretch; gap: 1rem;
  margin: 0 0 1.5rem; background: var(--page); border-bottom: 1px solid var(--line);
}
.tabs { display: flex; align-items: stretch; gap: .25rem; min-width: 0; overflow-x: auto; }
.tab {
  display: inline-flex; align-items: center; gap: .45rem; white-space: nowrap;
  padding: 0 .9rem; color: var(--ink-2); text-decoration: none; font-size: .95rem;
  border-bottom: 3px solid transparent; margin-bottom: -1px;
}
.tab:hover { color: var(--ink); }
.tab .n { font-size: .8rem; font-variant-numeric: tabular-nums; color: var(--ink-3); }
.tab.on { color: var(--teal); font-weight: 650; border-bottom-color: var(--teal); }
.tab.on .n { color: var(--teal); }
.tab-panel { margin: 0 0 3rem; scroll-margin-top: calc(var(--bar) + 1rem); }
.tab-title { font: 600 1.35rem/1.25 var(--serif); margin: 0 0 .5rem; }
.tab-title .count { color: var(--ink-3); font: 500 .95rem var(--sans); }
.tab-note { margin: 0 0 1.25rem; max-width: 76ch; color: var(--ink-2); font-size: .92rem; }
.js .tabbar ~ .tab-panel:not(.on) { display: none; }
.js .tabbar ~ .tab-panel > .tab-title { display: none; }
/* The undecided tab filters the one timeline; the script sets data-filter
   on its panel. Without the script the full timeline shows and the filter
   tab is hidden, so no link leads nowhere. */
.tab--filter, .count--undecided { display: none; }
.js .tab--filter { display: inline-flex; }
.tab-panel[data-filter="undecided"] .cand[data-decided="yes"],
.tab-panel[data-filter="undecided"] .group[data-n-undecided="0"],
.tab-panel[data-filter="undecided"] .group h2 .count:not(.count--undecided) { display: none; }
.tab-panel[data-filter="undecided"] .count--undecided { display: inline; }
.group { margin: 0 0 2rem; }
.group h2 {
  font-size: 1.05rem; font-weight: 650; margin: 0 0 .6rem; display: flex; align-items: baseline; gap: .5rem;
  padding-left: .7rem; border-left: 4px solid #9aa1a9;
}
.group h2 .count { color: var(--ink-3); font-weight: 500; font-size: .9rem; }
.group-note { margin: 0 0 .6rem; padding-left: .95rem; max-width: 76ch; color: var(--ink-2); font-size: .9rem; }
.group h2 { flex-wrap: wrap; }
/* The section of the latest run's date says so, the one relative cue on the page. */
.group h2 .latest {
  font-size: .78rem; font-weight: 600; padding: .1rem .5rem;
  border-radius: 999px; background: var(--teal); color: #fff;
}
.group[data-group="archive"] h2 { border-left-color: #8a5a00; }
.group + .group { margin-top: 2.5rem; }
.table { border: 1px solid var(--line); border-radius: 8px; overflow: clip; background: var(--surface); }
.thead, .cand > summary {
  display: grid;
  grid-template-columns: 2.5rem minmax(160px, 3fr) minmax(110px, 2fr) 5.5rem 4.5rem minmax(8rem, 11.5rem) 6.5rem 8rem;
  align-items: start; gap: .75rem;
  padding: .6rem .9rem;
}
.thead {
  font-size: .8rem; font-weight: 600; padding-top: .45rem; padding-bottom: .45rem;
  color: var(--ink-3); background: var(--soft); border-bottom: 1px solid var(--line-2);
  align-items: center;
  /* The header stays in view, under the tabs, while the editor scrolls a long group. */
  position: sticky; top: 0; z-index: 1;
}
.tabbar ~ .tab-panel .thead { top: var(--bar); }
.thead .sortable {
  cursor: pointer; user-select: none; display: inline-flex; align-items: baseline; gap: .15rem;
}
.thead .sortable:hover { color: var(--ink); }
.thead .sortable.active { color: var(--ink); }
.thead .ind { font-size: .9em; width: .6em; }
.cand { border-bottom: 1px solid var(--line-2); }
.cand:last-child { border-bottom: 0; }
.cand > summary { cursor: pointer; list-style: none; position: relative; }
.cand > summary::-webkit-details-marker { display: none; }
.cand > summary::before {
  content: "▸"; color: var(--ink-3); font-size: .8rem;
  position: absolute; left: .2rem; top: .8rem;
}
.cand[open] > summary::before { content: "▾"; }
.cand > summary:hover { background: var(--soft); }
.cand[open] > summary { background: var(--teal-tint); }
.cand .title { font: 400 1.06rem/1.35 var(--serif); overflow-wrap: anywhere; }
.cand[open] .title { font-weight: 700; }
.title-cell {
  min-width: 0; display: flex; flex-direction: column; align-items: flex-start; gap: .2rem;
}
.title-cell .title { width: 100%; min-width: 0; }
.title-cell .sub { color: var(--ink-3); font-size: .82rem; line-height: 1.35; overflow-wrap: anywhere; }
/* An updated book keeps its date and says when it changed. */
.title-cell .upd {
  display: inline-block; padding: .05rem .5rem; border-radius: 999px; white-space: nowrap;
  font-size: .78rem; font-weight: 600; color: #1e4f9c; background: #e8f0fd;
}
/* A Russian-edition finding (#1181): the same pill as the update mark; a confirmed
   edition reads as a caution, a LiveLib-only one stays grey with a dashed edge. */
.title-cell .ru-ed {
  display: inline-block; padding: .05rem .5rem; border-radius: 999px;
  font-size: .78rem; font-weight: 600; overflow-wrap: anywhere;
}
.title-cell .ru-ed--confirmed { color: #8a4b0f; background: #fdf0dd; border: 1px solid #e5b84b; }
.title-cell .ru-ed--unconfirmed { color: #4b5563; background: #eceef1; border: 1px dashed #9ca3af; }
.cand .author { color: var(--ink-2); font-size: .92rem; min-width: 0; line-height: 1.4; overflow-wrap: anywhere; }
.cand .num { color: var(--ink-3); font-variant-numeric: tabular-nums; }
.cand .c-added { font-size: .875rem; }
.cand .cid { font-family: ui-monospace, monospace; user-select: all; }
.ellip { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.title-evidence { display: inline-block; padding: .12rem .45rem; border-radius: 999px; font-size: .8rem; font-weight: 600; }
.title-evidence--published { color: #155e75; background: #e0f2fe; border: 1px solid #7dd3fc; }
.title-evidence--rendering { color: #6b4a00; background: #fff4d6; border: 1px solid #e5b84b; }
.title-evidence--original { color: #475569; background: #f1f5f9; border: 1px solid #cbd5e1; }
.title-evidence--unverified { color: #7f1d1d; background: #fef2f2; border: 1px solid #fecaca; }
.badge {
  display: inline-block; padding: .1rem .55rem; border-radius: 999px;
  font-size: .8rem; font-weight: 700; white-space: nowrap;
  justify-self: start;
}
.badge--prio-A { background: #e3f4e9; color: #146c3f; }
.badge--prio-B { background: #e8f0fd; color: #1e4f9c; }
.badge--prio-C { background: #fdf0dd; color: #8a4b0f; }
.badge--prio-D { background: #eceef1; color: #4b5563; }
.pill { font-size: .875rem; color: var(--ink-2); }
.source-cell { min-width: 0; display: inline-flex; align-items: baseline; gap: .35rem; font-size: .9rem; line-height: 1.35; }
.source-cell .source-flag { flex: 0 0 auto; font-size: 1rem; line-height: 1; }
.source-cell a { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-decoration: none; }
.source-cell a:hover { text-decoration: underline; }
/* A Probation Source is named in violet, a colour used for nothing else. */
.source-cell--trial, .source-cell--trial a { color: #6d28d9; }
.tag {
  display: inline-block; font-size: .8rem; font-weight: 600;
  padding: .05rem .5rem; border-radius: 6px;
  justify-self: start;
}
/* Editor-decision row states (dominant visual axis). */
.cand--decision-1 { background: #f2fbf5; }
.cand--decision-1 .tag { background: #e3f4e9; color: #146c3f; }
.cand--decision-2 .tag { background: #fdf0dd; color: #8a4b0f; }
.cand--decision-4 { opacity: .8; }
.cand--decision-4 .tag { background: #eceef1; color: #3d434a; }
.cand--decision-5 .tag { background: #e8f0fd; color: #1e4f9c; }
.cand--decision-3 { opacity: .6; }
.cand--decision-3 .title { text-decoration: line-through; }
.cand--decision-3 .tag { background: #fdecea; color: #9b1c13; }
.detail { padding: .5rem 1.4rem 1.2rem 3.4rem; background: var(--soft); border-top: 1px solid var(--line-2); }
.detail dl { display: grid; grid-template-columns: 9rem minmax(0, 1fr); gap: .45rem 1rem; margin: .5rem 0; }
.detail dt { color: var(--ink-3); font-size: .85rem; padding-top: .12rem; }
.detail dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
/* A value is running text: it keeps a readable line length on a wide screen. */
.detail dd { max-width: 76ch; }
/* The description leads the card and is set as running text. */
.detail dl > dd:first-of-type:has(> .note) { font: 400 1.04rem/1.6 var(--serif); max-width: 70ch; padding-bottom: .35rem; }
.detail dl > dt:first-of-type:has(+ dd > .note) { padding-top: .3rem; }
.detail .note { white-space: pre-wrap; }
.detail a { color: var(--teal-2); overflow-wrap: anywhere; }
.detail a.shop { display: inline-block; padding: .15rem .6rem; border-radius: 6px; font-size: .875rem; text-decoration: none; }
.detail a.shop--isbn { color: #fff; background: var(--teal); border: 1px solid var(--teal); font-weight: 600; }
.detail a.shop--isbn:hover { background: #0f3b38; }
.detail a.shop--search { color: var(--ink-2); background: transparent; border: 1px dashed #9aa3ad; }
.detail a.shop--search:hover { color: var(--teal-2); border-color: var(--teal-2); }
.detail a.shop + a.shop { margin-left: .4rem; }
.shop__isbn { font-variant-numeric: tabular-nums; }
.shop__hint { margin-left: .5rem; color: var(--ink-3); font-size: .8rem; }
.feedback { margin-top: .8rem; }
.feedback code {
  display: block; background: #1c2024; color: #e8eaed; border-radius: 6px;
  padding: .55rem .7rem; font-size: .8rem; overflow-x: auto; white-space: pre;
}
footer { color: var(--ink-2); font-size: .85rem; margin-top: 2.5rem; }
/* Phone and narrow windows (below the width the eight desktop columns need):
   each row becomes a small card (title, author, then one line of
   badges), and the column headers become a wrapping row of sort buttons, so
   nothing is cut off and nothing scrolls sideways. */
@media (max-width: 1000px) {
  body { padding: 1rem .6rem 3rem; }
  /* Only the row of tabs stays in view on a phone. */
  .tabbar { display: contents; }
  .tabs {
    position: sticky; top: 0; z-index: 3; min-height: var(--bar); margin: 0 -.6rem .7rem; padding: 0 .6rem;
    background: var(--page); border-bottom: 1px solid var(--line);
  }
  .tab { padding: 0 .7rem; }
  .thead, .tabbar ~ .tab-panel .thead { position: static; }
  .banner .alerts { font-size: .85rem; line-height: 1.4; }
  .banner .totals-lines { font-size: .8rem; line-height: 1.45; gap: .4rem; }
  h1 { font-size: 1.4rem; }
  .banner { padding: .75rem .75rem; margin-bottom: 1rem; }
  .banner .week { font-size: 1rem; }
  .banner .alert { padding: .45rem .55rem; gap: .25rem; }
  .thead {
    display: flex; flex-wrap: wrap; gap: .2rem .9rem; padding: .5rem .75rem;
    font-size: .8rem; color: var(--ink-2);
  }
  .thead .sortable { padding: .1rem 0; text-decoration: underline dotted #9aa1a9; text-underline-offset: 3px; }
  /* Only sort buttons stay in the header row; each card labels its own date. */
  .thead .plain { display: none; }
  .cand .c-added { font-size: .8rem; }
  .cand > summary > .pill:last-child { display: none; }
  .cand > summary {
    display: flex; flex-wrap: wrap; align-items: center; gap: .3rem .6rem;
    padding: .7rem .75rem .7rem 1.4rem;
  }
  .cand > summary::before { left: .45rem; top: .85rem; }
  .cand .c-num { flex: 0 0 auto; min-width: 1.6rem; align-self: flex-start; }
  .cand .title-cell { flex: 1 1 calc(100% - 2.6rem); }
  .cand .c-author { flex: 1 1 100%; padding-left: 2.2rem; }
  .cand .badge { margin-left: 2.2rem; }
  .cand .source-cell { flex: 1 1 100%; order: 1; padding-left: 2.2rem; }
  .cand .source-cell a { white-space: normal; overflow-wrap: anywhere; }
  .cand .badge, .cand .c-sig, .cand .c-added, .cand > summary > .tag { order: 2; }
  /* Without the column headers beside them, the bare numbers need a label. */
  .cand .c-sig::before { content: "Сигналы: "; color: var(--ink-3); }
  .cand .c-added::before { content: "Добавлено: "; }
  .detail { padding: .4rem .9rem 1rem 1.4rem; }
  .detail dl { grid-template-columns: minmax(0, 1fr); gap: 0; }
  /* Stacked, a label sits right above its value: a small caption with space above each pair. */
  .detail dt { font-size: .78rem; font-weight: 600; color: var(--ink-3); margin-top: .85rem; padding-top: 0; }
  .detail dd { margin-top: .15rem; line-height: 1.5; }
}
""".strip()

# Added to the page only with the fit score column: one more track in the row
# grid. The title, author and source tracks give up as much minimum width as
# the new track and its gap take, so the row needs no more width than without
# the column and nothing is clipped above the 1000px breakpoint.
_FIT_STYLE = """
.thead, .cand > summary {
  grid-template-columns: 2.5rem minmax(130px, 3fr) minmax(90px, 2fr) 5.5rem 3.5rem 4.5rem minmax(6.5rem, 11.5rem) 6.5rem 8rem;
}
.banner .order-note { color: var(--ink-2); font-size: .9rem; margin-top: .35rem; }
@media (max-width: 1000px) {
  .cand .c-fit { order: 2; }
  .cand .c-fit::before { content: "Оценка: "; color: var(--ink-3); }
}
""".strip()

# Added to the page only with the continuous undecided list (#1106).
_FLAT_STYLE = """
.group[data-group="flat"] { display: none; }
.tab-panel[data-flat] .group[data-group="flat"] { display: block; }
.tab-panel[data-flat] .group:not([data-group="flat"]) { display: none; }
""".strip()

# The continuous undecided list (#1106). The timeline is written once, under
# its dates; when the undecided tab opens, this script moves the undecided
# cards into one table, ordered by the fit score, and moves them back under
# their dates when another tab of the timeline opens. It follows the
# data-filter attribute the tab script sets, and writes nothing back. Without
# the script the undecided tab is the timeline filtered by date, as before.
_FLAT_SCRIPT = (
    """
(function () {
  var panel = document.querySelector('.tab-panel[data-tab="all"]');
  var flat = panel && panel.querySelector('.group[data-group="flat"] .table');
  if (!flat || !window.MutationObserver) return;
  var UNSCORED = "__UNSCORED__";
  function num(card) { return parseFloat(card.dataset.num) || 0; }
  function byNum(a, b) { return num(a) - num(b); }
  function added(card) { return card.dataset.added === "__blank__" ? "" : (card.dataset.added || ""); }
  // The highest score first; the books with no score after them, the newest date first.
  // Priority D books come after all the others, in the same order among themselves.
  function priorityD(card) { return card.dataset.priority === "__PRIORITY_D__" ? 1 : 0; }
  function byFit(a, b) {
    var last = priorityD(a) - priorityD(b);
    if (last) return last;
    var diff = (parseFloat(a.dataset.fit) || 0) - (parseFloat(b.dataset.fit) || 0);
    if (diff) return diff;
    if (a.dataset.fit === UNSCORED && added(a) !== added(b)) return added(a) < added(b) ? 1 : -1;
    return byNum(a, b);
  }
  function cards(root, selector) { return Array.prototype.slice.call(root.querySelectorAll(selector)); }
  function clearSort() {
    window._piSortState = { key: null, dir: "asc" };
    cards(document, ".thead .sortable").forEach(function (h) {
      var ind = h.querySelector(".ind");
      h.classList.remove("active");
      if (ind) ind.textContent = "";
    });
  }
  function apply() {
    var undecided = cards(panel, 'details.cand[data-decided="no"]');
    if (panel.dataset.filter === "undecided") {
      undecided.forEach(function (card) { if (!card._home) card._home = card.parentNode; });
      undecided.sort(byFit).forEach(function (card) { flat.appendChild(card); });
      panel.dataset.flat = "1";
      clearSort();
    } else if (panel.dataset.flat) {
      undecided.forEach(function (card) { if (card._home) card._home.appendChild(card); });
      cards(panel, ".table").forEach(function (table) {
        cards(table, ":scope > details.cand").sort(byNum).forEach(function (card) { table.appendChild(card); });
      });
      delete panel.dataset.flat;
      clearSort();
    }
  }
  new MutationObserver(apply).observe(panel, { attributes: true, attributeFilter: ["data-filter"] });
  apply();
})();
""".strip()
    .replace("__UNSCORED__", _UNSCORED_SORT_VALUE)
    .replace("__PRIORITY_D__", str(PRIORITY_RANK["D"]))
)

# View-only column sorter. Re-orders the rows already in the DOM; writes nothing
# back. Each sortable header sorts the rows of every date section of the open
# tab by one column (click again to reverse); the sections themselves stay in
# date order. The initial DOM order is the shared server-rendered order.
_SCRIPT = """
(function () {
  document.documentElement.classList.add("js");
  // Sorting works on the tab that is open. A page with no tabs is one scope:
  // the whole document.
  var panels = Array.prototype.slice.call(document.querySelectorAll(".tab-panel"));
  var tabbar = document.querySelector(".tabbar");
  var active = null;
  function scope() { return active || document; }
  function compare(a, b, key, type, dir) {
    var av = a.dataset[key] || "", bv = b.dataset[key] || "";
    if (type === "num") {
      var ndiff = (parseFloat(av) || 0) - (parseFloat(bv) || 0);
      return dir === "desc" ? -ndiff : ndiff;
    }
    var cmp = String(av).localeCompare(String(bv));
    return dir === "desc" ? -cmp : cmp;
  }
  function sortRows(rows, specs) {
    rows.sort(function (a, b) {
      for (var i = 0; i < specs.length; i++) {
        var s = specs[i], c = compare(a, b, s.key, s.type, s.dir);
        if (c !== 0) return c;
      }
      return (parseFloat(a.dataset.num) || 0) - (parseFloat(b.dataset.num) || 0);
    });
  }
  function sortPerGroup(specs) {
    // Sort the rows within each section's table; the sections keep their order.
    scope().querySelectorAll(".table").forEach(function (table) {
      var rows = Array.prototype.slice.call(table.querySelectorAll(":scope > details.cand"));
      sortRows(rows, specs);
      rows.forEach(function (r) { table.appendChild(r); });
    });
  }
  function mark(activeKey, dir) {
    document.querySelectorAll(".thead .sortable").forEach(function (h) {
      var ind = h.querySelector(".ind"), on = h.dataset.key === activeKey;
      h.classList.toggle("active", on);
      if (ind) ind.textContent = on ? (dir === "desc" ? "\\u25BC" : "\\u25B2") : "";
    });
  }
  function resetSort() {
    window._piSortState = { key: null, dir: "asc" };
    sortPerGroup([]);
    mark(null, "asc");
  }
  // The undecided tab shows the timeline panel, filtered by CSS.
  var FILTERS = { undecided: 1 };
  var links = Array.prototype.slice.call(document.querySelectorAll(".tabbar .tab"));
  function showTab(name, byEditor) {
    if (!links.some(function (a) { return a.dataset.tab === name; })) name = links.length ? links[0].dataset.tab : "";
    var key = FILTERS[name] ? "all" : name, panel = null;
    panels.forEach(function (p) { if (p.dataset.tab === key) panel = p; });
    panel = panel || panels[0];
    if (!panel) return;
    // The tab being left returns to page order, so a tab always opens the same way.
    if (active && active !== panel) resetSort();
    active = panel;
    if (panel.dataset.tab === "all") panel.dataset.filter = name;
    panels.forEach(function (p) { p.classList.toggle("on", p === panel); });
    links.forEach(function (a) {
      var on = a.dataset.tab === name;
      a.classList.toggle("on", on);
      if (on) { a.setAttribute("aria-current", "true"); } else { a.removeAttribute("aria-current"); }
    });
    if (!byEditor) return;
    try { history.replaceState(null, "", "#tab-" + name); } catch (e) { /* opened from disk */ }
    // A tab opens at its top, not at the scroll position of the tab before it.
    var top = panel.getBoundingClientRect().top + window.scrollY - (tabbar ? tabbar.querySelector(".tabs").offsetHeight : 0) - 16;
    if (window.scrollY > top) window.scrollTo(0, Math.max(0, top));
  }
  window._piSortState = { key: null, dir: "asc" };
  if (tabbar && panels.length) {
    var wanted = /^#tab-(.+)$/.exec(window.location.hash || "");
    showTab(wanted ? wanted[1] : panels[0].dataset.tab, false);
    tabbar.addEventListener("click", function (e) {
      var a = e.target.closest(".tab");
      if (!a) return;
      e.preventDefault();
      showTab(a.dataset.tab, true);
    });
  }
  document.querySelectorAll(".thead .sortable").forEach(function (h) {
    h.addEventListener("click", function () {
      var key = h.dataset.key, type = h.dataset.type || "text";
      var dir = (window._piSortState && window._piSortState.key === key && window._piSortState.dir === "asc") ? "desc" : "asc";
      window._piSortState = { key: key, dir: dir };
      sortPerGroup([{ key: key, type: type, dir: dir }]);
      mark(key, dir);
    });
  });
})();
""".strip()


# Source cautions longer than this many characters of markup fold behind a summary line.
_ALERTS_FOLD_LENGTH = 700


def _is_nonblank(value: Any) -> bool:
    return str(value or "").strip() != ""


def _link(url: str, text: str | None = None) -> str:
    """A new-tab link for an http(s) URL; anything else is only its escaped label.

    Scraped pages can supply ``javascript:``, ``data:`` or relative URLs. Those must
    never become an anchor, so the label alone is shown.
    """
    url = str(url or "").strip()
    if not url:
        return ""
    label = escape(text) if text else escape(url)
    if not url.casefold().startswith(("http://", "https://")):
        return label
    return f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{label}</a>'


def _shop_link_html(row: dict[str, Any]) -> str:
    """An exact-edition ISBN link and a title search look different (issue #978).

    IberLibro, when the row has its link, follows BookFinder in the same style (issue #983).
    """
    url = escape(str(row["shop_url"]), quote=True)
    second_url = escape(str(row.get("iberlibro_url", "") or "").strip(), quote=True)
    isbn = str(row.get("shop_isbn", "") or "").strip()
    kind = "isbn" if isbn else "search"
    first_label = (
        f'BookFinder · ISBN <span class="shop__isbn">{escape(isbn)}</span>'
        if isbn
        else "Искать на BookFinder по названию и автору"
    )
    links = [_shop_anchor(kind, url, first_label)]
    if second_url:
        links.append(_shop_anchor(kind, second_url, "IberLibro" if isbn else "Искать на IberLibro"))
    hint = "это издание в магазинах" if isbn else "ISBN не найден"
    return "".join(links) + f'<span class="shop__hint">{hint}</span>'


def _shop_anchor(kind: str, escaped_url: str, label_html: str) -> str:
    return (
        f'<a class="shop shop--{kind}" href="{escaped_url}" target="_blank" rel="noopener noreferrer">{label_html}</a>'
    )


def _link_list(value: str) -> str:
    links = [part.strip() for part in str(value or "").split(" | ") if part.strip()]
    return " &middot; ".join(_link(link) for link in links)


def _priority_badge(priority: str) -> str:
    label = str(priority or "").strip()
    key = label.upper()[:1]
    cls = f"badge--prio-{key}" if key in {"A", "B", "C", "D"} else "badge--prio-D"
    return f'<span class="badge {cls}">{escape(label or "—")}</span>'


def _editor_tag(status: str) -> str:
    key = str(status or "").strip()
    decision = EDITOR_DECISION.get(key)
    if decision is None:
        return '<span class="pill">—</span>'
    return f'<span class="tag">{escape(decision[1])}</span>'


def _detail_row(label: str, value_html: str) -> str:
    return f"<dt>{escape(label)}</dt><dd>{value_html}</dd>"


def _signal_int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


COUNTRY_FLAGS: dict[str, str] = {
    "argentina": "🇦🇷",
    "australia": "🇦🇺",
    "brazil": "🇧🇷",
    "canada": "🇨🇦",
    "czechia": "🇨🇿",
    "czech republic": "🇨🇿",
    "france": "🇫🇷",
    "germany": "🇩🇪",
    "italy": "🇮🇹",
    "norway": "🇳🇴",
    "netherlands": "🇳🇱",
    "poland": "🇵🇱",
    "spain": "🇪🇸",
    "united kingdom": "🇬🇧",
    "uk": "🇬🇧",
    "united states": "🇺🇸",
    "usa": "🇺🇸",
}

# Single source of truth for editor-facing source display metadata.
# Add or rename a source here once as (country, short name). An empty country
# means "no explicit mapping — fall back to the source_country field, then to
# domain". SOURCE_COUNTRIES and SOURCE_SHORT_NAMES below are derived views
# kept for backward compatibility so callers never update two maps
# independently.
_SOURCE_DISPLAY_METADATA: dict[str, tuple[str, str]] = {
    "Académie française Prix de l’essai": ("France", "Académie française"),
    "Amazon Charts Most Read Nonfiction": ("", "Amazon"),
    "Amazon.de Charts Meistgelesene Sachbücher": ("Germany", "Amazon.de"),
    "Audible Bestselling Business & Careers Audiobooks": ("", "Audible"),
    "Audible Bestselling Computers & Technology Audiobooks": ("", "Audible"),
    "Audible Bestselling Science & Engineering Audiobooks": ("", "Audible"),
    "Australian Book Industry Awards (ABIA) 2026 — Nonfiction": ("Australia", "ABIA"),
    "Audible.de Bestselling Biografien & Erinnerungen Hoerbuecher": ("Germany", "Audible.de"),
    "Audiobooks.com Nonfiction Audiobook Bestsellers": ("", "Audiobooks.com"),
    "Baillie Gifford Prize 2025 Shortlist": ("United Kingdom", "Baillie Gifford"),
    "Baillie Gifford Prize 2026 Longlist": ("United Kingdom", "Baillie Gifford"),
    "Baillie Gifford Prize 2026 Shortlist": ("United Kingdom", "Baillie Gifford"),
    "Baillie Gifford Prize 2026 Winner": ("United Kingdom", "Baillie Gifford"),
    "Beacon Press — Forthcoming": ("", "Beacon Press"),
    "Bokhandlerforeningen Boklista - Generell litteratur voksne": ("Norway", "Boklista"),
    "BookMarks (LitHub) – New Nonfiction": ("United States", "BookMarks"),
    "BookPage Nonfiction": ("United States", "BookPage"),
    "Cundill History Prize 2026 Longlist": ("Canada", "Cundill Prize"),
    "Current Weekly Nonfiction Agent Extraction": ("", "Weekly Agent"),
    "Cúspide 100 más vendidos": ("Argentina", "Cúspide"),
    "CPNB Top Non-Fictie": ("Netherlands", "CPNB"),
    "Deutscher Sachbuchpreis 2024": ("Germany", "Sachbuchpreis"),
    "Die Zeit Sachbuch-Bestenliste": ("Germany", "Zeit"),
    "Goodreads Choice Awards 2025 Nonfiction": ("United States", "Goodreads"),
    "Goodreads Choice Awards 2026 Nonfiction": ("United States", "Goodreads"),
    "IndieBound Indie Next List": ("United States", "IndieBound"),
    "Magnesia Litera 2026 — Naučná literatura a Publicistika": ("Czechia", "Magnesia Litera"),
    "National Book Awards 2025 Nonfiction": ("United States", "National Book Awards"),
    "National Book Awards 2026 Nonfiction": ("United States", "National Book Awards"),
    "National Book Critics Circle Awards": ("United States", "NBCC"),
    "Nagroda Złota Róża 2026 — nominacje": ("Poland", "Nagroda"),
    "Nonfiction.fr Critiques": ("France", "Nonfiction.fr"),
    "PEOPLE Books": ("United States", "PEOPLE"),
    "Premio Strega Saggistica 2026 Cinquina": ("Italy", "Premio Strega"),
    "PublishNews Nielsen Não Ficção Trade": ("Brazil", "PublishNews"),
    "Publishers Marketplace Rights Offerings": ("United States", "Publishers Marketplace"),
    "Publishers Weekly Hardcover Frontlist Nonfiction": ("United States", "Publishers Weekly"),
    "PW Best Books 2025": ("United States", "PW Best Books"),
    "PW Best Books 2026": ("United States", "PW Best Books"),
    "Spiegel Bestseller Sachbuch": ("Germany", "Spiegel"),
    "TodosTusLibros Librerías Recomiendan No Ficción": ("Spain", "TodosTusLibros"),
    "USA TODAY Bestselling Books Nonfiction": ("United States", "USA TODAY"),
    "Verso — Forthcoming": ("", "Verso"),
}

# A bounded fallback for legacy source evidence that did not persist country
# metadata. This is source identity metadata, not a claim about where a book
# was published.
SOURCE_COUNTRIES: dict[str, str] = {
    name: country for name, (country, _short) in _SOURCE_DISPLAY_METADATA.items() if country
}

SOURCE_DOMAIN_COUNTRIES: dict[str, str] = {
    "amazon.de": "Germany",
    "apnews.com": "United States",
    "audible.de": "Germany",
    "audiobooks.com": "Canada",
    "bokhandlerforeningen.no": "Norway",
    "cuspide.com": "Argentina",
    "magnesia-litera.cz": "Czechia",
    "publishnews.com.br": "Brazil",
    "zeit.de": "Germany",
}

SOURCE_DOMAIN_SHORT_NAMES: dict[str, str] = {
    "apnews.com": "APNews",
}

SOURCE_SHORT_NAMES: dict[str, str] = {
    name: short for name, (_country, short) in _SOURCE_DISPLAY_METADATA.items() if short
}


def _source_country(row: dict[str, Any]) -> str:
    explicit = str(row.get("source_country", "")).strip().casefold()
    if explicit in COUNTRY_FLAGS:
        return explicit
    source = str(row.get("source", "")).strip()
    mapped = SOURCE_COUNTRIES.get(source, "").casefold()
    if mapped in COUNTRY_FLAGS:
        return mapped
    host = urlparse(str(row.get("source_url", "")).strip()).netloc.casefold().split(":", 1)[0]
    for domain, country in SOURCE_DOMAIN_COUNTRIES.items():
        if host == domain or host.endswith(f".{domain}"):
            return country.casefold()
    return ""


def _source_short_name(row: dict[str, Any]) -> str:
    source = str(row.get("source", "")).strip()
    host = urlparse(str(row.get("source_url", "")).strip()).netloc.casefold().split(":", 1)[0]
    for domain, short_name in SOURCE_DOMAIN_SHORT_NAMES.items():
        if host == domain or host.endswith(f".{domain}"):
            return short_name
    if source in SOURCE_SHORT_NAMES:
        return SOURCE_SHORT_NAMES[source]
    return source


def _source_cell(row: dict[str, Any], run_date: str = "") -> str:
    """The source name with its flag; violet while the source is a Probation Source.

    The colour is the whole mark. The week of the trial is in the tooltip.
    """
    short_name = _source_short_name(row)
    if not short_name:
        return '<span class="pill">—</span>'
    flag = COUNTRY_FLAGS.get(_source_country(row), "🌐")
    source_url = str(row.get("source_url", "")).strip()
    source_name = str(row.get("source", "")).strip() or short_name
    source_text = escape(short_name)
    if source_url:
        source_text = _link(source_url, short_name)
    trial = next((entry for entry in row.get(TRIAL_SOURCES_FIELD) or [] if entry.get("name") == source_name), None)
    cell_class = "source-cell source-cell--trial" if trial else "source-cell"
    tooltip = trial_mark_text(trial, run_date) if trial else source_name
    return (
        f'<span class="{cell_class}" title="{escape(tooltip, quote=True)}">'
        f'<span class="source-flag" aria-hidden="true">{flag}</span>{source_text}</span>'
    )


def _sort_data_attrs(row: dict[str, Any], status: str, with_fit: bool = False) -> str:
    """Machine-sortable values for the client-side column sorter, one per
    sortable column. Ordinal columns carry a numeric rank; text columns carry a
    lowercased value for case-insensitive comparison. The added date is kept
    as machine-readable data, with a sentinel when it is unknown."""
    prio_key = str(row.get("priority", "")).strip().upper()[:1]
    fit = row.get("fit_score")
    first_seen = added_date(row)
    values = {
        "num": str(row.get("idx", "")),
        "title": str(row.get("title", "")).lower(),
        "author": str(row.get("authors", "")).lower(),
        "priority": str(PRIORITY_RANK.get(prio_key, 9)),
        **({"fit": _UNSCORED_SORT_VALUE if fit is None else f"{-fit:.4f}"} if with_fit else {}),
        "signal": str(_signal_int(row.get("signal_count"))),
        "source": _source_short_name(row).casefold(),
        "added": first_seen if first_seen else "__blank__",
        "editor": str(EDITOR_RANK.get(status, 9)),
        # The undecided filter tab reads this (#1063).
        "decided": "yes" if _has_decision(row) else "no",
    }
    return " ".join(f'data-{key}="{escape(value, quote=True)}"' for key, value in values.items())


def _candidate_html(row: dict[str, Any], run_date: str = "", with_fit: bool = False) -> str:
    status = str(row.get("editor_status", "") or "").strip()
    decision = EDITOR_DECISION.get(status)
    state_cls = decision[0] if decision else ""
    cand_class = "cand" + (f" cand--{state_cls}" if state_cls else "")
    sort_attrs = _sort_data_attrs(row, status, with_fit)
    fit_cell = (
        f'<span class="num c-fit">{escape(fit_score_text(row.get("fit_score")) or "—")}</span>' if with_fit else ""
    )

    # Display Added as YYYY-MM-DD, or — when blank.
    display_added = added_date(row) or "—"
    summary = (
        "<summary>"
        f'<span class="num c-num">{escape(str(row.get("idx", "")))}</span>'
        f'<span class="title-cell"><span class="title">{escape(clean_title(row.get("title", "")))}</span>'
        f"{_lead_buyer_html(row)}{_updated_mark_html(row, run_date)}{_russian_edition_mark_html(row)}</span>"
        f'<span class="author c-author">{escape(str(row.get("authors", "")))}</span>'
        f"{_priority_badge(str(row.get('priority', '')))}"
        f"{fit_cell}"
        f'<span class="pill c-sig">{escape(str(row.get("signal_count", "")))}</span>'
        f"{_source_cell(row, run_date)}"
        f'<span class="num c-added">{escape(display_added)}</span>'
        f"{_editor_tag(status)}"
        "</summary>"
    )

    # The description is what the editor opens a card for, so it leads the
    # card as running text, with its status right under it.
    detail_rows: list[str] = []
    if _is_nonblank(row.get("description")):
        description_html = f'<span class="note">{escape(str(row["description"]))}</span>'
        detail_rows.append(_detail_row("Описание", description_html))
    if _is_nonblank(row.get("description_status")):
        detail_rows.append(_detail_row("Статус описания", escape(str(row["description_status"]))))
    if row.get("earlier_edition_decisions"):
        detail_rows.append(
            _detail_row("Другое издание", "<br>".join(escape(text) for text in row["earlier_edition_decisions"]))
        )
    if row.get("withheld_editions"):
        # Editions left out of the review because of the decision on this card.
        detail_rows.append(
            _detail_row(
                "Другие издания",
                "Не показаны, потому что решение уже принято здесь: " + escape(", ".join(row["withheld_editions"])),
            )
        )
    if _is_nonblank(row.get("title")):
        detail_rows.append(_detail_row("Полное название", escape(clean_title(row["title"]))))
    if _is_nonblank(row.get("authors")):
        detail_rows.append(_detail_row("Автор", escape(str(row["authors"]))))
    publisher_year = " / ".join(
        value
        for value in (str(row.get("publisher", "")).strip(), str(row.get("publication_year", "")).strip())
        if value
    )
    english_title = str(row.get("english_title", "")).strip()
    original_text, original_url, original_label = format_original_edition(row)
    original = row.get("original_edition")
    hide_english = bool(
        original_text
        and isinstance(original, dict)
        and original.get("status") == "confirmed"
        and original.get("original_language") == "en"
        and effective_title_kind(row) == "sourced_published"
        and normalize_work_title(clean_title(english_title))
        == normalize_work_title(clean_title(original["original_title"]))
    )
    if english_title and not hide_english:
        formatted_title = format_editor_english_title(row)
        if formatted_title:
            kind = effective_title_kind(row)
            kind_class = {
                "sourced_published": "title-evidence--published",
                "generated_rendering": "title-evidence--rendering",
                "original": "title-evidence--original",
            }.get(kind, "title-evidence--unverified")
            title_html = f'<span class="title-evidence {kind_class}">{escape(formatted_title)}</span>'
            detail_rows.append(_detail_row("Английское название", title_html))
        sources = row.get("english_title_sources", [])
        if isinstance(sources, list) and sources:
            source_html = " &middot; ".join(_link(str(source), "Открыть источник") for source in sources)
            detail_rows.append(_detail_row("Источники названия", source_html))
    if original_text:
        original_html = escape(original_text)
        if original_url:
            original_html += " · " + _link(original_url, original_label)
        detail_rows.append(_detail_row("Оригинал", original_html))
    russian_text, russian_url, russian_label = format_russian_edition(row)
    if russian_text:
        detail_rows.append(
            _detail_row("Русское издание", escape(russian_text) + " · " + _link(russian_url, russian_label))
        )
    english_author_display = str(row.get("english_author_display", "")).strip()
    if english_author_display:
        detail_rows.append(
            _detail_row(
                "Английское имя автора",
                escape(format_editor_english_author_display(row)),
            )
        )
    if publisher_year:
        detail_rows.append(_detail_row("Издательство / год", escape(publisher_year)))
    work_age_text = format_work_age(row)
    if work_age_text:
        # Plain text only: the age of the work, never a chip or a stripe (#1138).
        detail_rows.append(_detail_row("Возраст произведения", escape(work_age_text)))
    rights_deal = str(row.get("rights_deal", "")).strip()
    if rights_deal:
        detail_rows.append(_detail_row("Сделка о правах", escape(rights_deal)))
    last_material_change = str(row.get("last_material_change_date", "")).strip()
    if last_material_change:
        detail_rows.append(_detail_row("Обновлено", escape(last_material_change)))
    if _is_nonblank(row.get("archive_batch_label")):
        # The archive issue's date stays on the card: the Added column shows the day the batch came in.
        issue_date = str(row.get("first_seen_date", "")).strip()
        archive_text = str(row["archive_batch_label"]) + (f" · выпуск от {issue_date}" if issue_date else "")
        detail_rows.append(_detail_row("Архив", escape(archive_text)))
    rendered_context = presentation_review_context(row.get("review_context"))
    if rendered_context:
        detail_rows.append(_detail_row("Контекст", escape(rendered_context)))
    # Coverage is shown for every row so an absent risk status is never
    # ambiguous (issue #714).
    detail_rows.append(
        _detail_row(
            "Проверка риска", escape(visible_risk_coverage_review_text(row.get(VISIBLE_RISK_SCREEN_COVERAGE_FIELD)))
        )
    )
    # Issue #278 cross-output contract: Markdown is the tabular report and
    # shows the localized risk status. HTML intentionally has no duplicate
    # status detail row; its compact expanded details keep category, rationale,
    # comment, and evidence.
    risk_status = str(row.get("risk", "")).strip()
    if risk_status in {"clear visible risk", "text check required"}:
        risk_category = str(row.get("risk_category", "")).strip()
        detail_rows.append(
            _detail_row(
                "Категория риска", escape(risk_category_label(risk_category)) if risk_category else "Требует уточнения"
            )
        )
        risk_reason = risk_scope_explanation(row.get("risk_scope"))
        if not risk_reason and not risk_category and not _is_nonblank(row.get("risk_comment")):
            risk_reason = (
                "В сохранённых данных нет категории или подтверждающего объяснения. "
                "Риск необходимо проверить по источникам."
            )
        elif not risk_reason and risk_status == "text check required":
            risk_reason = (
                "Публичные данные дают неполный или косвенный сигнал; "
                "необходимо проверить текст и подтверждающие источники."
            )
        elif not risk_reason:
            risk_reason = (
                "Категория или комментарий о риске сохранены, "
                "но основание необходимо сверить с подтверждающим источником."
            )
        detail_rows.append(_detail_row("Обоснование риска", f'<span class="note">{escape(risk_reason)}</span>'))
    if _is_nonblank(row.get("risk_comment")):
        detail_rows.append(
            _detail_row("Комментарий о риске", f'<span class="note">{escape(str(row["risk_comment"]))}</span>')
        )
    if _is_nonblank(row.get("risk_evidence_url")):
        detail_rows.append(
            _detail_row(
                "Источник риска",
                _link(str(row["risk_evidence_url"]), "Открыть подтверждающий источник"),
            )
        )
    # Mechanical depth marker beside the status, in the detail area only:
    # it says the text is shorter than the depth floor, never that it is
    # low quality (issue #672).
    if _is_nonblank(row.get("description_depth_status")):
        detail_rows.append(_detail_row("Глубина описания", escape(str(row["description_depth_status"]))))
    # One provenance line beside the one status, in the detail area only:
    # the always-visible summary gains no description field (issue #644).
    if _is_nonblank(row.get("description_provenance_label")):
        detail_rows.append(_detail_row("Источники описания", escape(str(row["description_provenance_label"]))))
    if _is_nonblank(row.get("description_identity_note")):
        detail_rows.append(_detail_row("Замечание о соответствии", escape(str(row["description_identity_note"]))))
    if _is_nonblank(row.get("description_stage_reason")):
        detail_rows.append(_detail_row("Причина проверки описания", escape(str(row["description_stage_reason"]))))
    if _is_nonblank(row.get("prominence")):
        detail_rows.append(_detail_row("Проминентность", escape(str(row["prominence"]))))
    if _is_nonblank(row.get("prominence_claim_type")):
        detail_rows.append(_detail_row("Тип проминентности", escape(str(row["prominence_claim_type"]))))
    if _is_nonblank(row.get("prominence_evidence_url")):
        urls = [part.strip() for part in str(row["prominence_evidence_url"]).split(" | ") if part.strip()]
        url_html = " &middot; ".join(_link(url, "Открыть доказательство") for url in urls)
        detail_rows.append(_detail_row("URL доказательства проминентности", url_html))
    if _is_nonblank(row.get("autobiographical_subject_context_warning")):
        detail_rows.append(
            _detail_row(
                "Контекст героя",
                f'<span class="note">{escape(str(row["autobiographical_subject_context_warning"]))}</span>',
            )
        )
    if _is_nonblank(row.get("why_entered")):
        detail_rows.append(_detail_row("Почему в списке", escape(str(row["why_entered"]))))
    if _is_nonblank(row.get("signal_summary")):
        detail_rows.append(_detail_row("Сводка сигналов", escape(str(row["signal_summary"]))))
    if _is_nonblank(row.get("source")) or _is_nonblank(row.get("source_url")):
        source_text = str(row.get("source", "")).strip() or str(row.get("source_url", "")).strip()
        source_html = (
            _link(str(row.get("source_url", "")), source_text)
            if _is_nonblank(row.get("source_url"))
            else escape(source_text)
        )
        detail_rows.append(_detail_row("Основной источник", source_html))
    if _is_nonblank(row.get("links")):
        detail_rows.append(_detail_row("Ссылки", _link_list(str(row["links"]))))
    if _is_nonblank(row.get("shop_url")):
        # Links only: AutoScout never fetches BookFinder or IberLibro (issues #975, #983).
        detail_rows.append(_detail_row("Где купить", _shop_link_html(row)))
    if _is_nonblank(row.get("editor_note")):
        detail_rows.append(
            _detail_row("Заметка редактора", f'<span class="note">{escape(str(row["editor_note"]))}</span>')
        )

    # The durable Candidate ID (Issue #851): the short form is shown for the
    # editor to copy, the full ID is kept in an attribute for tools.
    candidate_id = str(row.get("candidate_id", "")).strip()
    short_candidate_id = str(row.get("short_candidate_id", "")).strip()
    if short_candidate_id:
        detail_rows.append(_detail_row("ID", f'<code class="cid">{escape(short_candidate_id)}</code>'))
    id_attr = f' data-candidate-id="{escape(candidate_id, quote=True)}"' if candidate_id else ""

    detail = '<div class="detail">' + (f"<dl>{''.join(detail_rows)}</dl>" if detail_rows else "") + "</div>"
    return f'<details class="{cand_class}" {sort_attrs}{id_attr}>{summary}{detail}</details>'


def _lead_buyer_html(row: dict[str, Any]) -> str:
    """Under the title of a rights lead: who bought the book, the one fact a deal always has."""
    publisher = str(row.get("publisher", "")).strip()
    if not row.get("rights_lead_only") or not publisher:
        return ""
    return f'<span class="sub">Права купило: {escape(publisher)}</span>'


def _updated_mark_html(row: dict[str, Any], run_date: str) -> str:
    """Under the title of an updated book: the mark with the date of its change."""
    text = updated_mark_text(row, run_date)
    return f'<span class="upd">{escape(text)}</span>' if text else ""


def _russian_edition_mark_html(row: dict[str, Any]) -> str:
    """Under the title of a book with a Russian-edition finding: the mark, kept visible (#1181).

    The row says only that a finding exists; the title, publisher, year and
    link are in the «Русское издание» line of the opened card.
    """
    text = russian_edition_short_mark(row)
    if not text:
        return ""
    return f'<span class="ru-ed ru-ed--{russian_edition_mark_status(row)}">{escape(text)}</span>'


def _has_decision(row: dict[str, Any]) -> bool:
    """True when the card has an editor status: it is left out of the undecided tab."""
    return bool(str(row.get("editor_status", "") or "").strip())


def _tab_counts(rows: list[dict[str, Any]], run_date: str) -> dict[str, int]:
    """The number of cards of each tab the page shows; a tab left out has no cards to show.

    The undecided tab is shown only when the review has books of both kinds:
    otherwise it would repeat the "all" tab or be empty. Books with a decision
    have no tab of their own; they stay in the "all" tab.
    """
    books = [row for row in rows if not row.get("rights_lead_only")]
    decided = sum(1 for row in books if _has_decision(row))
    counts = {
        "all": len(books),
        "undecided": len(books) - decided if decided and decided < len(books) else 0,
        "updated": sum(len(section.rows) for section in updated_sections(books, run_date)),
        "leads": len(rows) - len(books),
    }
    return {key: count for key, count in counts.items() if count}


def _has_fit_scores(rows: list[dict[str, Any]]) -> bool:
    """True when a book of the review has a recorded fit score, so the column is shown."""
    return any(row.get("fit_score") is not None for row in rows)


def _flat_undecided(rows: list[dict[str, Any]], summary: dict[str, Any]) -> bool:
    """True when the undecided tab is one continuous list ordered by the fit score (#1106).

    That needs the order by fit score switched on, a recorded score, and the
    undecided tab itself, which the page has only with books of both kinds.
    """
    return (
        bool(summary.get("ordered_by_fit"))
        and _has_fit_scores(rows)
        and "undecided" in _tab_counts(rows, str(summary.get("run_date", "")))
    )


def _tab_panels_html(rows: list[dict[str, Any]], run_date: str = "", *, flat_undecided: bool = False) -> str:
    """The tab bar and the panels: the timeline of every book, the updated books, the leads.

    With *flat_undecided* the timeline starts with an empty table the script
    fills with the undecided cards when the undecided tab opens (#1106).

    The bar is shown when more than one tab has cards; without it the panels
    read as one list. The timeline is written once: the undecided tab
    filters it in the browser by each card's ``data-decided``.
    """
    counts = _tab_counts(rows, run_date)
    tabs = [(key, label, note) for key, label, note in REVIEW_TABS if key in counts]
    with_bar = len(tabs) > 1
    filtered = "undecided" in counts
    books = [row for row in rows if not row.get("rights_lead_only")]
    # The note of an archive block counts the decided books too, and those may be leads.
    archive_groups = {batch: (label, note) for batch, label, note in _archive_groups(rows)}
    with_fit = _has_fit_scores(rows)
    contents = {
        TIMELINE_TAB: (
            _group_section_html(
                f'data-group="{FLAT_GROUP_KEY}"',
                f'{escape(FLAT_UNDECIDED_HEADING)} <span class="count count--undecided">({counts["undecided"]})</span>',
                FLAT_UNDECIDED_NOTE,
                [],
                run_date,
                with_fit,
            )
            if flat_undecided
            else ""
        )
        + "".join(
            _section_html(section, archive_groups, run_date, filtered=filtered, with_fit=with_fit)
            for section in date_sections(books)
        ),
        "updated": "".join(
            _updated_section_html(section, run_date, with_fit) for section in updated_sections(books, run_date)
        ),
        "leads": "".join(
            _section_html(section, archive_groups, run_date, with_fit=with_fit)
            for section in date_sections([row for row in rows if row.get("rights_lead_only")])
        ),
    }
    panels = []
    for key, label, note in tabs:
        if key in FILTER_TABS:
            continue
        heading = ""
        if with_bar:
            heading = f'<h2 class="tab-title">{escape(label)} <span class="count">({counts[key]})</span></h2>'
            if note:
                heading += f'<p class="tab-note">{escape(note)}</p>'
        panels.append(f'<section class="tab-panel" id="tab-{key}" data-tab="{key}">{heading}{contents[key]}</section>')
    bar = ""
    if with_bar:
        links = "".join(
            f'<a class="tab{" tab--filter" if key in FILTER_TABS else ""}" href="#tab-{key}" data-tab="{key}">'
            f'{escape(label)} <span class="n">{counts[key]}</span></a>'
            for key, label, _note in tabs
        )
        bar = f'<nav class="tabbar" aria-label="Разделы обзора"><div class="tabs">{links}</div></nav>'
    return bar + "".join(panels)


def _alert_html(text_html: str) -> str:
    """One caution line with a hanging warning sign, so each line reads apart."""
    return f'<div class="alert"><span class="ico" aria-hidden="true">⚠</span><span>{text_html}</span></div>'


def _source_problem_html(summary: dict[str, Any]) -> str:
    """Render the named stale, failed, and skipped sources, or "" (Issue #825).

    Reads only the plain-text ``source_problem_lines`` the Markdown review
    also prints, so both renderings name the same sources with the same
    reasons, and a hand-built summary without them renders as before.
    """
    raw_lines = summary.get("source_problem_lines") or []
    if not isinstance(raw_lines, list):
        return ""
    lines = [str(line) for line in raw_lines if isinstance(line, str) and line.strip()]
    if not lines:
        return ""
    return '<div class="sources">' + "".join(_alert_html(escape(line)) for line in lines) + "</div>"


def _incomplete_coverage_line(summary: dict[str, Any]) -> str:
    """Render one banner line per partially covered source, or "".

    Reads only the ``incomplete_source_coverages`` summary entries
    (``source_name`` with integer ``observed_rows``/``expected_rows``), so a
    hand-built summary without coverage data renders exactly as before.
    """
    raw_coverages = summary.get("incomplete_source_coverages") or []
    if not isinstance(raw_coverages, list):
        return ""
    parts = []
    for entry in raw_coverages:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("source_name", "")).strip()
        observed = entry.get("observed_rows")
        expected = entry.get("expected_rows")
        if (
            not name
            or isinstance(observed, bool)
            or not isinstance(observed, int)
            or isinstance(expected, bool)
            or not isinstance(expected, int)
        ):
            continue
        left_out = left_out_titles_text(entry)
        parts.append(f"{name} — {observed} из {expected} строк рейтинга" + (f", {left_out}" if left_out else ""))
    if not parts:
        return ""
    text_html = (
        "Неполный охват: " + "; ".join(escape(part) for part in parts) + ". В обзор вошли только доступные строки."
    )
    return f'<div class="incomplete">{_alert_html(text_html)}</div>'


def _archive_groups(rows: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """Each archive block of *rows* as (batch, label, note), oldest batch first."""
    pending: dict[str, list[dict[str, Any]]] = {}
    decided: dict[str, int] = {}
    # The first day the batch was applied, over its decided books too.
    first_applied: dict[str, str] = {}
    for row in rows:
        batch = str(row.get("archive_batch", "")).strip()
        if not batch:
            continue
        applied = str(row.get("archive_applied_date", ""))
        first_applied[batch] = min(first_applied.get(batch, applied), applied)
        if row.get("archive_pending"):
            pending.setdefault(batch, []).append(row)
        else:
            decided[batch] = decided.get(batch, 0) + 1
    groups = []
    for batch, batch_rows in pending.items():
        applied = first_applied[batch]
        total = len(batch_rows) + decided.get(batch, 0)
        note = (
            f"Книги добавлены из архива одним пакетом {applied}. "
            f"Без решения — {len(batch_rows)} из {total} видимых книг архива. "
            f"{ARCHIVE_DECIDED_SENTENCE}"
        )
        groups.append((applied, batch, str(batch_rows[0].get("archive_batch_label", "")), note))
    return [(batch, label, note) for _applied, batch, label, note in sorted(groups)]


def _section_html(
    section: DateSection,
    archive_groups: dict[str, tuple[str, str]],
    run_date: str,
    *,
    filtered: bool = False,
    with_fit: bool = False,
) -> str:
    """One date section, or one archive block, with its heading, count and table.

    In a *filtered* timeline the section also carries the count of its cards
    without a decision, so the undecided tab shows each section's own count
    and hides a section with no card to show.
    """
    if section.archive_batch:
        label, note = archive_groups[section.archive_batch]
        attrs = f'data-group="{ARCHIVE_GROUP_KEY}" data-batch="{escape(section.archive_batch, quote=True)}"'
        heading = escape(label)
    else:
        note = ""
        attrs = f'data-group="{DATE_GROUP_KEY}" data-date="{escape(section.date, quote=True)}"'
        heading = escape(long_russian_date(section.date)) if section.date else UNKNOWN_DATE_HEADING
        if run_date and section.date == run_date:
            heading += f' <span class="latest">{LATEST_RUN_BADGE}</span>'
    counts = f'<span class="count">({len(section.rows)})</span>'
    if filtered:
        undecided = sum(1 for row in section.rows if not _has_decision(row))
        attrs += f' data-n-undecided="{undecided}"'
        counts += f'<span class="count count--undecided">({undecided})</span>'
    return _group_section_html(attrs, f"{heading} {counts}", note, section.rows, run_date, with_fit)


def _updated_section_html(section: DateSection, run_date: str, with_fit: bool = False) -> str:
    """One section of «Обновлённые»: the books changed on one date, or with no change date."""
    attrs = f'data-group="changed" data-date="{escape(section.date, quote=True)}"'
    heading = escape(long_russian_date(section.date)) if section.date else UNKNOWN_CHANGE_DATE_HEADING
    return _group_section_html(
        attrs, f'{heading} <span class="count">({len(section.rows)})</span>', "", section.rows, run_date, with_fit
    )


def _group_section_html(
    attrs: str, heading_html: str, note: str, rows: list[dict[str, Any]], run_date: str, with_fit: bool = False
) -> str:
    columns = [
        column
        for entry in COLUMNS
        for column in ((entry, FIT_COLUMN) if with_fit and entry[0] == _FIT_COLUMN_AFTER else (entry,))
    ]
    header = (
        '<div class="thead">'
        + "".join(
            f'<span class="sortable" data-key="{key}" data-type="{stype}">'
            f'{escape(label_)}<span class="ind"></span></span>'
            if stype
            else f'<span class="plain" data-key="{key}">{escape(label_)}</span>'
            for key, label_, stype in columns
        )
        + "</div>"
    )
    body = "".join(_candidate_html(row, run_date, with_fit) for row in rows)
    note_html = f'<p class="group-note">{escape(note)}</p>' if note else ""
    return (
        f'<section class="group" {attrs}>'
        f"<h2>{heading_html}</h2>"
        f"{note_html}"
        f'<div class="table">{header}{body}</div>'
        "</section>"
    )


def _review_totals_text(
    rows: list[dict[str, Any]], cumulative_counts: dict[str, Any], run_date: str, total_count: int
) -> str:
    """The review's totals: the cards of the latest run's date, the updated marks, the archive blocks, the rest."""
    return (
        f"Всего: добавлено в последний запуск — {added_on_run_date_count(rows, run_date)}, "
        f"с отметкой «обновлено» — {marked_updated_count(rows, run_date)}, "
        + "".join(
            f"{entry['label']} без решения — {int(entry['pending_count'])}, "
            for entry in cumulative_counts.get("archive_batches") or []
            if entry.get("pending_count")
        )
        + f"видимых — {int(cumulative_counts.get('visible_total_count', total_count))}, "
        f"скрытых редактором — {int(cumulative_counts.get('editor_hidden_count', 0))}. "
    )


def build_html_review(rows: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    """Render the full self-contained HTML review document.

    ``rows`` is a list of candidate dicts already in digest order, each with the
    keys produced by ``review_rendering._build_review_rows``. ``summary`` carries
    ``delta_counts``, ``hidden_by_rule_count``, ``editor_rows_count`` and
    ``total_count``.
    """
    delta_counts = summary.get("delta_counts", {}) or {}
    hidden = int(summary.get("hidden_by_rule_count", 0) or 0)
    total_count = int(summary.get("total_count", len(rows)) or 0)

    cumulative_counts = summary.get("cumulative_counts") or {}
    run_date = str(summary.get("run_date", ""))
    week_line = (
        f"новых — {int(delta_counts.get('new', 0))}, "
        f"улучшенных — {int(delta_counts.get('upgraded', 0))}, "
        f"изменённых — {int(delta_counts.get('changed', 0))}, "
        f"без изменений — {int(delta_counts.get('unchanged', 0))}, "
        f"скрыто правилами — {hidden}"
    )

    cumulative_totals = ""
    description_totals = ""
    stage_totals = ""
    if cumulative_counts:
        cumulative_totals = _review_totals_text(rows, cumulative_counts, run_date, total_count)
        rule_hidden = int(cumulative_counts.get("rule_hidden_count", 0) or 0)
        retired_hidden = int(cumulative_counts.get("retired_source_hidden_count", 0) or 0)
        edition_hidden = int(cumulative_counts.get("decided_edition_hidden_count", 0) or 0)
        if rule_hidden or retired_hidden or edition_hidden:
            # Cards an active Hard Rule, a retired source or a decision on
            # another edition keeps out of the whole review (#1025).
            cumulative_totals += (
                f"Скрыто из всего обзора: правилами редактора — {rule_hidden}, "
                f"из отключённых источников — {retired_hidden}"
                + (f", {DECIDED_EDITION_HIDDEN_LABEL} — {edition_hidden}" if edition_hidden else "")
                + ". "
            )
        description_coverage = cumulative_counts.get("description_coverage") or {}
        if description_coverage:
            # The clause says "of the verified ones", so it reads the
            # sourced-only depth count once coverage carries it (#788).
            try:
                below_depth = int(
                    description_coverage.get(
                        "sourced_below_depth_count", description_coverage.get("below_depth_count", 0)
                    )
                    or 0
                )
            except (TypeError, ValueError):
                below_depth = 0
            depth_clause = (
                f" Из проверенных краткими остаются — {below_depth} "
                "(порог глубины: от 200 знаков и не менее двух предложений)."
                if below_depth > 0
                else ""
            )
            # Unsourced stubs are not counted as verified (issue #788);
            # coverage written before the split carries no key and reads
            # as before.
            try:
                unsourced = int(description_coverage.get("unsourced_count", 0) or 0)
            except (TypeError, ValueError):
                unsourced = 0
            unsourced_clause = f"не подтверждены источниками — {unsourced}, " if unsourced > 0 else ""
            # Books that are not yet published need nothing from anyone, so
            # they are reported apart from the rows that do (issue #791).
            try:
                unpublished = int(description_coverage.get("not_yet_published_count", 0) or 0)
            except (TypeError, ValueError):
                unpublished = 0
            unpublished_clause = f", ещё не опубликованы — {unpublished}" if unpublished > 0 else ""
            # A logline description is no gap and no verified one (issue #950).
            logline = pm_logline_count(description_coverage)
            logline_clause = f", по логлайну сделки PM — {logline}" if logline > 0 else ""
            description_totals = (
                "Русские описания: проверено — "
                f"{verified_description_count(description_coverage)} "
                f"из {coverage_denominator(description_coverage)}, "
                f"{unsourced_clause}требуют дополнения — "
                f"{max(0, int(description_coverage.get('fallback_count', 0)) - unpublished)}"
                f"{unpublished_clause}{logline_clause}.{depth_clause} "
                "Пробелы в описаниях — предупреждение о дополнении данных, а не сбой сбора."
            )
    stage_counts = summary.get("description_stage_counts") or cumulative_counts.get("description_stage_counts") or {}
    if any(
        int(stage_counts.get(key, 0) or 0)
        for key in ("grounded", "single_source", "attempted_no_description", "not_attempted")
    ):
        stage_totals = (
            f"Проверка источников: подтверждено несколькими источниками - {int(stage_counts.get('grounded', 0) or 0)}, "
            f"по одному источнику - {int(stage_counts.get('single_source', 0) or 0)}, "
            f"без описания - {int(stage_counts.get('attempted_no_description', 0) or 0)}, "
            f"поиск не запускался - {int(stage_counts.get('not_attempted', 0) or 0)}."
        )
    risk_coverage_totals = visible_risk_coverage_summary_text(summary.get("visible_risk_screen_coverage_breakdown"))
    run_date_line = (
        f"<div class='run-date' data-run-date='{escape(run_date, quote=True)}'>Дата обзора: {escape(run_date)}</div>"
        if run_date
        else ""
    )

    reused_source_count = int(summary.get("reused_source_count", 0) or 0)
    collecting_source_count = int(summary.get("collecting_source_count", 0) or 0)
    raw_source_statuses = summary.get("source_statuses")
    if isinstance(raw_source_statuses, list | tuple):
        source_summary = summarize_source_runs(raw_source_statuses)
        effective_reused_source_count = source_summary.stale_source_count
        effective_collecting_source_count = source_summary.collecting_source_count
    else:
        effective_reused_source_count = reused_source_count
        effective_collecting_source_count = collecting_source_count
    reused_line = ""
    if effective_reused_source_count:
        reused_line = (
            '<div class="reuse">'
            + _alert_html(
                f"{effective_reused_source_count} "
                f"из {effective_collecting_source_count} источников — "
                "данные предыдущего запуска, а не свежий сбор. "
                "Числа выше относятся не только к этой неделе."
            )
            + "</div>"
        )
    incomplete_line = _incomplete_coverage_line(summary)
    source_problem_html = _source_problem_html(summary)
    description_caveats = "".join(
        f'<div class="reuse">{escape(str(caveat))}</div>' for caveat in summary.get("description_caveats", [])
    )
    # The reuse line is short and says how far the numbers can be trusted, so
    # it stays in view. The lines that name every source can run to a screen
    # of text; past a few lines they wait behind one click, with their count.
    source_alerts = f"{source_problem_html}{description_caveats}{incomplete_line}"
    if len(source_alerts) > _ALERTS_FOLD_LENGTH:
        alert_count = source_alerts.count('class="alert"') + len(summary.get("description_caveats", []))
        label = "Ещё предупреждений об источниках" if reused_line else "Предупреждений об источниках"
        source_alerts = (
            f'<details class="more-alerts"><summary>{label}: {alert_count}</summary>{source_alerts}</details>'
        )
    alerts = f"{reused_line}{source_alerts}"
    alerts_html = f'<div class="alerts">{alerts}</div>' if alerts else ""
    totals_lines = [
        f"{escape(cumulative_totals)}Всего книг: {total_count}. Чтобы принять решение по книге, напишите его в чате.",
        escape(description_totals),
        escape(stage_totals),
        "Проверка риска: " + escape(risk_coverage_totals) if risk_coverage_totals else "",
    ]
    with_fit = _has_fit_scores(rows)
    flat_undecided = _flat_undecided(rows, summary)
    fit_style = (f"\n{_FIT_STYLE}" if with_fit else "") + (f"\n{_FLAT_STYLE}" if flat_undecided else "")
    flat_script = f"<script>{_FLAT_SCRIPT}</script>\n" if flat_undecided else ""
    order_note = (
        f'<div class="order-note">Порядок книг: {escape(FIT_ORDER_NOTE)}</div>'
        if with_fit and summary.get("ordered_by_fit")
        else ""
    )
    totals_html = "".join(f'<div class="totals-line">{line}</div>' for line in totals_lines if line)
    totals_block = f'<details class="totals"><summary>Сводка обзора</summary><div class="totals-lines">{totals_html}</div></details>'
    body = (
        '<div class="wrap">'
        "<h1>Еженедельный обзор AutoScout</h1>"
        '<div class="banner">'
        f"{run_date_line}"
        f'<div class="week">За эту неделю: {escape(week_line)}.</div>'
        f"{order_note}"
        f"{alerts_html}"
        f"{totals_block}"
        "</div>"
        + _tab_panels_html(rows, run_date, flat_undecided=flat_undecided)
        + "<footer>Обзор предназначен только для чтения. "
        "Решения по книгам передавайте в чате.</footer>"
        "</div>"
    )

    return (
        "<!doctype html>\n"
        '<html lang="ru">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Еженедельный обзор AutoScout</title>\n"
        f'<link rel="icon" type="image/svg+xml" href="{_FAVICON_URL}">\n'
        f"<style>{_STYLE}{fit_style}</style>\n</head>\n<body>\n{body}\n"
        f"<script>{_SCRIPT}</script>\n{flat_script}</body>\n</html>\n"
    )
