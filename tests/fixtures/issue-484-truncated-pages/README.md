# Issue #484 truncated-page fixtures

These HTML files are offline regression fixtures for issue #484.

Capture date: 2026-08-16.

| Fixture | Source URL |
| --- | --- |
| `oversized-og-description.html` | https://fixtures.example.test/books/truncated-og-description |
| `truncated-middle-tag.html` | https://fixtures.example.test/books/truncated-middle-tag |
| `truncated-multibyte.html` | https://fixtures.example.test/books/truncated-multibyte |
| `truncated-jsonld.html` | https://fixtures.example.test/books/truncated-jsonld |
| `oversized-no-evidence.html` | https://fixtures.example.test/books/truncated-no-evidence |
| `below-limit.html` | https://fixtures.example.test/books/below-limit |

Trimmed in #1022: each oversized file keeps its first 4 KB and its closing tags. The test bound is 512 bytes (`LIMIT`) and the markers the tests look for sit in the first 250 bytes, so the removed middle was repeated `padding` text that no test reads.
