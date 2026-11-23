# Research: Big-Retailer Bestseller Lists as External Signals

**Decision:** Audible is the strongest public first-party retailer bestseller list
discovered — it is openly accessible via GET, server-rendered, paginated, and explicitly
adult-nonfiction. However, no candidate clears every runbook gate for immediate
onboarding: Audible's HEAD request returns 405 (unsupported method), which fails the
runbook's prescribed `curl -sIL` feasibility check. Barnes & Noble's nonfiction bestseller
carousel is JS-loaded with zero visible cards in the bestseller section. Amazon
subcategory pages pass the data gate but require a browser User-Agent. All other
retailers are blocked, JS-only, wrong-shape, or lack a working list. **Research only;
no onboarding recommended under the runbook as written unless the prescribed HEAD
evidence is amended or explicitly excepted.**

**Check date:** 2026-07-18

---

## Audible (Amazon) — Bestseller Charts Overview

Audible exposes individual bestseller chart pages per category using the same
`/charts/best/<slug>-audiobooks/<node-id>` pattern. All pages tested have identical
structural behaviour: HEAD=405, GET=200, server-rendered with visible titles, authors,
`/pd/` links, paginated (20/page × 5 pages = 100 results), "updated daily!" cadence,
no paywall/CAPTCHA.

### HEAD-based feasibility check note

