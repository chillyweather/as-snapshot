# Issue #540 detail-paragraph subject fixture

Capture date: 2026-08-20.

Source URL:

- https://www.barnesandnoble.com/w/this-moment-matters-ruth-chou-simons/1148674519 (`barnesandnoble-this-moment-matters.html`)

This is a full HTML capture from `BoundedHTTPFetcher`.

Trimmed in #1022: pages up to the 1,000,000-byte fetch bound lost their non-JSON-LD `<script>`, `<style>`, `<svg>`, comments and the subtrees the page-evidence parser ignores (`nav`, `header`, `footer`, `aside`, `form`, `button`, `noscript`); `_PageEvidenceParser` yields identical state on the trimmed and the full file. Pages larger than the bound (the Amazon pages and `barnesandnoble-exhumed.html`) keep their first 1,000,000 bytes untouched plus one byte, because the fetcher reads only that prefix and the page must stay over the bound to be reported as truncated; the unread tail was dropped.
