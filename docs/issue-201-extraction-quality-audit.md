# Issue #201: High-volume Codex Extraction Quality Audit

## Audited sources

Reviewed deterministic Codex replay artifacts and source guidance for high-volume sources:

- USA TODAY Bestselling Books Nonfiction
- PEOPLE Books
- BookPage Nonfiction
- Publishers Marketplace Rights Offerings
- BookMarks (LitHub) – New Nonfiction
- IndieBound Indie Next List
- Audiobooks.com Nonfiction Audiobook Bestsellers (#308)

## Tightened extraction guidance

The live and replay configs now instruct Codex to keep metadata-only evidence and drop common leakage patterns:

- fiction, children/YA, textbooks/workbooks, and topic-only rows
- duplicate list/review/deal mentions
- celebrity-news-only or gift-guide items without book-level nonfiction evidence
- retail/list rows without title + author/creator + evidence URL
- copied reviews, bookseller blurbs, deal text, article bodies, descriptions, excerpts, or samples

## Deterministic verification

`tests/test_source_collection.py` includes an Issue #201 regression that checks audited fixtures for:

- no forbidden copied-text fields
- no obvious fiction/children/YA categories
- bounded metadata-only evidence notes
- no duplicate title/author mentions within each audited source
- source-run metadata still reporting extracted, valid, and dropped mention counts honestly

## Audiobooks.com Nonfiction Audiobook Bestsellers (#308)

- Retargeted from Libro.fm to https://www.audiobooks.com/browse/non-fiction.
- Source family remains `audiobook ranking`; this is a secondary retailer-popularity signal, not measured sales data.
- Page-shape validation (fail-closed) checks for `Non-Fiction` category heading and `Sort by: Bestselling` sort label in captured HTML at both prepare and replay.
- Extraction contract:
  - Only adult nonfiction underlying books/works. Exclude fiction leakage, Kids, Teen/YA, summaries, courses, non-book audio, and Coming Soon items.
  - Never invent numeric rank or sales units; list order is an implicit retailer bestseller ordering.
  - Evidence notes describe bestseller ordering without claiming measured sales.
  - Book-level evidence URLs and category-level source URLs are provenance-verifiable.
- Deterministic fixture audit in `tests/test_issue_308_audiobooks_source.py`.

## Remaining concerns

- Mixed-list sources such as IndieBound and PW Best Books still rely on Codex following source-specific filtering notes; keep fixture audits in review when these pages refresh.
- USA TODAY category/subject-code filtering should be rechecked whenever its page markup changes.
- PEOPLE and BookPage are safe only when evidence notes stay metadata-only and do not quote review/article prose.