The runbook's feasibility checklist (section 1, first checkbox) prescribes `curl -sIL
<url>` as HTTP-status evidence. Audible returns **405 Method Not Allowed** to HEAD
requests on this endpoint on **every** category page tested. The GET response is 200,
open, and reliable in repeated checks, but the strict HEAD check fails. This is a
procedural mismatch, not an access block: the page is publicly accessible and
server-rendered. Resolution options: (a) the runbook's HEAD requirement is amended or
explicitly excepted for endpoints that do not implement HEAD but return 200 on GET, or
(b) the source remains deferred under the runbook as written.

### robots.txt nuance

The robots.txt contains `Disallow: /charts/*?` — a rule with a wildcard query-string
pattern that disallows URLs containing a `?` parameter. The base chart URL
(`/charts/best/...`) does not have a query string and is not affected by this rule.
Paginated URLs (`?page=N`) match the `Disallow: /charts/*?` rule. Practical impact:
the rule disallows query-string pagination for crawlers that respect robots.txt, so
collecting results beyond page 1 via automated pagination is incompatible with
that stated robots policy. Previous
version of this document oversimplified the robots.txt finding.

---

### Category comparison: five adult-nonfiction Audible chart categories

The initial recommendation focused on **Biographies & Memoirs** (node 18571951011).
After live probing, five other adult-nonfiction categories are structurally identical
(HEAD=405, GET=200, 20/page, 100 total, daily refresh) and some offer better topical
fit for AutoScout's editorial mission. Below is a per-category breakdown.

#### 1. Science & Engineering (node 18580540011)

**URL:** `https://www.audible.com/charts/best/science-engineering-audiobooks/18580540011`

| Criterion | Observation |
|-----------|-------------|
| **HTTP status (HEAD)** | 405 |
| **HTTP status (GET)** | 200 — ~1.07 MB |
| **Page heading** | `Bestselling Science & Engineering Audiobooks` |
| **Results** | 1 - 20 of 100 |
| **Visible titles** | _Sapiens_ (Harari), _The Body_ (Bryson), _Salt_ (Kurlansky), _Braiding Sweetgrass_ (Kimmerer), _Homo Deus_ (Harari), _An Immense World_ (Yong), _Breath_ (Nestor), _Outlive_ (Attia), _The Light Eaters_ (Schlanger), _Everything Is Tuberculosis_ (John Green), _Co-Intelligence_ (Mollick), _Algorithms to Live By_ (Christian). |
| **Product links** | Yes — all 20 `/pd/` links. |
| **Cadence** | "updated daily!" |
| **Topical contamination** | **Low.** One title (_Everything Is Tuberculosis_ by John Green) is science-adjacent narrative non-fiction. _Co-Intelligence_ and _Algorithms to Live By_ are crossovers with technology but still science-adjacent. No narrow manuals or how-to courses. Minimal memoirs and self-help leakage compared to other categories. |
| **Adult-nonfiction fit** | **Excellent.** Broad-audience popular science. The "Engineering" label in the category name appears under-represented in the top 20; the list is more popular science than engineering. This is an editorial-fit note, not a technical blocker. |

**Editorial-fit verdict:** Strongest editorial fit among the five for broad
general-interest nonfiction. Popular science titles consistently cover topics of
broad thematic interest (history, biology, environment, technology) with minimal
memoir or self-help content.

#### 2. Business & Careers (node 18572029011)

**URL:** `https://www.audible.com/charts/best/business-careers-audiobooks/18572029011`

| Criterion | Observation |
|-----------|-------------|
| **HTTP status (HEAD)** | **405 Method Not Allowed** |
| **HTTP status (GET, vanilla curl)** | **200** — ~1.08 MB, no redirect, no UA tricks. |
| **Page heading** | `<title>Bestselling Business & Careers Audiobooks</title>`, `<h1>Bestselling Business & Careers Audiobooks</h1>` |
| **Results text** | `1 - 20 of 100 results` — 5 pages, paginated via `?page=N`. |
| **Visible title/author in raw HTML** | **Yes** — full title and primary author in raw HTML, e.g. _Atomic Habits_ (James Clear), _48 Laws of Power_ (Robert Greene), _Never Split the Difference_ (Chris Voss), _Extreme Ownership_ (Jocko Willink), _Hidden Potential_ (Adam Grant), _Thinking, Fast and Slow_ (Daniel Kahneman). |
| **Product links** | **Yes** — `/pd/` links for all 20 ranked items. |
| **Cadence** | Meta description: "updated daily!" |
| **Biographies/Memoirs crossover** | **Present.** The top 20 includes memoir/inside-company narratives: _Kitchen Confidential_ (Anthony Bourdain), _Careless People_ (Sarah Wynn-Williams). These are personal/inside-company narratives. |
| **Topical contamination (courses/summaries/narrow manuals)** | **Moderate.** The list includes substantial self-help and management content (_Atomic Habits_, _48 Laws of Power_, _Never Split the Difference_, _Extreme Ownership_, _The 7 Habits of Highly Effective People_). Several titles are prescriptive management/self-help rather than general business narrative. |
| **Access blockers** | None — open GET. |
| **Adult-nonfiction fit** | **Excellent.** All 20 titles are adult nonfiction; no fiction, YA, children's. |

**Editorial-fit verdict:** Moderate-to-strong fit for AutoScout. Business books are
in-scope adult nonfiction, but the top 20 includes memoir narratives and substantial
self-help/management content that overlap with AutoScout's likely exclusion zones.
Usable as a secondary category with filtering for memoir and prescriptive
self-help titles.

#### 3. Computers & Technology (node 18573211011)

**URL:** `https://www.audible.com/charts/best/computers-technology-audiobooks/18573211011`

| Criterion | Observation |
|-----------|-------------|
| **HTTP status (HEAD)** | 405 |
| **HTTP status (GET)** | 200 — ~1.06 MB |
| **Page heading** | `Bestselling Computers & Technology Audiobooks` |
| **Results** | 1 - 20 of 100 |
| **Visible titles** | _Co-Intelligence_ (Mollick), _Algorithms to Live By_ (Christian), _The Infinity Machine_ (Mallaby), _Elon Musk_ (Isaacson), _Elon Musk_ (Vance — separate biography of the same subject/title), _Empire of AI_ (Hao), _The Singularity Is Nearer_ (Kurzweil), _Nexus_ (Harari), _The Coming Wave_ (Suleyman), _The Wright Brothers_ (McCullough — biography), _Project Maven_ (Manson). |
| **Product links** | Yes — all 20 `/pd/` links. |
| **Cadence** | "updated daily!" |
| **Topical contamination** | **Moderate.** Observed biographical leakage (two _Elon Musk_ entries — Isaacson and Vance — are separate biographies of the same subject/title, and _The Wright Brothers_ is a biography). _AI Engineering_ (Chip Huyen) is a narrow technical manual. The category is heavily AI/tech-culture focused. |
| **Adult-nonfiction fit** | **Excellent.** All titles are adult nonfiction; no fiction, children's, YA. |

**Editorial-fit verdict:** Moderate fit for AutoScout. A focused tech supplementary
option. Strongly skewed toward AI and tech culture rather than general nonfiction.
Biographical leakage and a narrow technical manual reduce signal quality.
Good as a supplementary signal but too narrow and noisy for a primary.

#### 4. Money & Finance (node 18574547011)

**URL:** `https://www.audible.com/charts/best/money-finance-audiobooks/18574547011`

| Criterion | Observation |
|-----------|-------------|
| **HTTP status (HEAD)** | 405 |
| **HTTP status (GET)** | 200 — ~1.08 MB |
| **Page heading** | `Bestselling Money & Finance Audiobooks` |
| **Results** | 1 - 20 of 100 |
| **Visible titles** | _The Psychology of Money_ (Housel), _Good to Great_ (Collins), _Rich Dad Poor Dad_ (Kiyosaki), _Die With Zero_ (Perkins), _The Millionaire Next Door_ (Stanley), _The Simple Path to Wealth_ (JL Collins), _The Book on Rental Property Investing_ (Turner), _The Total Money Makeover_ (Ramsey), _I Will Teach You to Be Rich_ (Sethi), _Poor Charlie's Almanack_ (Munger). |
| **Product links** | Yes — all 20 `/pd/` links. |
| **Cadence** | "updated daily!" |
| **Topical contamination** | **High.** Multiple titles are narrow personal-finance how-to books (_The Book on Rental Property Investing_, _How to Day Trade for a Living_, _Breaking Free from Broke_) or prescriptive money manuals. These are prescriptive manual/how-to content. Broad-audience money psychology books are also present, but the manual/how-to proportion is the highest of any category. |
| **Adult-nonfiction fit** | **Excellent on category.** Adult nonfiction. The concern is content shape: most titles are prescriptive manual/how-to content rather than editorial nonfiction. |

**Editorial-fit verdict:** Moderate fit. Personal finance overlaps with the broader
nonfiction portfolio, but the list contains a meaningful fraction of narrow
how-to/manual content that AutoScout may want to exclude deterministically. Worth
onboarding with exclusion rules for narrow financial how-to/real-estate/day-trading
titles.

#### 5. Health & Wellness (node 18573370011)

**URL:** `https://www.audible.com/charts/best/health-wellness-audiobooks/18573370011`

| Criterion | Observation |
|-----------|-------------|
| **HTTP status (HEAD)** | 405 |
| **HTTP status (GET)** | 200 — ~1.08 MB |
| **Page heading** | `Bestselling Health & Wellness Audiobooks` |
| **Results** | 1 - 20 of 100 |
| **Visible titles** | _The Body Keeps the Score_ (van der Kolk), _Good Energy_ (Means), _Man's Search for Meaning_ (Frankl), _Don't Believe Everything You Think_ (Nguyen), _The Untethered Soul_ (Singer), _When Breath Becomes Air_ (Kalanithi), _The Anxious Generation_ (Haidt), _Breath_ (Nestor), _Outlive_ (Attia), _Adult Children of Emotionally Immature Parents_ (Gibson), _Attached_ (Levine). Note: _Atomic Habits_ and _How to Win Friends & Influence People_ also appear — these are cross-category bestsellers, not health-specific titles. |
| **Product links** | Yes — all 20 `/pd/` links. |
| **Cadence** | "updated daily!" |
| **Topical contamination** | **High.** The list is heavily pop-psychology, self-help, and wellness (_Don't Believe Everything You Think_, _The Untethered Soul_, _The Courage to Be Disliked_). _The Regulated Bitch_ and _ADHD Is Awesome_ have casual/workbook tones. _It's Not You_ (Ramani Durvasula) is pop psychology. The category also includes legitimate health/medical books (_The Body Keeps the Score_, _Good Energy_, _Outlive_, _When Breath Becomes Air_) and cross-category bestsellers (_Atomic Habits_). Quality and credibility of individual titles would require separate evaluation — the chart only establishes what sells. |
| **Adult-nonfiction fit** | **Mixed.** Adult nonfiction in category, but most titles are pop-psychology, self-help, and wellness content that AutoScout would likely exclude. Also includes cross-category bestsellers (_Atomic Habits_) that are more business/self-help. |

**Editorial-fit verdict:** Weakest editorial fit of the five. Health & Wellness on
Audible's chart is heavily pop-psychology, self-help, and wellness content that
overlaps with AutoScout's likely exclusion zones. The first-party chart establishes
titles and categories, not scientific validity — quality/credibility would require
separate evaluation. Would require aggressive exclusion rules. Not recommended
as a primary signal.

---

### Prior recommendation: Biographies & Memoirs (node 18571951011) — Deprioritised

The initial Audible recommendation in earlier versions of this document focused on
**Biographies & Memoirs** (`/charts/best/biographies-memoirs-audiobooks/18571951011`).
This is a technically viable page (same HEAD=405, GET=200, 20/100, "updated daily!"
pattern). It is **deprioritised** because the editor has stated that memoirs are
the least-interesting nonfiction category for AutoScout's scope. This is an
editorial-fit judgment based on stated scope preference, not a technical failure.
The page works perfectly.

- **If AutoScout's editorial scope changes** to include memoir/biography, this
  page remains technically equivalent to the five charts above but is both editorially
  deprioritized and formally deferred under the runbook as written (HEAD=405).

---

### Category ranking for AutoScout

#### By technical feasibility (all identical)

All five categories plus Biographies & Memoirs share identical technical results:
HEAD=405 (procedural mismatch), GET=200 open, server-rendered, 20 items/page × 5
pages = 100 results, paginated, daily refresh. **No technical distinction among
them.**

#### By editorial-fit (subjective quality for AutoScout's nonfiction mission)

| Rank | Category | Editorial-fit verdict | Key reason |
|------|----------|-----------------------|------------|
| **1** | **Science & Engineering** | **Strongest** — popular science, broad general-interest nonfiction, minimal memoirs/self-help leakage | Best topical breadth for AutoScout's mission; titles consistently cover topics of broad thematic interest with lowest memoir and self-help contamination. |
| **2** | **Business & Careers** | **Second** — includes memoir narratives and substantial self-help/management content | Usable category but top 20 has memoir narratives (_Kitchen Confidential_, _Careless People_) and substantial prescriptive self-help/management content. Not unambiguously excellent. |
| **3** | **Computers & Technology** | **Moderate** — AI/tech-heavy, biographical leakage, one narrow manual | Focused tech supplementary option; observed biographical leakage (two _Elon Musk_ biographies plus _The Wright Brothers_) and _AI Engineering_ as a narrow manual. |
| **4** | **Money & Finance** | **Moderate** — broad money books mixed with narrow how-to manuals; highest manual proportion | Manual/how-to-heavy (real estate investing, day trading, personal finance prescriptive manuals). Usable with aggressive exclusion rules. |
| **5** | **Health & Wellness** | **Weakest** — pop-psychology, self-help, and wellness heavy | Most titles are pop-psychology, self-help, or wellness content. The chart establishes titles/categories, not scientific validity. Would require the heaviest editorial filtering. |
| — | **Biographies & Memoirs** | **Deprioritised** — least-interesting nonfiction category per stated editor preference | Technically equivalent to the five charts but editorially deprioritised and formally deferred under the runbook as written (HEAD=405). |

**Bottom line:** Science & Engineering is the best first category on the current
editorial criterion — broad general-interest nonfiction with minimal memoir and
self-help leakage. Business & Careers is second but carries notable memoir and
self-help/management content. Computers & Technology is a focused tech
supplementary option. Money & Finance is manual/how-to-heavy. Health &
Wellness is the weakest, heavily pop-psychology/self-help/wellness.
Biographies & Memoirs is deprioritised by stated scope preference.

---

## Barnes & Noble

### Nonfiction collection — bestseller carousel

**URL:** `https://www.barnesandnoble.com/collections/books/nonfiction`

| Criterion | Observation |
|-----------|-------------|
| **HTTP status** | 200, no unrelated redirect (`curl -sIL`). |
| **Page heading** | `<h1 class="collection__title">Nonfiction Books</h1>` — server-rendered. |
| **Bestseller section heading** | `<h2 class="carousel__heading">Nonfiction Bestsellers</h2>` with a first-party "See All" link to `/collections/books/bestselling-books?attributes.webCategoryDisplayName=Nonfiction`. |
| **Visible titles in bestseller section** | **None** — the `#carousel_nonfiction-bestsellers` section contains **only skeleton/loading placeholders** (56 skeleton class instances, `carousel__slider--retail-loading` marker). No `carousel-card__title`, `carousel-card__author`, or `/w/` links within the section bounds. The bestseller carousel is JS-hydrated. |
| **Visible titles elsewhere on page** | 17 `carousel-card__title` elements exist globally on the page, but these belong to **other carousels**: 1 title (`Regime Change`) is in "Best New Nonfiction Hardcovers" and 16 soccer-themed titles are in "Celebrate the Beautiful Game." These are not part of the Nonfiction Bestsellers section. |
| **"See All" deep-link** | `https://www.barnesandnoble.com/collections/books/bestselling-books?attributes.webCategoryDisplayName=Nonfiction` — HTTP 200, H1 "Bestselling Books." **Zero** visible `/w/` product links in raw HTML. **Zero** visible `/authors/` links. All product data is serialized/hydrated. The query param `attributes.webCategoryDisplayName=Nonfiction` is JS-consumed, not server-filtered. |
| **Access** | Open — no paywall, CAPTCHA, or bot-wall. Shopify/Oxygen/Hydrogen stack via Cloudflare; `curl` returns full HTML. |
| **Adult-nonfiction fit** | The collection page is explicitly Nonfiction and the carousel heading says "Nonfiction Bestsellers," but the bestseller data is not visible. |

**Verdict: Deferred — JS-loaded bestseller carousel.** The page has the correct
heading and a first-party See All link, but the `#carousel_nonfiction-bestsellers`
section has **zero visible cards** — only loading placeholders. The 17 global
`carousel-card__title` elements are from unrelated carousels. The See All URL has
no visible product links. This corrects the prior draft's claim that 17 visible
cards existed in the bestseller section. B&N does not pass the exact bounded-list
visibility gate. Close deferral because the page exposes a heading, link, and
visible data in adjacent carousels — if B&N makes the bestseller carousel or a
standalone URL server-render, recheck immediately.

### Bestsellers landing page

**URL:** `https://www.barnesandnoble.com/pages/bestsellers`

| Criterion | Observation |
|-----------|-------------|
| **HTTP status** | 200 |
| **Page heading** | `<h1 class="landing-page__title">Bestsellers</h1>` |
| **Visible book data** | **None** — all book titles, authors, and links are in an inline `<script>` React Router serialized JSON payload. No visible title/author HTML elements for books. |

**Verdict: Deferred (JS-loaded).**

---

## Amazon

### Top-level Books Bestsellers

**URL:** `https://www.amazon.com/gp/bestsellers/books/`

| Criterion | Observation |
|-----------|-------------|
| **HTTP status (HEAD)** | **503** (`curl -sI`) |
| **HTTP status (GET, vanilla curl)** | **Variable** — usually 503 (automated-access notice, ~2.7 KB). Observed once at 200 (~76 KB) in independent checks. Not deterministically replay-safe. |
| **HTTP status (GET, browser UA)** | **200** — full HTML with titles, authors, links. |
| **Redirects** | None |
| **Page heading** | `<h1>Best Sellers in Books</h1>` — server-rendered with browser UA. |
| **Visible titles/authors** | **Yes** (browser UA) — `<span class="p13n-sc-css-line-clamp-*">` elements with visible title text and author names. Product links present. |
| **Exact bounded list** | Yes — paginated ~30 items per page, numbered #1–#30. |
| **Adult-nonfiction fit** | **FAILS** — mixed all-categories list. No umbrella "Nonfiction" node in Amazon's bestseller taxonomy. |
| **Access** | **Nondeterministic** — HEAD→503, vanilla GET variable (usually 503, once observed 200 at ~76 KB in an independent check), browser-UA GET→200. Not replay-safe for CI. |
| **robots.txt** | `/gp/bestsellers` is **not** in the Disallow list. The automated-access page directs scrapers to the Product Advertising API. |
| **Refresh cadence** | First-party label: "Updated frequently." |
| **URL stability** | Stable — well-known URL pattern. |

### Subcategory bestseller pages

**Example URL:** `https://www.amazon.com/gp/bestsellers/books/2` (Biographies)

| Criterion | Observation |
|-----------|-------------|
| **HTTP status (HEAD)** | 503 |
| **HTTP status (vanilla GET)** | **503** — consistently returns the automated-access notice (~2.7 KB) in all repeated checks. |
| **HTTP status (browser-UA GET)** | 200 |
| **Page heading** | `<h1>Best Sellers in Biographies</h1>` |
| **Visible titles/authors** | **Yes** (browser UA) — e.g. `Strangers: A Memoir of Marriage` — Belle Burden; `London Falling` — Patrick Radden Keefe; `The Steps: A Memoir` — Sylvester Stallone. |
| **Bounded list** | Yes — ~30 items, paginated, numbered. |
| **Adult-nonfiction fit** | **Yes for individual subcategories** — a single subcategory like Biographies (node 2) contains adult nonfiction. But no single subcategory covers all adult nonfiction; the signal would be partial. A subcategory passes the data gate on its own terms; the limitation is scope, not structural infeasibility. |

**Verdict: Deferred — UA-conditional access.** HEAD→503, vanilla GET→503 in all
repeated checks, browser-UA GET→200. The subcategory page requires a browser
User-Agent; not replay-safe for CI.

---

## Indigo (Canada)

**URLs probed:**
- `https://www.indigo.ca/collections/bestselling-books` — HTTP 200, 431 KB
- `https://www.indigo.ca/collections/indigo-non-fiction-bestsellers` — HTTP 200, 392 KB

| Criterion | Observation |
|-----------|-------------|
| **HTTP status** | 200 |
| **Page heading** | `<title>Up to 25% off Indigo Bestsellers</title>` |
| **Server-rendered book data** | **FAILS** — the product grid uses `constructor-product-grid js-constructor-product-grid` with `is-skeleton` placeholder cards. Zero visible `/products/` book-level anchors (only `/products/anywhere-bag-green` for a non-book item). The text "Regime Change" and "Maggie Haberman" appears only inside script/analytics payload data — not as visible title/author card elements and not linked to visible product anchors. |
| **Visible title cards** | **None** — all `cio-product-card` elements are `is-skeleton`. |

**Verdict: Deferred (JS-loaded).** Constructor.io grid is purely client-side.

---

## Other major retailers

| Retailer | URL | Status | Detail |
|----------|-----|--------|--------|
| **Books-A-Million** | `https://www.booksamillion.com/bestsellers` | ❌ 403 | Cloudflare challenge |
| **Walmart** | `https://www.walmart.com/browse/books/bestsellers/3920` | ❌ 307 → `/blocked` | Bot-detection redirect; all book paths blocked |
| **Target** | `https://www.target.com/c/books/-/N-5xtcr` | ❌ 404 | Books section removed or URL changed |
| **Waterstones** | `https://www.waterstones.com/books/bestsellers/non-fiction` | ❌ 403 | Cloudflare challenge even with browser UA; consistent with #199 and #299 |
| **Foyles** | `https://www.foyles.co.uk/bestsellers` | ❌ 403 | Cloudflare challenge |
| **Powell's** | `https://www.powells.com/bestsellers` | ❌ 403 | Cloudflare challenge |
| **Half Price Books** | `https://www.hpb.com/bestsellers` | ❌ 403 | Cloudflare challenge |
| **Kobo / Rakuten** | `https://www.kobo.com/us/en/ebooks/nonfiction` | ❌ 403 | Cloudflare challenge |
| **Apple Books** | `https://books.apple.com/us/charts/books` | ❌ 404 (SPA shell) | Ember.js SPA — HTML is an app shell; JS-loaded |
| **Bookshop.org** | `https://bookshop.org/?category=nonfiction` | ❌ CF challenge | Redirects `/books?category=nonfiction` → `/?category=nonfiction`, then Cloudflare JS challenge |
| **Bookshop.org** | `https://bookshop.org/lists/nonfiction-bestsellers` | ❌ CF challenge | HTTP 200 on HEAD, Cloudflare JS challenge on GET body |
| **ThriftBooks** | `https://www.thriftbooks.com/browse/?b.search-type=category&b.category=nonfiction` | ❌ Wrong shape | H1 "Search page" — general browse, not a bestseller list |
| **Better World Books** | `https://www.betterworldbooks.com/shop/category/nonfiction` | ❌ 403 | Cloudflare challenge |
| **Blackwell's** | `https://blackwells.co.uk/bookshop/collection/Bestselling-Non-Fiction/_/N-1z13tvj` | ❌ 403 | Cloudflare challenge |
| **Audible (non-chart URL)** | `https://www.audible.com/ep/nonfiction-best-sellers` | ❌ Wrong shape | GET 200 but zero `/pd/` product links, no visible title/author data. |
| **Google Play Books** | `https://play.google.com/store/books/category/NON_FICTION` | ❌ 404 | Category page returns 404 |
| **Alibris** | `https://www.alibris.com/books/bestsellers/nonfiction` | ❌ 403 | Cloudflare challenge |
| **AbeBooks** | `https://www.abebooks.com/books/bestsellers/nonfiction/index.shtml` | ❌ 404 | Path not found |
| **eBay** | `https://www.ebay.com/b/Nonfiction-Books/261186/bn_7117457753` | ❌ 302 → error | Redirects to error page |
| **HarperCollins** | `https://www.harpercollins.com/collections/books-nonfiction-best-sellers` | ❌ 403 | Cloudflare challenge |
| **Penguin Random House** | `https://www.penguinrandomhouse.com/books/bestsellers/nonfiction/` | ❌ 404 | Path not found |
| **Simon & Schuster** | `https://www.simonandschuster.com/bestsellers/nonfiction` | ❌ 403 | Blocked |
| **Macmillan** | `https://www.macmillan.com/bestsellers` | ❌ Unreachable | No response (timeout) |

---

## Consolidated gate table

| Retailer | HTTP / Access | Server-rendered | Exact bounded list | Adult NF fit | Book-level links | Verdict |
|----------|--------------|-----------------|---------------------|-------------|-----------------|---------|
| **Audible (charts, all categories)** | ✅ GET 200; ⚠️ HEAD 405 | ✅ Visible titles + authors | ✅ 20/page × 5 pages, paginated | ✅ Per category | ✅ `/pd/` links | **Deferred** — HEAD 405 (GET is open) |
| **B&N nonfiction collection** | ✅ 200 | ❌ JS-loaded bestseller carousel | ❌ 0 visible cards in section | ✅ Explicitly NF heading | ❌ None in section | **Deferred** — JS-loaded |
| **B&N /pages/bestsellers** | ✅ 200 | ❌ JS-serialized only | N/A | N/A | ❌ None visible | **Deferred** — JS-loaded |
| **Amazon top-level** | ⚠️ Variable (usually 503) | ✅ With browser UA | ✅ Paginated grid | ❌ Mixed all-categories | ✅ Product links | **Deferred** — access + no NF category |
| **Amazon subcategory** | ⚠️ UA-conditional (HEAD 503, GET 503) | ✅ With browser UA | ✅ Paginated grid | ✅ Per subcategory | ✅ Product links | **Deferred** — UA-conditional access |
| **Indigo** | ✅ 200 | ❌ JS-loaded (Constructor.io) | ❌ Skeleton cards only | N/A | ❌ None visible | **Deferred** — JS-loaded |
| All others | ❌ Blocked / 404 | N/A | N/A | N/A | N/A | **Rejected** |

---

## Ranked recommendation

### 1. Audible Bestseller Charts — strongest candidate, but category ranking reflects editorial fit

**Best first category on current editorial criterion: Science & Engineering** (node 18580540011)
**Second: Business & Careers** (node 18572029011)

All Audible `/charts/best/` category pages share identical technical properties:
GET=200 open, server-rendered with visible titles + primary authors + `/pd/` links,
bounded (20 items per page, 100 total, paginated), explicitly adult nonfiction per
category, daily cadence, no UA tricks, no CAPTCHA.

The **sole procedural blocker** is the runbook's prescribed `curl -sIL` HEAD feasi-
bility check: all Audible chart pages return 405 Method Not Allowed because the
server does not support HEAD on this endpoint. This is a procedural mismatch, not
an access block. GET is consistently 200 on every category tested. All five categories
share identical technical feasibility.

**Why Biographies & Memoirs was the initial recommendation and why it changed:**
The first scan checked Biographies & Memoirs (node 18571951011) because it was the
most obviously "adult nonfiction" category. That page is technically viable. However,
follow-up research across five additional categories showed that **Science &
Engineering** and **Business & Careers** have stronger editorial alignment with
AutoScout's nonfiction mission (broad thematic interest, fewer personal narratives).
Biographies & Memoirs is deprioritised based on stated scope preference. This is an
editorial-fit judgment, not a technical failure.

**Recommendation:** If the runbook's HEAD-based feasibility check is amended or
excepted, the best Audible category to onboard first is **Science & Engineering**
(node 18580540011) — broad general-interest nonfiction with minimal memoir and
self-help leakage. **Business & Careers** (node 18572029011) is second, usable
with filtering for memoir narratives and self-help/management content.
**Computers & Technology** is a focused tech supplementary option (third).
**Money & Finance** is manual/how-to-heavy (fourth). **Health & Wellness**
requires the most filtering and is not recommended (fifth). Biographies & Memoirs
is deprioritised by stated scope preference.

### 2. Amazon individual nonfiction subcategory — passes data, requires browser UA

A single subcategory page (e.g. Biographies node 2) passes the data gate with a
browser UA, but HEAD and vanilla GET consistently return 503. Access is
UA-conditional, not replay-safe for CI.

**Recommendation:** Recheck only with a stable browser-profile extraction approach.

### 3. Barnes & Noble — JS-loaded bestseller carousel

The nonfiction collection page has the correct heading and a See All link, but the
bestseller carousel section has zero visible cards. The See All URL has zero visible
product links. B&N does not pass the exact-list visibility gate.

**Recommendation:** Recheck if B&N makes the bestseller carousel or a standalone
nonfiction bestseller URL server-render.

### 4. Other retailers — no viable candidates

Every other probed retailer is either blocked, JS-loaded, wrong-shape, or lacks a
working bestseller list.

---

## Existing portfolio coverage

The current portfolio contains three `bestseller/ranking` sources:

- **Publishers Weekly Hardcover Frontlist Nonfiction** — weekly Circana BookScan
  ranking (open, server-rendered, table-structured). Family: `bestseller/ranking`.
- **USA TODAY Bestselling Books Nonfiction** — dedicated adult nonfiction
  bestseller list (open, server-rendered). Family: `bestseller/ranking`.
- **Spiegel Bestseller Sachbuch** — German nonfiction bestseller list. Family:
  `bestseller/ranking`.

One additional retailer audiobook-popularity source exists but is **not** a
`bestseller/ranking` source:

- **Audiobooks.com Nonfiction Audiobook Bestsellers** — family `audiobook ranking`,
  implicit "Sort by: Bestselling" popularity order with no numeric rank or sales
  units. A secondary audiobook-popularity signal, not a ranked bestseller list.

---

## Methodology

All checks performed on 2026-07-18 with:

```bash
# Head request (status, redirects, headers)
curl -sIL --max-time 30 <url>

# Body retrieval (vanilla)
curl -sL --max-time 30 <url>

# Body retrieval (browser User-Agent)
curl -sL --max-time 30 \
  -H "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36" \
  <url>
```

Page content examined for: H1/page heading, visible book titles in HTML elements
(not script/JSON payloads or skeleton placeholders), visible author names, book-level
anchor links, pagination markers, and JS-loading indicators (empty grid containers,
SPA shells, `is-skeleton` classes, `constructor-product-grid` placeholders).

B&N section-specific analysis: extracted `#carousel_nonfiction-bestsellers` by
section bounds (`<section id="carousel_nonfiction-bestsellers"...></section>`),
counted visible title/author/link elements within that section only. Distinguished
global `carousel-card__title` elements by tracing their containing section IDs.

Amazon access: tested HEAD, vanilla GET, and browser-UA GET against both
top-level and subcategory URLs in multiple repeated checks. Top-level vanilla GET
returned 503 in all local checks; independent review observed one 200 (~76 KB)
response in a separate check window (reported as variable). Subcategory vanilla
GET returned 503 in all checks by both parties (reported as UA-conditional).

Audible five-category follow-up: five Audible chart pages (Computers & Technology
node 18573211011, Money & Finance node 18574547011, Science & Engineering node
18580540011, Business & Careers node 18572029011, Health & Wellness node
18573370011) probed with HEAD (all 405), vanilla GET (all 200, ~1.06–1.08 MB each),
title/author/product-link extraction, results-count extraction, pagination checks,
and topical contamination assessment. The prior Biographies & Memoirs data was
retained from the original probe set.

No automated crawling, discovery, or recommendation engine was used. Each URL
was probed directly. No evasive workarounds beyond the browser UA test were
attempted.
