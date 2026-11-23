# Issue #502 agreeing-subset fixtures

Capture date: 2026-08-17.

Source URLs:

- https://www.amazon.com/Exhumed-Unearthing-History-American-Vampire/dp/B0G4P2VQ4P (`amazon-exhumed.html`)
- https://us.amazon.com/Exhumed-Unearthing-History-American-Vampire-ebook/dp/B0GFP7VDW9 (`us-amazon-exhumed-ebook.html`)
- https://bookshop.org/p/books/exhumed-unearthing-the-history-of-the-american-vampire-aaron-mahnke/c7e72bebb737534a (`bookshop-exhumed.html`)
- https://www.hachettebookgroup.com/titles/aaron-mahnke/exhumed/9798894141817/ (`hachette-exhumed.html`)
- https://www.barnesandnoble.com/w/exhumed-aaron-mahnke/1148854448 (`barnesandnoble-exhumed.html`)

The five files are full HTML captures saved from bounded fetches, not reduced files.

Trimmed in #1022: pages up to the 1,000,000-byte fetch bound lost their non-JSON-LD `<script>`, `<style>`, `<svg>`, comments and the subtrees the page-evidence parser ignores (`nav`, `header`, `footer`, `aside`, `form`, `button`, `noscript`); `_PageEvidenceParser` yields identical state on the trimmed and the full file. Pages larger than the bound (the Amazon pages and `barnesandnoble-exhumed.html`) keep their first 1,000,000 bytes untouched plus one byte, because the fetcher reads only that prefix and the page must stay over the bound to be reported as truncated; the unread tail was dropped.
