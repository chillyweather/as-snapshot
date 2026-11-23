# AutoScout — Scouting Workflow and Source Map

Status: translated and consolidated from Russian source notes. This is not a PRD yet.

Source files used:

- `AutoScout.md`
- Related context from `Project Analysis Instructions.md`
- Related context from `Editorial Declaration.md`
- Risk-screening references consolidated separately in `02-editorial-fit-and-risk-screening.en.md`

## 1. Purpose of the tool

AutoScout should help collect, filter, and rank adult nonfiction books that may be suitable for the publisher.

The tool should:

1. Collect books from external sources:
   - awards
   - sales rankings
   - bestseller lists
   - media lists and reviews
   - Goodreads / StoryGraph reader signals
   - audiobook charts
   - rights and deal mentions
2. Keep only adult nonfiction that may fit the publisher.
3. Check whether each book has already been published in Russian.
4. Mark obvious prohibited or risky content if visible from public materials:
   - descriptions
   - reviews
   - annotations
   - table of contents
   - samples / Look Inside
   - author interviews
   - media materials
5. Rank results so that books with the highest number of independent external signals appear first.

## 2. Core scouting instruction

Collect a list of adult nonfiction books potentially suitable for the publisher from:

- awards
- bestseller lists
- sales rankings
- media lists
- Goodreads
- audiobook rankings
- rights / deal signals

Do not search for books “just by topic.” Candidate discovery comes from configured source families; targeted search can enrich or verify already discovered candidates. Include only books with an external signal, such as:

- award win, shortlist, or longlist
- sales ranking or bestseller-list placement
- audiobook chart placement for an underlying book/work
- Goodreads Choice Awards / reader interest signal
- mention in an authoritative media list
- rights or deal mention

For each book, capture:

- source
- source URL
- title
- author
- publisher
- publication year
- topic
- short description
- why the book entered the list
- whether it has been published in Russian
- Russian-edition URL, if found
- Amazon / Goodreads / publisher-page links
- visible risk content

Subscription-only sources should not be bypassed. Mark them as:

> source is paid / subscription required

## 3. Candidate inclusion rules

Include a book if it has at least one strong signal or several weaker signals.

### 3.1 Strong or useful signals

1. Bestseller / sales ranking
   - Amazon, Der Spiegel, Waterstones, Barnes & Noble, Kinokuniya, Kyobo, YES24, Books.com.tw, Audible, etc.

2. Sustained sales / rising-star behavior
   - Amazon BSR 50,000–150,000 = watch zone.
   - BSR below 50,000 within the last 12 months = strong long-tail signal.

3. Award signal
   - winner, shortlist, or longlist.
   - Relevant categories include adult nonfiction: history, science, society, economics, biography, psychology, technology, etc.

4. Media signal
   - best books
   - books to read
   - books of the year
   - CEO recommendations
   - author interviews
   - serious reviews in strong media outlets

5. Audiobook signal
   - Audible / Apple / Libro.fm placement for an underlying book/work.
   - Especially important when audiobook performance appears before print/e-book performance.

6. Goodreads / StoryGraph signal
   - Choice Awards
   - Most Anticipated
   - high want-to-read count
   - noticeable review growth

7. Rights / deal signal
   - Publishers Marketplace
   - Publishers Weekly
   - The Bookseller
   - fair catalogues
   - sold in multiple territories

8. Trend + specific book
   - Start with a trend from Google Trends, conferences, McKinsey, business media, etc.
   - Then include only a book that already has an external signal.

## 4. What not to include

Do not include:

- pure fiction
- children’s / YA books
- esotericism and religion
- narrow academic books without a mass-market signal
- books without any external confirmation of market or editorial interest

Note: some memoirs, essays, journalism, narrative nonfiction, popular psychology, self-development, business, popular science, health, parenting, and cultural nonfiction may fit depending on the publisher’s editorial scope and risk profile.

## 5. Russian-edition check

A book remains a stronger scouting candidate if no Russian edition is found.

If a Russian edition is found, record the link and do not treat the book as an unclaimed acquisition opportunity without further validation.

### 5.1 Sources to check

Retailers and e-book platforms:

