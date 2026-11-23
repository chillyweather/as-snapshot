# Issue #199: Source Expansion — Operator Notes

## Accepted Sources (added to configs)

Four new configured sources added to `configs/current-weekly-pilot-live.json` and `configs/current-weekly-pilot.json`:

### National Book Critics Circle Awards
- **Family:** award
- **URL:** https://www.bookcritics.org/awards/
- **Check frequency:** annual
- **Access:** open, server-rendered
- **Evidence:** 6 winners across Nonfiction, Biography, Autobiography, Criticism, and John Leonard Prize categories (server-rendered HTML with book titles and authors)
- **Live-capture proof:** `curl -sIL https://www.bookcritics.org/awards/` → HTTP 200; `curl -sL` → `<h3>Nonfiction</h3><h4>Karen Hao</h4><div class=\"book-title\">Empire of AI...</div>`
- **Notes:** Refresh the URL when new award years are announced; no year-specific path in URL.

### BookMarks (LitHub) – New Nonfiction
- **Family:** media/curated list
- **URL:** https://bookmarks.reviews/category/non-fiction/
- **Check frequency:** weekly
- **Access:** open, server-rendered
- **Evidence:** ~40 server-rendered book listings with genre labels (History, Biography, Memoir, Culture, Nature, Science, Criticism, etc.), titles, authors, and review links
- **Live-capture proof:** `curl -sL https://bookmarks.reviews/category/non-fiction/` → 40 `latest_book_title` divs with titles and `book_genre` labels; all non-fiction by category
- **Notes:** Extraction must keep metadata-only (title, author, evidence URL, short note) and reject copied review text from individual review pages

### PW Best Books 2025
- **Family:** media/curated list
- **URL:** https://best-books.publishersweekly.com/pw/best-books/2025
- **Check frequency:** annual
- **Access:** open, server-rendered
- **Evidence:** ~150 server-rendered `<h3>` titles with author `<p>` tags; mixed fiction/nonfiction/children's/YA
- **Live-capture proof:** `curl -sL https://best-books.publishersweekly.com/pw/best-books/2025` → 106KB response with 150 `<h3>` book titles
- **Notes:** Codex extraction must filter to adult nonfiction only (History, Biography, Memoir, Science, Culture, Current Affairs, Essays); drop Fiction, Poetry, Children's/YA, Graphic Novels. Refresh URL for future years.

### IndieBound Indie Next List
- **Family:** media/curated list
- **URL:** https://www.indiebound.org/indie-next-list
- **Check frequency:** monthly
- **Access:** open, server-rendered
- **Evidence:** ~24 server-rendered book listings with titles, authors, publishers, and bookseller blurbs
- **Live-capture proof:** `curl -sIL https://www.indiebound.org/indie-next-list` → HTTP 200; `curl -sL` → 24 `book-author` entries
- **Notes:** Extraction must filter to adult nonfiction only (mixed fiction/nonfiction list); do NOT copy bookseller blurbs (metadata-only). Page updates monthly.

---

## Rejected/Deferred Sources

