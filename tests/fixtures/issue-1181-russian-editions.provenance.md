# Issue #1181 Russian-edition fixtures

Answers recorded on 2026-11-21 for twelve books (the slug starts each file name), from a plain client named `AutoScout Russian-edition check`, requests spaced at least a second apart.
Each file is trimmed to what the parsers read, and is metadata only: no description, annotation, review or sample.

- `<slug>.livelib-search.html`: `https://www.livelib.ru/find/books/<title>`. Each result block keeps its opening tag with the book path, its title link and its author link; an empty search keeps only the «ничего не найдено» line.
- `<slug>.wikidata-search-ru.json`: `wbsearchentities` for the English author name with `uselang=ru`, keeping per hit the id, the displayed label with its language (`ru` only when a Russian label exists) and the English name it matched.
- `<slug>.rsl-author.json`: the State Library's `ajax-search` answer to `author:(<Surname> <Given name>)`. Each result card keeps the title line, the author line, the bibliographic line and the record link.
- `<slug>.litres-author.json`: `https://api.litres.ru/foundation/api/search?q=<Cyrillic name>&limit=20&types=text_book`, keeping per entry the id, address, title, language code, credited persons and their roles, and the date fields.
- `atomic-habits.litres-art-detail.json`: `https://api.litres.ru/foundation/api/arts/<id>` for the Atomic Habits edition, keeping the id, address, title, language code, ISBN, the date fields and the rightholders' names.
- `livelib-ddos-guard.html`: the HTTP 403 page every LiveLib book page answered (all identical), unchanged.
- `livelib-ratelimit.html`: the page every LiveLib search was redirected to (`https://www.livelib.ru/service/ratelimitcaptcha`, HTTP 200) after 31 searches 3 seconds apart, recorded on 2026-11-21; trimmed to its title, its two headings and its captcha form, without the counters and scripts.
- `wikidata-rate-limited.txt`: the HTTP 429 body Wikidata answered for the last two books, unchanged.

The two untranslated books (`24-hours-capitol`, `augustine-african`) have no State Library or LitRes files: Wikidata gives neither author a Russian label, so no Cyrillic name is known.