- Ozon — https://www.ozon.ru/
- Labirint — https://www.labirint.ru/
- Wildberries — https://www.wildberries.ru/
- LitRes — https://www.litres.ru/
- Chitai-gorod — https://www.chitai-gorod.ru/
- Book24 — https://book24.ru/
- My-shop — https://my-shop.ru/

Russian publishers:

- the publisher’s own catalogue/site
- MIF — https://www.mann-ivanov-ferber.ru/
- Eksmo — https://eksmo.ru/
- Bombora — https://bombora.ru/
- AST — https://ast.ru/
- Piter — https://www.piter.com/
- Corpus — https://ast.ru/redactions/corpus/
- Individuum — https://individuum.ru/

### 5.2 Search methods

Search by:

- original title
- author name in Latin script
- possible Russian transliteration of the author’s name
- approximate Russian translation of the title
- ISBN, if available

### 5.3 Russian-publication statuses

Use one of three statuses:

- `not found in Russian`
- `found in Russian` — include link
- `unclear` — explain what could not be checked

## 6. Risk-content quick check

For scouting-stage screening, look for visible risk signals using public materials only.

Use:

- annotation
- publisher description
- table of contents, if available
- sample / Look Inside
- reviews
- Goodreads reviews
- author interviews
- media materials about the book

Screen for themes listed in the detailed risk file:

- LGBTQ / non-traditional relationships, gender transition, rejection of childbearing
- Russia, USSR, Putin, contemporary Russian politics
- war, WWII / Great Patriotic War, comparison of USSR and Nazi Germany, denial of the USSR’s role
- drugs, microdosing, psychoactive substances
- suicide, suicide methods, romanticizing suicide
- Nazi symbols, justification of Nazism
- extremist, terrorist, or “undesirable” organizations
- “foreign agents”
- obscene language
- religious, esoteric, and pseudoscientific practices
- violence, crime, lawbreaking

Use simple scouting-stage labels:

- `no visible risk`
- `clear visible risk`
- `text check required`

If risk is visible, add a short category label, for example:

- `Russia / politics`
- `non-traditional relationships`
- `drugs`
- `suicide`
- `WWII / USSR`
- `foreign agent`
- `esotericism`

If the book looks interesting but risk cannot be assessed from public data, keep it in the list with:

> text check required

Detailed criteria are consolidated in `02-editorial-fit-and-risk-screening.en.md`.

## 7. Output table for scouting results

The scouting output should be a single table sorted by number of independent signals, highest first.

Columns:

1. Title
2. Author
3. Annotation
4. Description
5. Signal count
6. Visible-risk status
7. Risk comment
8. English title
9. English author display
10. English annotation
11. Check date
12. Source
13. Source URL
14. Publisher / year
15. Category / topic
16. Why it entered the list
    - examples: FT shortlist, Amazon top-20, Goodreads winner, Audible bestseller, 3 media mentions
17. Signal summary
18. Priority label
19. Russian-edition status
20. Russian-edition URL
21. Links
22. Editor status
23. Editor note

`Annotation` is the compact synthesized scouting note for spreadsheet use. `Description` carries original English `content_description` scouting prose when available, regardless of source language, and must not contain copied publisher blurbs, reviews, article bodies, jacket copy, or book samples.

## 8. Source inventory

The source list is intentionally broad. The MVP should later narrow it, but this document preserves the full initial scouting map.

### 8.1 Amazon markets

Use adult nonfiction from general and category bestseller lists, especially if a book remains in the top, grows sharply, or appears across multiple countries.