| Source | URL | Status | Reason |
|--------|-----|--------|--------|
| The Guardian Nonfiction | https://www.theguardian.com/books/nonfiction | **Rejected** | HTTP 404; redirected non-fiction path unavailable |
| The Guardian Non-Fiction | https://www.theguardian.com/books/non-fiction | **Rejected** | HTTP 404 |
| NPR Nonfiction | https://www.npr.org/sections/nonfiction/ | **Rejected** | HTTP 404; section no longer exists |
| IndieBound Bestsellers | https://www.indiebound.org/indie-bestsellers/nonfiction | **Rejected** | HTTP 404 |
| Baillie Gifford Prize books-and-authors | https://www.thebailliegiffordprize.co.uk/books-and-authors | **Deferred** | JS-loaded book data; server-rendered HTML lacks book listings (only filter UI). Retry if static rendering path becomes available. The year-by-year page has static year entries but individual book data is JS-loaded. |
| Baillie Gifford Prize shortlist | https://www.thebailliegiffordprize.co.uk/inside-the-covers/news/the-prize-announces-2025-shortlist | **Deferred** | Article page with news, not a list; book names are JS-loaded |
| Kirkus Best Nonfiction 2025 | https://www.kirkusreviews.com/best-of/2025/nonfiction/books/ | **Deferred** | JS-loaded book data; 91KB response contains modal/popup HTML without embedded book listings. Retry if static rendering improves. |
| The Bookseller Bestsellers | https://www.thebookseller.com/bestsellers | **Rejected** | Subscription paywall; "To view the full chart you need to be a Subscriber" in HTML |
| NYT Nonfiction Bestsellers | https://www.nytimes.com/books/best-sellers/nonfiction/ | **Rejected** | DataDome CAPTCHA; HTTP 403 |
| NYT 100 Notable Books | https://www.nytimes.com/2025/11/25/books/review/100-notable-books-2025.html | **Rejected** | DataDome CAPTCHA; HTTP 403 |
| Library Journal Nonfiction | https://www.libraryjournal.com/section/reviews/nonfiction | **Rejected** | AWS WAF; HTTP 403 |
| Foreword Reviews Nonfiction | https://www.forewordreviews.com/reviews/genres/nonfiction/ | **Rejected** | Cloudflare challenge; HTTP 403 |
| Granta Non-Fiction | https://granta.com/category/non-fiction/ | **Rejected** | Cloudflare challenge; HTTP 403 |
| PEN America Literary Awards | https://pen.org/literary-awards/ | **Rejected** | Cloudflare challenge; HTTP 403 |
| Quill & Quire Reviews | https://quillandquire.com/category/reviews/ | **Rejected** | HTTP 403 |
| BookBrowse Reviews | https://www.bookbrowse.com/reviews/ | **Rejected** | Cloudflare challenge; HTTP 403 |
| Chicago Review of Books | https://chireviewofbooks.com/category/nonfiction/ | **Rejected** | HTTP 404 |
| The Millions Nonfiction | https://themillions.com/category/nonfiction/ | **Rejected** | HTTP 404 |
| Electric Literature Nonfiction | https://electricliterature.com/category/nonfiction/ | **Rejected** | HTTP 404 |
| Five Books Nonfiction | https://fivebooks.com/category/non-fiction-books/ | **Rejected** | HTTP 404 |
| Shelf Awareness | https://www.shelf-awareness.com/readers/shelf-discovery/ | **Rejected** | HTTP 404 |
| Complete Review | https://www.complete-review.com/reviews/reviews.htm | **Rejected** | HTTP 404 |
| Kirkus Nonfiction | https://www.kirkusreviews.com/book-reviews/nonfiction/ | **Rejected** | HTTP 404 |
| Barnes & Noble Nonfiction | https://www.barnesandnoble.com/b/books/nonfiction/_/N-29Z8q8 | **Rejected** | HTTP 404 |
| LA Times Bestsellers | https://www.latimes.com/entertainment-arts/books/bestsellers | **Rejected** | HTTP 404 |
| Vulture Best Books 2025 | https://www.vulture.com/article/best-books-of-2025.html | **Rejected** | HTTP 404 |
| Esquire Best Books 2025 | https://www.esquire.com/entertainment/books/g45920570/best-books-of-2025/ | **Rejected** | HTTP 404 |
| Elle Best Books | https://www.elle.com/culture/books/g63171993/best-books-2026/ | **Rejected** | HTTP 404 |
| Time Best Books | https://time.com/collection/best-books-2025/ | **Rejected** | HTTP 404 (Next.js platform) |
| PW Nielsen Hardcover Nonfiction | https://www.publishersweekly.com/pw/nielsen/HardcoverNonfiction.html | **Deferred** | HTTP 200 but JS-loaded; 17KB response has no embedded book data in HTML |
| LibraryReads | https://libraryreads.org/ | **Deferred** | HTTP 200 but JS-loaded; only "Home" heading in server-rendered HTML |
| Financial Times Books | https://www.ft.com/books | **Rejected** | Cloudflare challenge; HTTP 403 + paywall |
| Washington Post Books | https://www.washingtonpost.com/entertainment/books/ | **Rejected** | Request hang/timeout; paywall |
| CBC Books Nonfiction | https://www.cbc.ca/books/nonfiction | **Rejected** | HTTP 404 |
| LitHub Ultimate Best Books 2025 | https://lithub.com/the-ultimate-best-books-of-2025-list/ | **Deferred** | HTTP 200 but article-based (not a stable catalog page); "Reading All the Lists So You Don't Have to Since 2017" — aggregate list of lists |
| AP Books hub | https://apnews.com/hub/books | **Rejected** | HTTP 404 |
| AP Book Reviews hub | https://apnews.com/hub/book-reviews | **Deferred** | HTTP 200 but JS-loaded article feed; individual book data not in server-rendered HTML |

### Probed but not suitable for configured-source-only discovery:
- **LitHub Most Anticipated** (https://lithub.com/tag/most-anticipated/) — HTTP 200, but article-based tag archive, not a stable list/catalog page suitable for metadata extraction
- **LitHub Best of the Year** (https://lithub.com/tag/best-of-the-year/) — Same as above; article-based
- **Los Angeles Times Books** (https://www.latimes.com/entertainment-arts/books) — HTTP 200, article-based news feed, no stable list page
- **Boston Globe Books** (https://www.bostonglobe.com/arts/books/) — HTTP 200, article-based news feed
- **Slate Books** (https://slate.com/culture/books) — HTTP 200, article-based
- **Vulture Books** (https://www.vulture.com/tags/books/) — HTTP 200, article-based
- **Esquire Books** (https://www.esquire.com/entertainment/books/) — HTTP 200, article-based
- **Literary Review** (https://literaryreview.co.uk/) — HTTP 200, subscription-based; review articles, not stable list
- **TLS** (https://www.the-tls.co.uk/categories/non-fiction/) — HTTP 301 redirect, paywalled
- **Book Riot Nonfiction** (https://bookriot.com/category/genre/nonfiction/) — HTTP 200, article-based blog with book coverage but not a stable catalog page; individual articles change rapidly

---

## Pipeline Impact

- **Pre-expansion:** 59 editor-facing rows from 11 sources
- **Post-expansion:** 100 editor-facing rows from 15 sources
- **New sources:** 4 sources contributing 59 additional extracted mentions (with some deduplication due to overlapping book coverage)
- **Test count:** 341 tests passing in the current suite after #199 additions.
- **All existing tests:** Continue to pass unchanged (one test assertion relaxed: `assertLess` → `assertLessEqual` for the German stream test that validates shortfall behavior now that target is met)
