# Issue #866: authorless silent-source replay

## Inputs and method

- Historical stage: `git show 8bf8f09:runs/current-weekly-pilot-russian/grounded-description-stage.json`, dated 2026-10-26. Select all 20 outcomes whose final reason is `source_identity_disagreement`; unlike #755, do not filter by candidate ISBN.
- Baseline: `498074d7677d5b9559f4450bff0a03ee48192042` (the merge of #755 into main). Read titles, authors, and candidate ISBNs from its `runs/current-weekly-pilot-russian/latest-scouting-candidates.json`. The Demon-Haunted World is absent from both latest lists; resolve its exact durable ID `candidate-73ed2701fcfcf67b7dca8a7b` through `8bf8f09`'s `candidate-history.json` (Carl Sagan, no ISBN). No metadata was guessed from its URLs.
- Re-fetch all retained rejected URLs except historical `source_outside_policy` entries. Use production `SourceClassifier(owner_policy=load_owner_policy("configs/source-owner-policy.json"))` and `BoundedHTTPFetcher`, with default response-byte, timeout, redirect, challenge-retry, and host-spacing limits. Four threads share the fetcher and its host limiter.
- Compare the baseline module loaded from `git show <baseline>:autoscout/grounded_description.py` against the #866 gate. Both receive identical fetched fields, including in-memory visible identity text and URL ISBN proof, reconstructed with each module's `FetchedSource` constructor. Fetching/parsing is unchanged by #866, so there is no separate baseline network request. Call each module's `evaluate_grounding_gate` once per book on all its successfully fetched retained pages, not just identity-positive pages.
- Record individual identity evidence and the final gate, including the accepted URL set. The per-URL table distinguishes passing identity wording from positive author/ISBN evidence: a title-only match can pass `_identity_reason` yet still have no positive evidence.
- No discovery, synthesis, model call, weekly workflow, or canonical state mutation. Temporary fetched fields stayed under Git-ignored `tmp/issue-866-replay/`; no file under `runs/` changed. These are grounding-eligibility measurements on live pages, not generated descriptions or an exact reproduction of historical HTTP responses.

Live replay: 2026-10-27T16:04:01.232361+00:00 to 2026-10-27T16:04:20.234326+00:00.

The retained fetched fields were re-evaluated offline after review (no additional network requests): headline evidence now requires the target author's full name, and author names in authorless prose no longer count as conflicts. Book-level counts are unchanged; Smart Mouth's accepted set dropped the SmartyPants page (see below).

## Result

All **53 URLs fetched successfully** across all **20 historical disagreements**. On those identical responses, the baseline accepts **6 books**, and #866 accepts **11**. Thus **5 of the 20 become newly eligible because of #866**. None lose eligibility. The other 6 eligible books already pass current main with #755; they must not be attributed to #866. No descriptions were generated.

The five changed outcomes are Poétique de Tom Cruise, Smart Mouth, The Coming Wave, L’âme noire de la démocratie, and A marcha da irrelevância. Four gain headline author evidence; L’âme noire de la démocratie and Smart Mouth exercise the one-evidenced-source rule, accepting only the evidenced page and excluding silent pages. Eight books still fail with `source_identity_disagreement`, and one still fails with `subject_content_disagreement`.

### Limits and evidence caveats

- Headline evidence requires the target author's full name, with the existing punctuation fold, word boundaries, and accent sensitivity. A surname alone is not enough because a headline repeats the book title: the authorless SmartyPants page titled “Smart Mouth” is a different book and is not evidence for Jean Smart’s memoir. It is now silent and excluded; Smart Mouth is grounded on the People page alone. The eligibility count is a mechanical result, not a claim that all 11 books have editorially verified source sets.
- Heróis por acaso remains refused: the Globo headlines now supply author evidence, but a retained Bafafá page still has conflicting extracted-author metadata. Headline evidence does not override a different extracted author. The deterministic regression test separately proves headline-only author evidence and silent-peer exclusion.
- A different author counts as a conflict only in extracted author metadata. Names in authorless prose are not authorship claims: reviewer and foreword mentions (“Publishers Weekly wrote…”, “Bill Bryson wrote the foreword”) made a prose heuristic refuse pages that main accepts, so such pages are silent and simply not used. The two #485 different-author subject tests were updated accordingly (see the PR’s product-rule section).
- A matching checked ISBN retains #755’s proof semantics; a different checked ISBN without target-author evidence still conflicts. The identical-subject `legacy_subject_corroboration` admission path was not changed. This replay adds no source-admission exception.
- Live page contents, current owner derivation, and #755 already differ from the historical run. Only the old-versus-new gate comparison on these same responses isolates #866.

## Per-book report

Every row had the historical final reason `source_identity_disagreement`. “Changed” means the current-main gate refuses and the #866 gate accepts on the same fetched pages.

| Book | Fetched URLs | Baseline gate | #866 gate | Changed |
|---|---:|---|---|---|
| The Color of Law: A Forgotten History of How Our Government Segregated America | 3 | eligible (2 source(s)) | eligible (2 source(s)) | no |
| A History of the World in Twelve Beans | 3 | eligible (2 source(s)) | eligible (2 source(s)) | no |
| The Great Cosmic Mother: Rediscovering the Religion of the Earth | 2 | `source_identity_disagreement` | `source_identity_disagreement` | no |
| Heróis por acaso | 3 | `source_identity_disagreement` | `source_identity_disagreement` | no |
| Poétique de Tom Cruise | 2 | `source_identity_disagreement` | eligible (2 source(s)) | yes |
| Smart Mouth | 2 | `source_identity_disagreement` | eligible (1 source(s)) | yes |
| Digital Products Business Manual | 3 | `source_identity_disagreement` | `source_identity_disagreement` | no |
| A Brief History of Cities in 32 Inventions: The Ideas and Technologies That Made Modernity Possible | 2 | `subject_content_disagreement` | `subject_content_disagreement` | no |
| The Coming Wave | 3 | `source_identity_disagreement` | eligible (2 source(s)) | yes |
| The Demon-Haunted World | 3 | `source_identity_disagreement` | `source_identity_disagreement` | no |
| Bogland: The Secret World That Defies Death and Protects Life | 3 | eligible (2 source(s)) | eligible (2 source(s)) | no |
| Looking for Terry: A Memoir | 2 | `source_identity_disagreement` | `source_identity_disagreement` | no |
| Biblioteca estoica - Grandes mestres - Box com 4 livros | 3 | `source_identity_disagreement` | `source_identity_disagreement` | no |
| Mastering American Mahjong: A Complete Guide to Rules and Strategy | 3 | eligible (1 source(s)) | eligible (1 source(s)) | no |
| La guerre du Pacifique | 3 | `source_identity_disagreement` | `source_identity_disagreement` | no |
| L'âme noire de la démocratie. Manifeste pour un autre idéal politique | 3 | `source_identity_disagreement` | eligible (1 source(s)) | yes |
| Como se tornar sobrenatural | 3 | eligible (3 source(s)) | eligible (3 source(s)) | no |
| A marcha da irrelevância | 3 | `source_identity_disagreement` | eligible (2 source(s)) | yes |
| The Sound of Purpose | 2 | `source_identity_disagreement` | `source_identity_disagreement` | no |
| OTHELLO | 2 | eligible (2 source(s)) | eligible (2 source(s)) | no |

## Newly eligible accepted sources

### Poétique de Tom Cruise

- [Poétique de Tom Cruise, d’Olivier Maillart](https://www.marestediteur.com/produit/poetique-de-tom-cruise-dolivier-maillart/); extracted authors: none; baseline: no positive identity evidence; #866: author evidence.
- [Poétique de Tom Cruise](https://www.mollat.com/livres/3050555/olivier-maillart-poetique-de-tom-cruise); extracted authors: Olivier Maillart; baseline: author evidence; #866: author evidence.

### Smart Mouth

- [‘Hacks’ Star Jean Smart Announces Revealing New Memoir ‘Smart Mouth’ (Exclusive)](https://people.com/jean-smart-smart-mouth-cover-reveal-exclusive-12185999); extracted authors: none; baseline: author evidence; #866: author evidence.
- [Smart Mouth](https://smartypantsromance.com/books/smart-mouth/); extracted authors: none; baseline: no positive identity evidence; #866: silent, not accepted (different book; surname-only title collision).

### The Coming Wave

- [The Coming Wave](https://www.penguinrandomhouse.com/books/722674/the-coming-wave-by-mustafa-suleyman-with-michael-bhaskar/); extracted authors: Mustafa Suleyman; baseline: author evidence; #866: author evidence.
- [The Coming Wave by Mustafa Suleyman review](https://www.theguardian.com/books/2023/sep/08/the-coming-wave-by-mustafa-suleyman-review-a-tech-tsunami); extracted authors: none; baseline: no positive identity evidence; #866: author evidence.

### L'âme noire de la démocratie. Manifeste pour un autre idéal politique

- [L'âme noire de la démocratie : manifeste pour un autre idéal politique](https://www.mollat.com/livres/3414852/geoffroy-de-lagasnerie-l-ame-noire-de-la-democratie-manifeste-pour-un-autre-ideal-politique); extracted authors: Geoffroy de Lagasnerie; baseline: author evidence; #866: author evidence.

### A marcha da irrelevância

- [A Marcha da Irrelevância: Um Livro Sobre Sobrevivência](https://news.entrelinhas.ai/p/a-marcha-da-irrelevancia-um-livro); extracted authors: Junior Borneli; baseline: author evidence; #866: author evidence.
- [A marcha da irrelevância: O sistema operacional das empresas que se recusam a desaparecer de Junior Borneli, (Livre numérique Kobo)](https://www.indigo.ca/fr/products/a-marcha-da-irrelevancia-o-sistema-operacional-das-empresas-que-se-recusam-a-desaparecer?variant=47204507091154); extracted authors: none; baseline: no positive identity evidence; #866: author evidence.

## Per-URL identity evidence

The full gate also enforces subject, policy, independence, and selection checks; positive identity evidence alone does not guarantee admission. URLs historically excluded by policy were never fetched and are omitted here.

| Book | Re-fetched URL | Baseline identity | #866 identity | Used by #866 |
|---|---|---|---|---|
| The Color of Law: A Forgotten History of How Our Government Segregated America | [https://wwnorton.com/books/the-color-of-law/](https://wwnorton.com/books/the-color-of-law/) | no positive identity evidence | no positive identity evidence | no |
| The Color of Law: A Forgotten History of How Our Government Segregated America | [https://pastforward.org/products/the-color-of-law-a-forgotten-history-of-how-our-government-segregated-america-9781631494536](https://pastforward.org/products/the-color-of-law-a-forgotten-history-of-how-our-government-segregated-america-9781631494536) | ISBN proof | ISBN proof | yes |
| The Color of Law: A Forgotten History of How Our Government Segregated America | [https://www.amazon.com/Color-Law-Forgotten-Government-Segregated/dp/1631494538](https://www.amazon.com/Color-Law-Forgotten-Government-Segregated/dp/1631494538) | ISBN proof | ISBN proof | yes |
| A History of the World in Twelve Beans | [https://usa.kinokuniya.com/products/9781778403545](https://usa.kinokuniya.com/products/9781778403545) | ISBN proof | ISBN proof | yes |
| A History of the World in Twelve Beans | [https://www.lindentreebooks.com/a-history-of-the-world-in-twelve-beans.html](https://www.lindentreebooks.com/a-history-of-the-world-in-twelve-beans.html) | no positive identity evidence | no positive identity evidence | no |
| A History of the World in Twelve Beans | [https://everyonesbooks.com/item/AS48k97jmWNs34mElc91nQ](https://everyonesbooks.com/item/AS48k97jmWNs34mElc91nQ) | author evidence | author evidence | yes |
| The Great Cosmic Mother: Rediscovering the Religion of the Earth | [https://www.abebooks.com/9780062507914/Great-Cosmic-Mother-Rediscovering-Religion-0062507915/plp](https://www.abebooks.com/9780062507914/Great-Cosmic-Mother-Rediscovering-Religion-0062507915/plp) | author_mismatch | author_mismatch | no |
| The Great Cosmic Mother: Rediscovering the Religion of the Earth | [https://books.google.com/books/about/The_Great_Cosmic_Mother.html?id=KAseAgAAQBAJ](https://books.google.com/books/about/The_Great_Cosmic_Mother.html?id=KAseAgAAQBAJ) | no positive identity evidence | no positive identity evidence | no |
| Heróis por acaso | [https://oglobo.globo.com/politica/noticia/2026/09/21/herois-por-acaso-octavio-guedes-lanca-livro-que-remonta-historia-de-personagens-improvaveis-que-mudaram-os-rumos-do-brasil.ghtml](https://oglobo.globo.com/politica/noticia/2026/09/21/herois-por-acaso-octavio-guedes-lanca-livro-que-remonta-historia-de-personagens-improvaveis-que-mudaram-os-rumos-do-brasil.ghtml) | no positive identity evidence | author evidence | no |
| Heróis por acaso | [https://extra.globo.com/rio/noticia/2026/09/novo-livro-de-octavio-guedes-traz-herois-por-acaso-do-brasil.ghtml](https://extra.globo.com/rio/noticia/2026/09/novo-livro-de-octavio-guedes-traz-herois-por-acaso-do-brasil.ghtml) | no positive identity evidence | author evidence | no |
| Heróis por acaso | [https://bafafa.com.br/arte-e-cultura/letras-e-livros/octavio-guedes-lanca-herois-por-acaso-livro-sobre-personagens-improvaveis-da-historia-do-brasil](https://bafafa.com.br/arte-e-cultura/letras-e-livros/octavio-guedes-lanca-herois-por-acaso-livro-sobre-personagens-improvaveis-da-historia-do-brasil) | author_mismatch | author_mismatch | no |
| Poétique de Tom Cruise | [https://www.marestediteur.com/produit/poetique-de-tom-cruise-dolivier-maillart/](https://www.marestediteur.com/produit/poetique-de-tom-cruise-dolivier-maillart/) | no positive identity evidence | author evidence | yes |
| Poétique de Tom Cruise | [https://www.mollat.com/livres/3050555/olivier-maillart-poetique-de-tom-cruise](https://www.mollat.com/livres/3050555/olivier-maillart-poetique-de-tom-cruise) | author evidence | author evidence | yes |
| Smart Mouth | [https://people.com/jean-smart-smart-mouth-cover-reveal-exclusive-12185999](https://people.com/jean-smart-smart-mouth-cover-reveal-exclusive-12185999) | author evidence | author evidence | yes |
| Smart Mouth | [https://smartypantsromance.com/books/smart-mouth/](https://smartypantsromance.com/books/smart-mouth/) | no positive identity evidence | no positive identity evidence | no |
| Digital Products Business Manual | [https://www.amazon.co.uk/DIGITAL-PRODUCTS-BUSINESS-MANUAL-sustainable-ebook/dp/B0FNBTZ19Z](https://www.amazon.co.uk/DIGITAL-PRODUCTS-BUSINESS-MANUAL-sustainable-ebook/dp/B0FNBTZ19Z) | no positive identity evidence | author evidence | no |
| Digital Products Business Manual | [https://sidebusinesspro.com/the-complete-digital-products-business-manual/](https://sidebusinesspro.com/the-complete-digital-products-business-manual/) | author_mismatch | author_mismatch | no |
| Digital Products Business Manual | [https://us.amazon.com/DIGITAL-PRODUCTS-BUSINESS-MANUAL-sustainable-ebook/dp/B0FNBTZ19Z](https://us.amazon.com/DIGITAL-PRODUCTS-BUSINESS-MANUAL-sustainable-ebook/dp/B0FNBTZ19Z) | no positive identity evidence | no positive identity evidence | no |
| A Brief History of Cities in 32 Inventions: The Ideas and Technologies That Made Modernity Possible | [https://www.lindentreebooks.com/a-brief-history-of-cities-in-32-inventions-the-ide.html](https://www.lindentreebooks.com/a-brief-history-of-cities-in-32-inventions-the-ide.html) | ISBN proof | ISBN proof | no |
| A Brief History of Cities in 32 Inventions: The Ideas and Technologies That Made Modernity Possible | [https://www.amazon.com/Brief-History-Cities-Inventions-Technologies/dp/B0GKR72MGD](https://www.amazon.com/Brief-History-Cities-Inventions-Technologies/dp/B0GKR72MGD) | ISBN proof | ISBN proof | no |
| The Coming Wave | [https://www.penguinrandomhouse.com/books/722674/the-coming-wave-by-mustafa-suleyman-with-michael-bhaskar/](https://www.penguinrandomhouse.com/books/722674/the-coming-wave-by-mustafa-suleyman-with-michael-bhaskar/) | author evidence | author evidence | yes |
| The Coming Wave | [https://www.theguardian.com/books/2023/sep/08/the-coming-wave-by-mustafa-suleyman-review-a-tech-tsunami](https://www.theguardian.com/books/2023/sep/08/the-coming-wave-by-mustafa-suleyman-review-a-tech-tsunami) | no positive identity evidence | author evidence | yes |
| The Coming Wave | [https://ig.ft.com/sites/business-book-award/books/2023/shortlist/the-coming-wave-by-mustafa-suleyman-and-michael-bhaskar/](https://ig.ft.com/sites/business-book-award/books/2023/shortlist/the-coming-wave-by-mustafa-suleyman-and-michael-bhaskar/) | author_mismatch | author_mismatch | no |
| The Demon-Haunted World | [https://www.penguin.com.au/books/the-demon-haunted-world-9780345409461](https://www.penguin.com.au/books/the-demon-haunted-world-9780345409461) | author evidence | author evidence | no |
| The Demon-Haunted World | [https://www.penguinrandomhouse.com/books/159731/the-demon-haunted-world-by-carl-sagan/](https://www.penguinrandomhouse.com/books/159731/the-demon-haunted-world-by-carl-sagan/) | author_mismatch | author_mismatch | no |
| The Demon-Haunted World | [https://www.amazon.com/Demon-Haunted-World-Science-Candle-Dark/dp/0345409469](https://www.amazon.com/Demon-Haunted-World-Science-Candle-Dark/dp/0345409469) | no positive identity evidence | author evidence | no |
| Bogland: The Secret World That Defies Death and Protects Life | [https://www.publishersweekly.com/9781324086536](https://www.publishersweekly.com/9781324086536) | ISBN proof | ISBN proof | yes |
| Bogland: The Secret World That Defies Death and Protects Life | [https://www.amazon.com/Bogland-Secret-World-Defies-Protects/dp/132408653X](https://www.amazon.com/Bogland-Secret-World-Defies-Protects/dp/132408653X) | ISBN proof | ISBN proof | yes |
| Bogland: The Secret World That Defies Death and Protects Life | [https://www.indigo.ca/products/bogland-the-secret-world-that-defies-death-and-protects-life](https://www.indigo.ca/products/bogland-the-secret-world-that-defies-death-and-protects-life) | ISBN proof | ISBN proof | no |
| Looking for Terry: A Memoir | [https://premierecollectibles.com/terrybradshaw](https://premierecollectibles.com/terrybradshaw) | ISBN proof | ISBN proof | no |
| Looking for Terry: A Memoir | [https://www.harpercollinsfocus.com/blog/2026/06/22/terry-bradshaw-memoir-looking-for-terry-2026/](https://www.harpercollinsfocus.com/blog/2026/06/22/terry-bradshaw-memoir-looking-for-terry-2026/) | author_mismatch | author_mismatch | no |
| Biblioteca estoica - Grandes mestres - Box com 4 livros | [https://www.amazon.com.br/Biblioteca-Estoica-Grandes-Mestres-livros-ebook/dp/B0C15MZNLQ](https://www.amazon.com.br/Biblioteca-Estoica-Grandes-Mestres-livros-ebook/dp/B0C15MZNLQ) | no positive identity evidence | no positive identity evidence | no |
| Biblioteca estoica - Grandes mestres - Box com 4 livros | [https://www.amazon.es/Biblioteca-Estoica-Grandes-Mestres-Portuguese-ebook/dp/B0C15MZNLQ](https://www.amazon.es/Biblioteca-Estoica-Grandes-Mestres-Portuguese-ebook/dp/B0C15MZNLQ) | no positive identity evidence | no positive identity evidence | no |
| Biblioteca estoica - Grandes mestres - Box com 4 livros | [https://www.amazon.com.mx/Biblioteca-Estoica-Grandes-Mestres-Portuguese-ebook/dp/B0C15MZNLQ](https://www.amazon.com.mx/Biblioteca-Estoica-Grandes-Mestres-Portuguese-ebook/dp/B0C15MZNLQ) | no positive identity evidence | no positive identity evidence | no |
| Mastering American Mahjong: A Complete Guide to Rules and Strategy | [https://www.amazon.com/Mastering-American-Mahjong-Complete-Strategy-ebook/dp/B0GGJF6MKS](https://www.amazon.com/Mastering-American-Mahjong-Complete-Strategy-ebook/dp/B0GGJF6MKS) | no positive identity evidence | no positive identity evidence | no |
| Mastering American Mahjong: A Complete Guide to Rules and Strategy | [https://us.amazon.com/Mastering-American-Mahjong-Complete-Strategy/dp/B0GGPBK7DM](https://us.amazon.com/Mastering-American-Mahjong-Complete-Strategy/dp/B0GGPBK7DM) | ISBN proof | ISBN proof | yes |
| Mastering American Mahjong: A Complete Guide to Rules and Strategy | [https://www.abebooks.com/products/isbn/9798217274161?ref_=pd_offer-1-v](https://www.abebooks.com/products/isbn/9798217274161?ref_=pd_offer-1-v) | ISBN proof | ISBN proof | no |
| La guerre du Pacifique | [https://www.glenat.com/glenat-livres/la-guerre-du-pacifique-9782344070543/](https://www.glenat.com/glenat-livres/la-guerre-du-pacifique-9782344070543/) | no positive identity evidence | no positive identity evidence | no |
| La guerre du Pacifique | [https://www.mollat.com/livres/3360137/frederic-guelton-la-guerre-du-pacifique](https://www.mollat.com/livres/3360137/frederic-guelton-la-guerre-du-pacifique) | author evidence | author evidence | no |
| La guerre du Pacifique | [https://123savoie.com/la-guerre-du-pacifique-par-frederic-guelton/](https://123savoie.com/la-guerre-du-pacifique-par-frederic-guelton/) | author_mismatch | author_mismatch | no |
| L'âme noire de la démocratie. Manifeste pour un autre idéal politique | [https://www.mollat.com/livres/3414852/geoffroy-de-lagasnerie-l-ame-noire-de-la-democratie-manifeste-pour-un-autre-ideal-politique](https://www.mollat.com/livres/3414852/geoffroy-de-lagasnerie-l-ame-noire-de-la-democratie-manifeste-pour-un-autre-ideal-politique) | author evidence | author evidence | yes |
| L'âme noire de la démocratie. Manifeste pour un autre idéal politique | [https://geoffroydelagasnerie.com/livres-2/lame-noire-de-la-democratie-manifeste-pour-un-autre-ideal-politique/](https://geoffroydelagasnerie.com/livres-2/lame-noire-de-la-democratie-manifeste-pour-un-autre-ideal-politique/) | no positive identity evidence | no positive identity evidence | no |
| L'âme noire de la démocratie. Manifeste pour un autre idéal politique | [https://geoffroydelagasnerie.com/2026/01/14/lame-noire-de-la-democratie-manifeste-pour-un-autre-ideal-politique-a-paraitre-le-25-fevrier-2026/](https://geoffroydelagasnerie.com/2026/01/14/lame-noire-de-la-democratie-manifeste-pour-un-autre-ideal-politique-a-paraitre-le-25-fevrier-2026/) | no positive identity evidence | no positive identity evidence | no |
| Como se tornar sobrenatural | [https://www.agapea.com/Dr-Joe-Dizpenza/Como-se-Tornar-Sobrenatural-9786550470517-i.htm](https://www.agapea.com/Dr-Joe-Dizpenza/Como-se-Tornar-Sobrenatural-9786550470517-i.htm) | ISBN proof | ISBN proof | yes |
| Como se tornar sobrenatural | [https://books.google.com/books/about/Como_Se_Tornar_Sobrenatural.html?id=yqfMzQEACAAJ](https://books.google.com/books/about/Como_Se_Tornar_Sobrenatural.html?id=yqfMzQEACAAJ) | ISBN proof | ISBN proof | yes |
| Como se tornar sobrenatural | [https://www.worldofbooks.com/products/como-se-tornar-sobrenatural-book-dispenza-9786550470517](https://www.worldofbooks.com/products/como-se-tornar-sobrenatural-book-dispenza-9786550470517) | ISBN proof | ISBN proof | yes |
| A marcha da irrelevância | [https://www.livrariascuritiba.com.br/a-marcha-da-irrelevancia-lv558331/p](https://www.livrariascuritiba.com.br/a-marcha-da-irrelevancia-lv558331/p) | ISBN proof | ISBN proof | no |
| A marcha da irrelevância | [https://www.indigo.ca/fr/products/a-marcha-da-irrelevancia-o-sistema-operacional-das-empresas-que-se-recusam-a-desaparecer?variant=47204507091154](https://www.indigo.ca/fr/products/a-marcha-da-irrelevancia-o-sistema-operacional-das-empresas-que-se-recusam-a-desaparecer?variant=47204507091154) | no positive identity evidence | author evidence | yes |
| A marcha da irrelevância | [https://news.entrelinhas.ai/p/a-marcha-da-irrelevancia-um-livro](https://news.entrelinhas.ai/p/a-marcha-da-irrelevancia-um-livro) | author evidence | author evidence | yes |
| The Sound of Purpose | [https://www.complex.com/pop-culture/a/bernadette-giacomazzo/nia-long-sound-purpose-memoir](https://www.complex.com/pop-culture/a/bernadette-giacomazzo/nia-long-sound-purpose-memoir) | author_mismatch | author_mismatch | no |
| The Sound of Purpose | [https://people.com/nia-long-the-sound-of-purpose-cover-reveal-exclusive-12185089](https://people.com/nia-long-the-sound-of-purpose-cover-reveal-exclusive-12185089) | author evidence | author evidence | no |
| OTHELLO | [https://www.amazon.com/Othello-American-Tragedy-Black-Lives/dp/0300267703](https://www.amazon.com/Othello-American-Tragedy-Black-Lives/dp/0300267703) | ISBN proof | ISBN proof | yes |
| OTHELLO | [https://yalebooks.yale.edu/book/9780300267709/othello/](https://yalebooks.yale.edu/book/9780300267709/othello/) | ISBN proof | ISBN proof | yes |