- Amazon US — Books Best Sellers — https://www.amazon.com/gp/bestsellers/books/
- Amazon US — Business & Money
- Amazon US — Health, Fitness & Dieting
- Amazon US — Self-Help — https://www.amazon.com/gp/bestsellers/books/4736/
- Amazon US — Psychology & Counseling
- Amazon US — Science & Math
- Amazon US — Computers & Technology — https://www.amazon.com/gp/bestsellers/books/5/
- Amazon Charts US — https://www.amazon.com/charts
- Amazon UK — Books Best Sellers — https://www.amazon.co.uk/gp/bestsellers/books/
- Amazon UK — Business, Finance & Law
- Amazon UK — Health, Family & Lifestyle
- Amazon UK — Society, Politics & Philosophy — https://www.amazon.co.uk/gp/bestsellers/books/275389/
- Amazon UK — Science, Nature & Math
- Amazon Germany — Bücher Bestseller — https://www.amazon.de/gp/bestsellers/books/
- Amazon Germany — Business & Karriere
- Amazon Germany — Ratgeber
- Amazon Germany — Politik & Geschichte — https://www.amazon.de/gp/bestsellers/books/15777241/
- Amazon Germany — Naturwissenschaften & Technik
- Amazon France — Meilleures ventes Livres — https://www.amazon.fr/gp/bestsellers/books/
- Amazon France — Business et économie
- Amazon France — Santé, forme et diététique — https://www.amazon.fr/gp/bestsellers/books/301132/
- Amazon France — Sciences humaines
- Amazon Italy — Libri Bestseller — https://www.amazon.it/gp/bestsellers/books/
- Amazon Italy — Economia, affari e finanza — https://www.amazon.it/gp/bestsellers/books/508776031/
- Amazon Italy — Salute e benessere — https://www.amazon.it/gp/bestsellers/books/508776031/
- Amazon Spain — Libros más vendidos — https://www.amazon.es/gp/bestsellers/books/
- Amazon Spain — Empresa, estrategia y gestión
- Amazon Spain — Salud, familia y desarrollo personal
- Amazon Japan — Books Best Sellers — https://www.amazon.co.jp/gp/bestsellers/books/
- Amazon Japan — Business & Economics — https://www.amazon.co.jp/gp/bestsellers/books/466282/
- Amazon Japan — Nonfiction — https://www.amazon.co.jp/gp/bestsellers/books/466284/
- Amazon Japan — Health — https://www.amazon.co.jp/gp/bestsellers/books/466304/
- Amazon Canada — Books Best Sellers — https://www.amazon.ca/gp/bestsellers/books/
- Amazon Australia — Books Best Sellers — https://www.amazon.com.au/gp/bestsellers/books/
- Amazon India — Books Best Sellers — https://www.amazon.in/gp/bestsellers/books/
- Amazon Netherlands — Boeken Bestsellers — https://www.amazon.nl/gp/bestsellers/books/
- Amazon Mexico — Libros más vendidos — https://www.amazon.com.mx/gp/bestsellers/books/
- Amazon Brazil — Livros mais vendidos — https://www.amazon.com.br/gp/bestsellers/books/
- Amazon Singapore — Books Best Sellers — https://www.amazon.sg/gp/bestsellers/books/
- Amazon UAE — Books Best Sellers — https://www.amazon.ae/gp/bestsellers/books/

### 8.2 Other retailers and sales rankings

Use adult nonfiction from bestseller / ranking / chart pages, especially outside the US and UK.

English-language markets:

- Barnes & Noble — Bestsellers — https://www.barnesandnoble.com/b/books/_/N-1fZ29Z8q8
- Barnes & Noble — Best Books
- Waterstones — Bestsellers — https://www.waterstones.com/books/bestsellers
- Waterstones — Non-Fiction
- Bookshop.org — Best Sellers — https://bookshop.org/lists/bestsellers
- Kobo — Bestselling eBooks
- Apple Books Charts — https://books.apple.com/us/charts

Germany / Europe:

- Der Spiegel Bestseller — Sachbuch
- Buchreport Bestseller
- Thalia Bestseller
- FNAC — Meilleures ventes livres
- La Feltrinelli — Classifiche libri
- Casa del Libro — Más vendidos — https://www.casadellibro.com/libros-mas-vendidos
- El Corte Inglés — Libros más vendidos — https://www.elcorteingles.es/libros/mas-vendidos/

Japan:

- Kinokuniya Japan — Bestsellers
- honto — Ranking — https://honto.jp/ranking.html
- Rakuten Books — Ranking
- Honya Club — Ranking
- Oricon — Book Ranking — https://www.oricon.co.jp/rank/ob/

South Korea:

- Kyobo Book Centre — Bestsellers — https://product.kyobobook.co.kr/bestseller/total
- YES24 — Bestseller — https://www.yes24.com/24/Category/BestSeller
- Aladin — Bestseller
- Korean Publishers Association — https://kpa21.or.kr/

China / Taiwan / Hong Kong:

