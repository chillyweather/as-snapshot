# Issue #758: which discovery query variants return a page for the book

Recorded 2026-10-12 from live searches through the production search adapter.
No weekly run was started, no page was fetched, and no weekly or editorial state was changed.

## Method

The books are the candidates the 2026-10-05 run left with a placeholder description (`final_state: still_fallback` in `grounded-description-stage.json`) that were still in `latest-scouting-candidates.json`: 22 non-English and 5 English.
The issue speaks of 14 books; the artifact does not say which 14, so all 22 non-English ones were measured, and they include every book the issue names.
Five English candidates the same run grounded, and seven English candidates with a subtitle, were added as controls.

Each query went to the adapter with the candidate's `search_lang`, exactly as `HTTPDiscoveryProvider` sends it.
About 235 vendor requests were made.

| Variant | Query |
|---|---|
| sent | as sent on 2026-10-05: quoted title, author as printed by the source, `book`, ISBN |
| current | `_build_discovery_queries` today: the same with clean author names (#767) |
| no `book` | current without the word `book` |
| no ISBN | current without the ISBN |
| plain | quoted title and clean author names only |
| ISBN only | the ISBN alone |
| title only | the quoted main title alone |
| unquoted | main title and clean author names, no quotes |
| main title | current with only the main title quoted (the #708 and #772 fallback query) |

A cell reads `results / hits`.
A hit is a result whose page title contains the candidate's main title as a whole phrase, ignoring case and punctuation, on a host that is not a social network.
A hit says the page names this title.
It does not prove the page is about this book: for a generic title (*ADHD*, *Astrup*, *Bob Dylan*, *Husdraumar*) the title-only query collects hits about the subject, a company, or a television series.

## Result 1: the word `book`, the ISBN, and the author form

| Book | Lang | Reason on 2026-10-05 | sent | current | no `book` | no ISBN | plain | ISBN only | title only | unquoted |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| *Ich erklär das jetzt mal psychologisch* | de | `source_identity_disagreement` | 20/18 | 20/18 | 19/15 | 20/18 | 19/15 | — | 20/18 | 19/15 |
| *Wahre Verbrechen* | de | `title_mismatch` | 7/0 | 7/0 | 5/0 | 7/0 | 5/0 | — | 20/16 | 19/8 |
| *HACKEA TU CEREBRO* | es | `source_identity_disagreement` | 18/17 | 18/17 | 19/18 | 20/17 | 20/16 | 9/8 | 20/17 | 20/16 |
| *Ainsi pensait Michel Foucault* | fr | `insufficient_description_depth` | 16/9 | 16/9 | 18/9 | 16/9 | 18/9 | — | 13/9 | 20/13 |
| *Chief Geopolitical Officer* | fr | `author_mismatch` | 15/7 | 15/7 | 17/7 | 15/7 | 17/7 | — | 20/15 | 20/10 |
| *La sommation technologique* | fr | `search_or_fetch_failure` | 17/9 | 17/9 | 14/9 | 17/9 | 14/9 | — | 13/10 | 20/10 |
| *L’Année terrible des animaux* | fr | `no_results` | 4/2 | 4/2 | 4/2 | 4/2 | 4/2 | — | 4/3 | 18/2 |
| *Richelieu et l'Histoire* | fr | `author_mismatch` | 3/2 | 3/2 | 5/3 | 3/2 | 5/3 | — | 3/2 | 20/2 |
| *ADHD* | no | `source_identity_disagreement` | 20/16 | 20/17 | 20/16 | 19/19 | 20/13 | 4/0 | 20/16 | 20/13 |
| *Astrup* | no | `author_mismatch` | 16/10 | 16/10 | 16/10 | 19/8 | 20/9 | 20/1 | 20/19 | 20/8 |
| *Babymatboka* | no | `no_results` | 3/3 | 3/3 | 4/3 | 5/3 | 5/3 | 19/2 | 7/4 | 18/2 |
| *Bakmennene* | no | `insufficient_subject_text` | 12/5 | 12/5 | 14/5 | 13/5 | 14/5 | 9/2 | 20/11 | 20/5 |
| *Bob Dylan* | no | `insufficient_subject_text` | 20/15 | 20/15 | 20/14 | 20/12 | 19/12 | 20/2 | 20/15 | 19/11 |
| *Bundet av taushet* | no | `insufficient_subject_text` | 3/2 | 3/2 | 3/2 | 3/2 | 3/2 | 4/1 | 7/2 | 19/2 |
| *Evig fysen* | no | `title_mismatch` | 7/2 | 7/2 | 7/2 | 7/2 | 7/2 | 20/1 | 3/1 | 20/2 |
| *For mine sønner* | no | `title_mismatch` | 8/2 | 8/2 | 9/2 | 10/2 | 8/2 | 4/0 | 1/1 | 20/1 |
| *Fra sofa til ultraløp* | no | `title_mismatch` | 0/0 | 0/0 | 0/0 | 0/0 | 0/0 | 11/0 | 0/0 | 20/0 |
| *Husdraumar* | no | `title_mismatch` | 13/4 | 13/4 | 12/4 | 12/4 | 10/4 | 20/0 | 20/18 | 19/4 |
| *Hvordan bli økonomisk fri* | no | `author_mismatch` | 6/0 | 6/0 | 6/0 | 6/0 | 6/0 | 19/0 | 0/0 | 20/0 |
| *Kodenavn Jakob* | no | `title_mismatch` | 7/2 | 8/2 | 11/2 | 11/2 | 15/3 | 12/0 | 4/3 | 20/2 |
| *Lur hverdagsmat* | no | `title_mismatch` | 1/0 | 2/0 | 2/0 | 1/0 | 1/0 | 20/0 | 0/0 | 19/0 |
| *Smertefri* | no | `search_or_fetch_failure` | 18/11 | 20/17 | 20/15 | 18/11 | 15/5 | 20/0 | 20/12 | 20/0 |
| **Books with a hit** | | | 18 | 18 | 18 | 18 | 18 | 7 of 15 | 19 | 18 |
| **Hits in total** | | | 136 | 143 | 138 | 134 | 121 | 17 | 192 | 126 |

English controls, `results / hits`:

| Book | 2026-10-05 | current | no `book` |
|---|---|---:|---:|
| *A Great Book with Barack Obama* | placeholder | 20/9 | 20/8 |
| *Conversation* | placeholder | 19/18 | 19/14 |
| *Crush It!* | placeholder | 20/9 | 19/12 |
| *God, AI and the End of History* | placeholder | 18/16 | 18/16 |
| *Start with Why 15th Anniversary Edition* | placeholder | 17/17 | 18/18 |
| *Fictional Selves* | grounded | 20/14 | 20/9 |
| *Game Theory* | grounded | 11/8 | 11/8 |
| *Holding Lightning* | grounded | 20/14 | 19/13 |
| *How Bright the Path Grows* | grounded | 19/18 | 19/19 |
| *The Finest Hotel in Kabul* | grounded | 20/19 | 19/17 |

What the table shows:

- **The word `book` costs nothing.**
  No book gains its first hit when the word is removed, and the totals are 143 with it and 138 without.
  For the English controls the totals are 142 with it and 134 without.
- **The ISBN in the query costs nothing, and the ISBN alone is much worse.**
  Removing it leaves the same 18 books with a hit.
  The ISBN alone finds a page for 7 of the 15 books that have one, against 12 of the same 15 for the current query, and most of its results are unrelated catalogue pages.
- **Clean author names (#767) changed little here.**
  The sent and current columns differ for four books, and no book gains or loses its first hit.
- **Quotes are not the obstacle.**
  The unquoted query returns 18 to 20 results for every book and no more hits.

So the measurement backs no change to `book`, to the ISBN, or to the quoting.
The first query stays as it is.

## Result 2: for most of these books the search already finds the book

For 18 of the 22 books the current query returns a page that names the title, and for 17 of them it is the first result.
The pages are bookshops and publishers: ARK, Norli, Tronsmo, Gyldendal, Samlaget, Mollat, Lireka, Cúspide, Thalia, Hugendubel.
These candidates failed later, at the title, author, and identity gates (`title_mismatch`, `author_mismatch`, `source_identity_disagreement`, `insufficient_subject_text`), and no query variant addresses that.
The problem stated in the issue title does not hold for them.

It holds for four books:

- *Fra sofa til ultraløp*, *Hvordan bli økonomisk fri*, and *Lur hverdagsmat* have no hit in any variant, including the ISBN alone.
  The vendor's index does not contain a page for them, and no change to the query can reach one.
- *Wahre Verbrechen: Menschliche Abgründe* returns seven Amazon and Audible bestseller lists for the full title.

*Babymatboka* and *L’Année terrible des animaux* ended with `no_results` on 2026-10-05 and return results today for the query exactly as it was sent then.
The form of the query did not cause those two empty answers.

## Result 3: the main-title query leads with the publisher

Subtitled books, `results / hits`, with the hosts of the first three hits:

| Book | Lang | current | First hits | main title | First hits |
|---|---|---:|---|---:|---|
| *Wahre Verbrechen* | de | 7/0 | — | 19/14 | kulturkaufhaus.de, thalia.de, beck-shop.de |
| *Art Cure* | en | 18/14 | sbbresearch.org, goodreads.com, amazon.com | 18/18 | penguin.co.uk, theguardian.com, us.macmillan.com |
| *Artists, Siblings, Visionaries* | en | 18/14 | thenomadsalon.com, literaryreview.co.uk, us.amazon.com | 19/18 | panmacmillan.com, theguardian.com, goodreads.com |
| *Between the Lines* | en | 20/15 | amazon.com, bbk.ac.uk, play.google.com | 20/16 | simonandschuster.com, simonandschuster.co.uk, books.google.com |
| *Game Theory* | en | 11/8 | books.google.com, amazon.co.uk, amazon.com | 19/15 | us.amazon.com, waterstones.com, amazon.co.uk |
| *Karen* | en | 19/19 | amazon.com, goodreads.com, booktrib.com | 20/18 | amazon.com, barnesandnoble.com, harpercollinsfocus.com |
| *Nation of Strangers* | en | 18/18 | amazon.com, calirb.com, goodreads.com | 19/18 | simonandschuster.com, canongate.co.uk, amazon.com |
| *The Finest Hotel in Kabul* | en | 20/19 | goodreads.com, nytimes.com, amazon.com | 18/17 | penguin.co.uk, penguinrandomhouse.com, theguardian.com |
| *Ainsi pensait Michel Foucault* | fr | 16/9 | fabula.org, michel-foucault.com, scienceshumaines.com | 20/13 | scienceshumaines.com, editionsladecouverte.fr, fabula.org |
| *Chief Geopolitical Officer* | fr | 15/7 | lireka.com, va-editions.fr, halldulivre.com | 20/10 | eyrolles.com, halldulivre.com, lireka.com |
| *La sommation technologique* | fr | 17/9 | icigrandsboulevards.fr, cultura.com, librairie35.fr | 19/10 | lafabrique.fr, senscritique.com, icigrandsboulevards.fr |
| *L’Année terrible des animaux* | fr | 4/2 | up-magazine.info, bigshopper.se | 3/2 | up-magazine.info, gleeph.com |

- With the full title quoted, the publisher's page is among the first three hits for one of the twelve books (*Chief Geopolitical Officer*, VA Éditions).
- With only the main title quoted it is among the first three for eight others: Penguin twice, Pan Macmillan, Simon & Schuster twice, HarperCollins Focus, La Découverte, La Fabrique.
  For *Chief Geopolitical Officer* the publisher drops out of the first three.
- The number of hits rises for eight books, falls by one or two for two, and stays the same for two.
- The four French titles with a subtitle after a full stop support the separator added in #772: the main title never returns fewer hits than the whole string.

Two limits apply.
The *Wahre Verbrechen* hits for the main title are *stern Crime – Wahre Verbrechen*, a different publication, so a short main title with an organisation as author can drift.
And this is a search measurement only: whether a publisher page at the top produces a grounded description was not measured.

Today the main-title query is a fallback, issued only when the full-title query returns nothing (#708).
In this sample that condition was never met, so the fallback would not have run for any of the twelve books.
Sending the main-title query first, with the full title as the fallback, would cost no extra vendor request.
It would change the first query of every candidate with a subtitle, English included, and it reverses the order that #708 fixed in its tests.
That is a product-rule change and needs the operator's decision; it is not made here.

## Side observation: transient vendor errors

39 of the first 134 requests came back from the adapter as HTTP 502 at the production spacing of one request per second.
Every one of them succeeded when repeated with at least 2.5 seconds between requests.
The error body was not kept, so the vendor's reason is not known.
The 2026-10-05 run recorded 13 `search_or_fetch_failure` outcomes, and three of the 22 books here carry that reason; whether it is the same failure was not checked.