- Douban Books — https://book.douban.com/
- Dangdang — Book Bestseller
- JD Books — https://book.jd.com/
- Books.com.tw — Bestseller — https://www.books.com.tw/web/sys_saletopb/books/
- Eslite — Bestseller — https://www.eslite.com/category/1
- Kingstone — Bestseller

India / Southeast Asia:

- Flipkart — Books
- Crossword India — Bestsellers — https://www.crossword.in/
- Kinokuniya Singapore — Bestsellers — https://singapore.kinokuniya.com/
- Popular Singapore — https://www.popular.com.sg/
- Times Bookstores Singapore

### 8.3 Awards

Use winners, shortlists, and longlists. Do not limit to business books; the publisher is interested in broad adult nonfiction.

Business / economics / management:

- Financial Times / Standard Chartered Business Book of the Year — https://www.ft.com/bookaward — often paywalled
- Porchlight Business Book Awards — https://www.porchlightbooks.com/awards
- Axiom Business Book Awards — https://axiomawards.com/
- The Business Book Awards UK — https://www.businessbookawards.co.uk/
- National Business Book Award Canada — https://www.nbbaward.com/
- Thinkers50 Awards
- Bracken Bower Prize / FT young business writers — https://www.ft.com/bookaward — often paywalled
- Chartered Management Institute Management Book of the Year — if the current cycle is available
- 800-CEO-READ / Porchlight archive — https://www.porchlightbooks.com/awards

Broad nonfiction:

- Baillie Gifford Prize for Non-Fiction — https://www.thebailliegiffordprize.co.uk/
- Pulitzer Prize — General Nonfiction — https://www.pulitzer.org/prize-winners-by-category/223
- Pulitzer Prize — Biography — https://www.pulitzer.org/prize-winners-by-category/222
- National Book Awards — Nonfiction — https://www.nationalbook.org/national-book-awards/years/
- National Book Critics Circle Awards — https://www.bookcritics.org/awards/
- PEN America Literary Awards — https://pen.org/literary-awards/
- Nero Book Awards — Non-Fiction — https://nerobookawards.com/
- British Academy Book Prize
- Royal Society Trivedi Science Book Prize — https://royalsociety.org/medals-and-prizes/science-book-prize/
- PEN/E. O. Wilson Literary Science Writing Award — https://pen.org/literary-awards/
- Phi Beta Kappa Book Awards
- Cundill History Prize — https://www.cundillprize.com/
- Wolfson History Prize — https://www.wolfsonhistoryprize.org.uk/
- Orwell Prize for Political Writing — https://www.orwellfoundation.com/the-orwell-prizes/ — political-risk caution
- Orwell Prize for Reporting — https://www.orwellfoundation.com/the-orwell-prizes/ — political-risk caution

Australia / Canada / New Zealand:

- Prime Minister’s Literary Awards Australia — https://www.arts.gov.au/pm-literary-awards
- Victorian Premier’s Literary Awards — https://www.wheelercentre.com/victorian-premiers-literary-awards
- New South Wales Premier’s Literary Awards — https://www.sl.nsw.gov.au/awards/nsw-premiers-literary-awards
- Governor General’s Literary Awards Canada — https://ggbooks.ca/
- BC and Yukon Book Prizes — https://bcyukonbookprizes.com/
- Ockham New Zealand Book Awards — https://www.nzbookawards.nz/new-zealand-book-awards/

Asia:

- Japan Booksellers’ Award / 本屋大賞 — https://www.hontai.or.jp/ — mostly fiction, but useful as a Japanese mass-market demand signal
- Books from Japan — Awards — useful entry point for Japanese awards
- Japan Foundation / JFNY Literary Series / Japanese literature resources — not a sales ranking, but useful for tracking books that travel in translation
- Singapore Book Awards — https://singaporebookpublishers.sg/singapore-book-awards/ — check nonfiction, business, illustrated nonfiction if available
- Singapore Literature Prize — check nonfiction / creative nonfiction if available
- Taipei International Book Exhibition Award — https://www.tibe.org.tw/en/ — check nonfiction / publishing-award categories
- Golden Tripod Awards Taiwan — https://gta.moc.gov.tw/ — Taiwanese book and publishing awards
- Hong Kong Publishing Biennial Awards — mainly a publishing-industry signal; use selectively
- Korea Publishing Culture Award — https://www.kpa21.or.kr/ — via Korean Publishers Association; check nonfiction / social books
- Korea Book Awards / Korean Publishers Association — https://kpa21.or.kr/ — if current lists exist
- Sahitya Akademi Awards India — https://sahitya-akademi.gov.in/ — mostly literary, but useful as an Indian-market cultural signal
- Tata Literature Live! Awards — https://tatalitlive.in/ — India; includes nonfiction
- Kalinga Literary Festival Book Awards — https://kalingaliteraryfestival.com/ — India; check nonfiction / business / current affairs
- JCB Prize for Literature — https://www.thejcbprize.org/ — fiction, but can remain in the broad Indian-market signal list; not primary for the publisher
- Asian Publishing Awards — industry source; use selectively
- AAS Book Prizes — https://www.asianstudies.org/grants-awards/book-prizes/ — academic books about Asia; include only with mass-review support

### 8.4 Media: best books, notable books, recommendations

Use adult nonfiction from lists such as:

- best books
- books of the year
- books to read
- business books
- science books
- technology books
- ideas books

Business / economics / technology:

- Financial Times — Books — https://www.ft.com/books — paywall
- The Economist — Culture / Books — often paywalled
- Bloomberg — Books — partly paywalled
- Harvard Business Review — Books — partly paywalled
- MIT Sloan Management Review — https://sloanreview.mit.edu/ — partly paywalled
- strategy+business — Best Business Books
- McKinsey — Author Talks — https://www.mckinsey.com/featured-insights/mckinsey-on-books/author-talks
- McKinsey — McKinsey on Books — https://www.mckinsey.com/featured-insights/mckinsey-on-books
- BCG — Publications — https://www.bcg.com/publications
- Deloitte Insights — https://www2.deloitte.com/us/en/insights.html
- PwC — Strategy+Business — https://www.strategy-business.com/
- Fast Company — Ideas — https://www.fastcompany.com/ideas
- Fast Company — Books — https://www.fastcompany.com/section/books
- Inc. — https://www.inc.com/
- Inc. — Books — https://www.inc.com/books
- Forbes — Editors’ Picks — https://www.forbes.com/editors-picks/
- Forbes — Books
- Fortune — Rankings — https://fortune.com/rankings/
- Business Insider — https://www.businessinsider.com/
- TechCrunch — https://techcrunch.com/
- Mashable
- Wired — Books — https://www.wired.com/tag/books/
- Wired — Ideas — https://www.wired.com/category/ideas/

Broad nonfiction / science / society:

- TIME — Books — https://time.com/section/books/
- The Guardian — Books — https://www.theguardian.com/books
- The New Yorker — Books — https://www.newyorker.com/books — paywalled / partly paywalled
- The Atlantic — Books — https://www.theatlantic.com/books/ — paywall
- The New York Times — Books — paywall
- Washington Post — Books — https://www.washingtonpost.com/books/ — paywall
- Los Angeles Times — Books — https://www.latimes.com/entertainment-arts/books
- NPR — Books — https://www.npr.org/books/
- BBC Culture — Books — https://www.bbc.com/culture/books
- Smithsonian Magazine — Books — https://www.smithsonianmag.com/tag/books/
- Scientific American — Books — https://www.scientificamerican.com/books/
- New Scientist — Books — partly paywalled
- Nature — Books & Arts — https://www.nature.com/nature/articles?type=book-review
- Science — Books et al.
- Quanta Magazine — https://www.quantamagazine.org/
- Nautilus — https://nautil.us/
- Aeon — https://aeon.co/
- Five Books — https://fivebooks.com/
- Next Big Idea Club — https://nextbigideaclub.com/magazine/
- Literary Hub — https://lithub.com/
- Book Riot — Nonfiction — https://bookriot.com/category/nonfiction/
- CrimeReads — https://crimereads.com/ — only nonfiction / true crime / investigation
- The Marginalian — https://www.themarginalian.org/ — use cautiously as an intellectual-interest signal, not as a sales ranking

Asia / international media:

- South China Morning Post — Books — https://www.scmp.com/culture/books
- The Japan Times — Books — https://www.japantimes.co.jp/culture/books/
- The Korea Times — Books
- Korea JoongAng Daily — Culture / Books
- The Straits Times — Books
- Taipei Times — Books — https://www.taipeitimes.com/News/feat
- The Hindu — Books — https://www.thehindu.com/books/
- Hindustan Times — Books — https://www.hindustantimes.com/books
- Scroll.in — Books
- The Indian Express — Books and Literature — https://indianexpress.com/section/books-and-literature/
- Nikkei Asia — https://asia.nikkei.com/ — paywall; check business / current-affairs books if available
- Caixin Global — https://www.caixinglobal.com/ — paywall; useful for Chinese economy/business themes and authors, not primarily as a book source

### 8.5 Goodreads, StoryGraph, and reader signals

Include adult nonfiction with high reader interest, awards placement, Most Anticipated placement, large number of ratings/reviews, or sharp growth.

- Goodreads Choice Awards — https://www.goodreads.com/choiceawards
- Goodreads — Best Books of the Year / Blog
- Goodreads — Lists — https://www.goodreads.com/list
- Goodreads — Giveaways — https://www.goodreads.com/giveaway
- StoryGraph — https://app.thestorygraph.com/
- LibraryThing — Zeitgeist — https://www.librarything.com/zeitgeist
- BookBrowse — Award Winners / Best Books — https://www.bookbrowse.com/

### 8.6 Audio

Include adult nonfiction that sells or performs well in audio.

The source methodology explicitly treats audiobooks as an early indicator of reader interest: a strongly performing audiobook may predict print and e-book success.

- Audible — Best Sellers — https://www.audible.com/adblbestsellers
- Audible — Business & Careers
- Audible — Money & Finance — https://www.audible.com/cat/Money-Finance-Audiobooks/18574547011
- Audible — Health & Wellness
- Audible UK — Best Sellers — https://www.audible.co.uk/adblbestsellers
- Apple Books Charts — https://books.apple.com/us/charts
- Libro.fm — Bestsellers — https://libro.fm/bestsellers
- Spotify Audiobooks
- Kobo Audiobooks — https://www.kobo.com/us/en/audiobooks

### 8.7 Rights, deals, early signals, catalogues

Include adult nonfiction if there is a deal signal, large advance, multiple-territory sale, rights buzz, fair catalogue appearance, pre-empt, or auction.

Do not bypass paid sources. Mark them as subscription-only.

- Publishers Marketplace — https://www.publishersmarketplace.com/ — subscription
- Publishers Lunch — https://lunch.publishersmarketplace.com/ — subscription / partly open
- Publishers Weekly — Book Deals — https://www.publishersweekly.com/pw/by-topic/industry-news/book-deals/index.html — partly open
- Publishers Weekly — Rights
- The Bookseller — Rights — https://www.thebookseller.com/rights — often paywalled
- The Bookseller — News — https://www.thebookseller.com/news — often paywalled
- BookBrunch — https://www.bookbrunch.co.uk/ — subscription
- Publishing Perspectives — https://publishingperspectives.com/
- Frankfurt Rights
- London Book Fair — https://www.londonbookfair.co.uk/
- BolognaBookPlus
- International Publishers Association — https://internationalpublishers.org/
- RightsDesk
- PubMatch — https://www.pubmatch.com/

### 8.8 Book clubs and curators

Use cautiously. These sources are often fiction-heavy, but sometimes include memoirs, essays, and general-interest nonfiction. They are secondary signals for the publisher.

- Belletrist / Emma Roberts — https://tertulia.com/belletrist
- Oprah’s Book Club — https://www.oprahdaily.com/entertainment/books/
- Reese’s Book Club — https://reesesbookclub.com/
- Read with Jenna — https://www.today.com/read-with-jenna
- Good Morning America Book Club
- Book of the Month — https://www.bookofthemonth.com/
- Barnes & Noble Book Club — https://www.barnesandnoble.com/h/book-club
- Indie Next List — https://www.bookweb.org/indie-next-list
- LibraryReads — https://libraryreads.org/

## 9. Notes for future product work

These are not final requirements yet, but they matter for the next PRD step:

- The full source list is too broad for an MVP.
- MVP should probably start with a limited number of source families.
- The main initial product output should likely be a weekly report / watchlist, not a complex dashboard.
- The system should preserve source evidence for every candidate.
- Risk flags are not final legal review; they are early-screening indicators.
- Russian-publication checks need uncertainty states, because title translation and transliteration can make matches ambiguous.
