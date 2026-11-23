# Pre-collection passes and field completion

Part of the [operator workflow](../current-weekly-operator-workflow.md).

## Kirkus Pre-Collection Step (#816)

Kirkus asks for 20 seconds between requests, so its coming-soon nonfiction listing is collected by its own pass and not inside the capture stage.

`./scripts/run-weekly` starts the pass itself after the toolchain check, alongside capture and extraction of the other sources, so nobody has to remember it.
The run announces it as «Собираю данные Kirkus, это занимает до получаса...» and adds `Сбор Kirkus: запрошено страниц N.` to the current stage's counts in one shared heartbeat line, at most once per 60 seconds overall.
After the main extraction, any unfinished pass is reported as a wait with the Kirkus page count; the run then captures and extracts Kirkus before continuing.
During the overlap, `./scripts/doctor` reports both stages with their counts, elapsed time against their budgets when available, and last snapshot time as `run-in-progress`, with no escalation question.
The run makes at most one pass a day: when a pass was already attempted on the run date, whether it finished, was cut short or failed, it makes no request.
Each pass records its date and outcome in a small file beside the result, so a pass that failed and wrote no result still counts as the pass of its day.
The pass counts toward the weekly operation ceiling and may spend at most 40 minutes (`KIRKUS_PASS_RUNTIME_BUDGET_SECONDS`); at that allowance it stops, keeps the pages it opened, and the next pass continues.
No outcome of the pass stops the run.
When the pass of the run date fails and an earlier result is still inside the age limit, the run reports the books of that earlier result and names Kirkus as an incomplete source, with the date of the result it used.
A run resumed with its capture already current starts no pass.

The same pass can be run by hand:

```bash
./scripts/collect-kirkus
```

Use it to refresh Kirkus outside a weekly run, on the machine that runs the weekly scan.
One pass takes up to about half an hour: at most 40 listing requests and at most the declared number of review pages, 20 seconds apart.

The step writes one result file under `data/kirkus-precollection/`, which Git ignores, and prints counts only: listing pages, listed rows, review pages opened, rows waiting for a later pass, and rejected rows by reason.
It exits 0 when a result was written and 2 when the pass failed; a failed pass leaves the earlier result in place.
A status of `partial` means the site refused a review page and the pass stopped there; run the step again later, not at once.
The first passes of a new installation leave rows waiting, because a pass opens a bounded number of review pages; each later pass opens more.

The capture stage reads the result and makes no request to Kirkus.
When the result is missing, older than the declared age limit, or from a pass the site cut short before it reached any new book, the run reports Kirkus as a failed source by name and continues with every other source.

The result is a machine-local file and not weekly state: the run's repository check refuses any untracked or modified file, so the result lives in an ignored directory.
It is therefore never committed, and a machine without it collects it in its first weekly run.
Losing the file costs only a repeated pass.

The step does not touch Candidate History, the seen-item index, configuration, or Git.
The contract is in `docs/source-onboarding-runbook.md`, "Explicit Kirkus coming-soon listing contract".

## ISBN Completion Step (#979)

After the pipeline and before the description stages, the weekly run fills missing ISBNs of this run's candidates.
It tries, in order: the book link the source gave (Amazon `/dp/`, bookshop.org, publisher pages), the captured list page, the book's own page behind that link, and an Open Library edition in the source language.
A list page counts only when one layout (ISBN after or before each title) gives at least half of its titles exactly one ISBN.
A book page counts only when it shows the book's title, and a page with two different ISBNs of the same kind gives none.
Audiobook sources skip the page methods, because their ISBNs belong to the audio edition.

Every learned ISBN is fill-only and discovery-only: it never replaces an ISBN and never merges two books.
Each one records `isbn_provenance` (method, URL, date) in Candidate History and is copied onto the current and latest candidate snapshots.
A book whose network lookup found nothing records `isbn_lookup_date` and is asked again after 28 days; when Open Library was unreachable or answered garbage, nothing is recorded and the book is asked again on the next run.
The stage spends at most 10 minutes on network lookups, reports through the shared heartbeat as `Поиск ISBN`, writes `isbn-completion.json`, and no outcome of it stops the run.

The review shows an exact-edition BookFinder link for a book with an ISBN and a title search for the rest (#978).
To fill ISBNs for books that already wait for a decision, run `./scripts/complete-isbns` (preview) and then `./scripts/complete-isbns --apply`.
It refuses to write while a weekly run is in progress.

## Original Edition Completion Step (#968)

A book found only on a non-English list is often a translation: «Ahora que se acerca el final» by Diana Athill is *Somewhere Towards the End*.
The `complete-originals` pass records the original edition per book in Candidate History: a model proposal that Open Library confirms, with a translator credit as extra evidence.

### National libraries first (#1139, #1147)

A book with an ISBN found on a German list is first looked up in the German National Library (DNB, SRU, MARC 21), one found on a French list in the French National Library (BnF, SRU, UNIMARC), and one with a Spanish ISBN (978-84, 979-13) found on a Spanish list in the Spanish National Library (BNE, SRU, MARC 21); all three are free and keyless.
The DNB is paused (#1145): since 2026-11-18 it has refused every request (TLS refused, then HTTP 403 and 429, even for its robots file), although its rules allow keyless use, so the pass does not ask it and German-list books go on to the model; it resumes once `dnb` is removed from `PAUSED_LIBRARY_KEYS`.
The BNE's robots file asks for ten seconds between requests, so the pass spaces BNE requests that far apart; a wait that would cross the stage budget stops the pass like any other lookup.
The BNE often returns two records for one ISBN, the e-book and the print edition; the print record usually carries field 041.
A record that states a translation with its original language answers the book without a model call.
An original title stated in the record goes through the Open Library confirmation below; when Open Library confirms it, the original is `confirmed` with the library record as translation evidence.
When Open Library does not confirm it, or the record names only the original language, the book is stored as `library_stated`: `is_translation: yes`, the original language and, when stated, the original title, with the library record as evidence.
A BnF record that names an intermediate language (101 `$b`) gives only the original language, because its 454 title may be the intermediate edition's.
A BNE record states a translation through 041 (indicator 1 `1` or `$h`) or through a uniform title in 240 with a language subfield `$l`; the 240 title is the original title. A 240 without `$l` is the uniform title of a work in its own language (Cien años de soledad has one) and states nothing. A record that states a translation only through 240 `$l` names no original language, so the language stays empty and the record goes to the model as a credit with the stated original title.
A record silent about translation, no record, or an unreachable library is no answer, and the book goes on to the model as before; a record silent about translation never means "not a translation".
A record that states a translation without a usable language counts as a translator credit for the model.
A library that fails three times in a row is not asked again in the same pass; its failures are counted for that library alone in `library_unavailable`.
The record's page count is stored as `page_count` with `page_count_source_url` when the book has none.
The preview prints `answered_by_library` and `answered_by_model`.

The library modules live in `autoscout/library_catalogue.py`; another library is one more `LibraryCatalogue` entry in `ALL_LIBRARY_CATALOGUES`, the pass asks those not named in `PAUSED_LIBRARY_KEYS`, whose `isbn_prefixes` and `min_request_interval_seconds` set the ISBN range it is asked about and its request spacing.

### The booksellers' portal (#1140)

A Spanish-list book with an ISBN that no library answered with an original language is looked up on todostuslibros.com, under CEGAL's written permission of 2026-11-19.
The permission, its conditions, the ceilings, the ledger and the first load are in `docs/operator/bookseller-portal.md`.

### The model step

It reads the first three evidence pages already used for the book's description and looks for a translator credit; the credit is extra evidence, not a gate.
Every selected book no library answered is asked: a Codex CLI call proposes the original title and its ISO 639-1 language for each one, in bounded batches of 10, with a 120-second call timeout and one retry for a stalled call.
Open Library must then confirm the proposal: only a confirmed original is stored as `original_edition`.
A confirmed original in another language proves the translation (`is_translation: yes`), with the credit page as evidence when there is one and the Open Library page otherwise.
A book with no credit and no confirmed original stays `is_translation: unknown`.
An unconfirmed proposal is stored as `not_found`; a failed proposal call or an unreachable catalogue leaves the record untouched for the next run.

A confirmed English original also sets the sourced published English title, through the existing title-evidence guard, and never replaces an English title already proven that way.
A book whose original is confirmed, or whose lookup is younger than 28 days, is not asked again.
The pass spends at most 15 minutes and writes `original-edition-completion.json`.
`./scripts/run-weekly` runs it itself after the description and PM logline stages and before the refresh, for this run's candidates, within the same 15-minute budget. It reports through the shared heartbeat as `Поиск оригинальных изданий`.
No outcome of it stops the weekly run. Its counts, including a failure or `budget_exhausted`, go to `workflow-log.json` and `original-edition-completion.json`, like the ISBN step's; the editor is not told, because a book without a confirmed original keeps its machine-translated English title.

To record original editions for books already collected, run `./scripts/complete-originals` (preview) and then `./scripts/complete-originals --apply`.
It refuses to write while a weekly run is in progress.
An unconfirmed proposal is never stored as the original; only a library record's or the portal page's own statement is stored without Open Library (`library_stated`).

## Work Age Completion Step (#1138)

AutoScout holds the year of the listed edition only, so *Sapiens* or *Atomic Habits* found on a Spanish list dated 2026 reads as a new book.
The `complete-work-age` pass records, per book in Candidate History, how old the underlying work is: the year it was first published anywhere and how many editions Open Library knows, in one `work_age` block with its status, its basis and the Open Library work page.

A book with a confirmed original edition is looked up by the original's title, so a translation takes the age of its original.
Any other book is looked up by its ISBN when it has one, and then, or without an ISBN, by its title and author; this applies to books on English lists too.
A work must agree on title and author through the strict match the original-edition confirmation uses: the whole main title (the work's or the matched edition's) and the author's first given name and surname.
A doubtful match is stored as `not_found` with the year and edition count unknown, never as a guess: a book called «Castles» is not *Castles made of sand*.
`not_found` means the catalogue has no agreeing work, which is usual for a new book; a book never looked up, or whose lookup failed, has no `work_age` and counts as not checked.
A `not_found` book is asked again after 28 days; a found work is not asked again, unless an original edition was confirmed after it was looked up by the listed edition.
Several agreeing works (Open Library sometimes holds a work twice) give the earliest year and the sum of their editions.
No model is called. Requests to Open Library are spaced at least 2 seconds apart, within its guidance of one request per second for clients that give no contact details.

`./scripts/run-weekly` runs it itself after original edition completion and before the refresh, for this run's candidates, within 10 minutes. It reports through the shared heartbeat as `Год первой публикации`.
No outcome of it stops the weekly run; its counts go to `workflow-log.json` and `work-age-completion.json`.
The review card shows the facts as plain text, «Возраст произведения: Впервые издана в 2016, 42 издания в Open Library», and nothing when they are unknown.
The fit score input does not include them.

To record the work age of books already collected, run `./scripts/complete-work-age` (preview) and then `./scripts/complete-work-age --apply`, then rerender the review.
The command takes every book in Candidate History by default, decided or not, since the age of the work is a fact about the book (`--scope undecided` takes the books without an editor decision, `--scope current` this run's candidates); it spends at most an hour, and a run cut short by that budget is finished by running it again.
After 3 unavailable answers in a row (Open Library down or rate-limiting) the pass stops asking; the remaining books stay unchecked for the next run and are counted as `skipped_catalogue_unavailable`. An answer resets the count.
It refuses to write while a weekly run is in progress.

## PM Deal-Email Pre-Collection Step (#946)

Publishers Marketplace deals come from the member's "Today's Deals from PublishersMarketplace.com" emails, which arrive in a mailbox dedicated to AutoScout.
The pass that reads them is declared in `configs/pm-deal-email.json`, beside the weekly config.

`./scripts/run-weekly` starts the pass itself after the toolchain check and before the source check, on every weekly run: mail is cheap to read, so there is no once-a-day guard.
The run announces it as «Читаю письма Publishers Marketplace о сделках...» and reports `Сбор PM: прочитано писем N.` through the shared heartbeat, at most once per 60 seconds and with no titles.
During the pass, `./scripts/doctor` reports the `pm-precollection` stage as `run-in-progress` with its counts, elapsed time against its budget and the last snapshot time, with no escalation question.
The pass may spend at most 5 minutes (`PM_PASS_RUNTIME_BUDGET_SECONDS`): at that allowance it fetches no further email, keeps what it read and ends `partial`, and the next pass reads the rest.
No outcome of the pass stops the run.
A failed pass (no credentials, refused login, no connection, timeout, a missing folder) is named in the completion summary as a failed source; a `partial` pass is named as incomplete coverage.
Deals emails that parse into no deal at all are the named failure `pm_no_deals_parsed`: a PM format change, not a quiet week. That pass writes neither the result nor the watermark, so the next pass reads the same emails again.
A week in which PM sent emails but none carried a new deal reports `collected` with the outcome `no_new_deals`; a week with no new email reports `no_new_messages`.
A run resumed with its capture already current starts no pass.

Only one pass runs at a time.
A pass holds a lock beside its attempt record for the whole read, merge and write, because two overlapping passes could lose deals while the watermark marks them read.
A second pass, from the weekly run or by hand, refuses at once with `pm_pass_in_progress` and writes nothing; the operating system releases the lock when a pass ends, also when it crashes.

`./scripts/doctor` reports the last PM pass whether or not a run is going: its status, date and counts, and the number of deals in the last result.

The same pass can be run by hand, on the machine that runs the weekly scan:

```bash
./scripts/collect-pm-deals
```

It prints counts only and exits 0 when a result was written and 2 when the pass failed.
It writes the result, the attempt record and the watermark under `data/pm-precollection/`, which Git ignores.
It opens the mail folder read-only and never sets, moves or deletes a message.

### Mailbox setup

The pass reads one Gmail label of the dedicated mailbox over IMAP (`imap.gmail.com`, port 993, SSL).

1. **Label and filter.** In Gmail, create the label `PM`. Add a filter for mail from `daily@publisherslunch.com` that applies the label `PM`. The label is the folder the declaration names, so the pass sees no other mail.
2. **App password.** Turn on 2-Step Verification for the account, then create an app password for AutoScout in the Google account's security settings. The pass logs in with that app password, never with the account password.
3. **Key file.** On the machine that runs the weekly scan, create `~/.config/autoscout/pm-mailbox.env` with mode `0600`:

   ```
   AUTOSCOUT_PM_IMAP_USER=<the mailbox address>
   AUTOSCOUT_PM_IMAP_PASSWORD=<the app password>
   ```

   The same two names may be set in the environment instead; the environment wins.
   Neither value is ever written to the repository, configuration, state, logs or the heartbeat.

To move the source to another mailbox, change the host, port and folder in `configs/pm-deal-email.json` and the two values in the key file; no code changes.

### The PM row as a rights lead (#948)

Each PM deal in the review carries one «Сделка о правах» line built from the parsed fields only: publisher and acquiring editor, agent and agency, the contact address listed after the deal, the territory of the deal, the territories already sold («права уже проданы», from "Rights also to …"), the deal size band and type, the expected publication season or month and year, the PM category and the author's earlier titles.

When Russia is among the territories already sold, the line says «русские права уже проданы».
A field the deal does not state is left out; nothing is guessed, and no logline or retail link reaches the review.

The PM source in the weekly config may declare `pm_min_deal_size_band` (`nice`, `very nice`, `good`, `significant` or `major`).
It is off by default, so every deal passes.
When it is set, a smaller deal is left out as `below_minimum_deal_size` and a deal with no disclosed size as `deal_size_not_disclosed`; both are counted in the source run, not reported as a shortfall.

The pass result is `schema_version` 2.
A version 1 result from before #948 stays readable to the weekly capture and the doctor, with the new fields empty.
The next pass keeps its rows and reads their emails again within the maximum age, so each row gains the fields as its email is read; a pass cut short keeps the rows it has not reached yet, and the next one continues.
Only a title linked to a bookshop page (Bookshop.org, Amazon, Barnes & Noble, IndieBound, Indigo) counts as an earlier book.

### The original edition in the review (#969)

The review shows an «Оригинал» line in HTML and an optional column in Markdown; the editor CSV adds an optional `Original edition` column. A confirmed original shows its title, language, optional year and Open Library evidence. An unconfirmed language is marked as such. An original stated only by a national library's record (`library_stated`) shows its title (or «Перевод, язык оригинала — …») and links the library record. A translation without a confirmed original shows «Перевод, оригинал не найден» and translator-credit evidence when available. Unknown or malformed records show nothing. The CSV places the evidence URL in parentheses.

### The Russian-edition mark in the review (#1181)

The review shows the Russian-edition Finding as a mark under the title of the HTML card, with a «Русское издание» line and its evidence link in the card; Markdown adds an optional «Поиск русского издания» column and the editor CSV an optional `Russian-edition finding` column. A `confirmed` finding reads «Есть русское издание: «…»» with the publisher and year when known and links the State Library record («Каталог РГБ») or the LitRes page («ЛитРес»). An `unconfirmed` one reads «Возможно, есть русское издание: «…» (каталоги не подтвердили)», links LiveLib and looks different in HTML. `no_find`, missing or malformed records show nothing. The CSV places the evidence URL in parentheses. A marked book keeps its place, tabs and priority (ADR 0024).

### The Russian-edition stage of the weekly run (#1181)

With `russian_edition_completion: true` in the weekly config, `./scripts/run-weekly` runs the Russian-edition pass after work-age completion and before the review, on every undecided book (this run's books first), and the review of that same run shows its marks; absent or false, the stage never starts and asks nothing. Any other value (the string `"true"`, say) keeps it off too, but the run logs the stage as failed with `invalid_weekly_config`, so `run-health` and the doctor name it. The other keys are optional and override the pass's defaults: `russian_edition_max_books` (150 books), `russian_edition_max_livelib_searches` (25 LiveLib searches a pass, since LiveLib refuses a client after about 30), `russian_edition_budget_seconds` (1800) and `russian_edition_skip_sources` (any of `livelib`, `wikidata`, `rsl`, `litres`; at least one of `rsl` and `litres` must stay). The config validator reports an unusable value or a selection without a catalogue; met by the run, it fails the stage with `invalid_weekly_config` and the run goes on: no other part of the run stops on these keys.
The stage takes only the time left in the run: its budget is the smaller of `russian_edition_budget_seconds` and what remains of the run's worst case (`docs/operator/run-server.md`, "Time limits") after the editorial-fit stage and publication; with less than 50 seconds left it is skipped (`run-health`: `russian_edition_completion=skipped`, `russian_edition_completion_skipped=run_time_limit`; the doctor names it). The report transaction is taken only around its write, never during the lookups; the stage's report file, `russian-edition-completion.json`, is written after it, and a failure to write that diagnostic is noted in the stage's log entry (`report_file: not_written`) and never stops the run.
LiveLib searches are 6 seconds apart and Wikidata requests 7; a source that fails or blocks 3 times in a row is not asked again that pass; a book without a confirmed edition is asked again after 28 days. The heartbeat reads `Поиск русских изданий: проверено N, найдено M.`.
No outcome of the stage stops the run: an exception, an unreadable Candidate History, the budget or every source blocked costs only the marks. `workflow-log.json` holds its counts and, per source, the `answered`, `blocked`, `failed` and `not_asked` requests. `run-health` and the doctor read the latest attempt of the date only, including a retry that failed before its `operation-start` (at the repository preflight, the update or the code gate) or crashed before its first stage (an `unexpected-error` entry right after the earlier attempt's last entry), and they skip an entry they cannot read; `run-health` prints `russian_edition_completion=<status>`, `russian_edition_completion_problem=<cause>` when it failed, and `russian_edition_source_problem=<source>|blocked=N|failed=N|not_asked=N` for a source that blocked or failed 3 times or more; the doctor adds one Russian line for either. The editor's completion message does not change.
To turn it on: run `./scripts/complete-russian-editions` without `--apply` on the run server, read the per-source counts (a source with `blocked` requests is refusing the server: skip it), then set the switch in `configs/current-weekly-pilot-live.json`.

### PM-only books wait for their publication season (#947)

A book whose only signal is a PM deal is one to three years from publication, so neither description stage searches it: it costs no Brave call and no agent call, and it takes no slot in the adjudication cap.
The review shows it as «Книга ещё не опубликована — описание появится позже», it is counted under «Ещё не опубликованы», and it never produces a «Внимание … без описания» line for PM.
In place of the generic «Описание требует дополнительной проверки», its description line reads «Описания пока нет: книга ещё не опубликована. Что известно о ней — в сведениях о сделке на этой карточке.» This is display text only: Candidate History keeps the placeholder, so every stage and count treats the row as before.
Both stages read the deal from the book's source evidence (`autoscout/pm_publication.py`); only a captured PM deal counts, never a PM rights offering.
Candidate History keeps one flag, `cited_beyond_pm_deals`, so a book another source cited in an earlier week is never held back when later only PM reports its deal.
It also keeps the earliest re-check date a PM deal gave (`pm_publication_recheck_date`), so a later deal for the same book never moves the re-check later.

The book goes back to description search two months before its publication season starts (`PUBLICATION_RECHECK_LEAD_MONTHS`).
The seasons start in January (winter), March (spring), June (summer) and September (fall) of the stated year (`SEASON_START_MONTHS`); a deal that names a month starts from that month, and one that names only a year from its January.
A deal that names no publication is searched again 365 days after it was reported (`DEFAULT_PUBLICATION_RECHECK_DAYS`), the near end of the one-to-three-year range, so a book is never searched later than its earliest plausible publication.
As soon as another source cites the book, it is no longer PM-only and both stages search it in that same run.
A PM page (a dealmaker profile or the deals page) is never grounding evidence for a description: the stages neither offer nor count it.

### A short description from the PM deal logline (#950)

A full description is not expected for a PM deal: the book is one to three years from publication and no page describes it yet.
The weekly-config switch `pm_logline_descriptions` lets the deal's logline ground a short Russian description instead.
It is off by default in code and **on** in `configs/current-weekly-pilot-live.json` and `configs/current-weekly-live.json`, by Dima's decision of 2026-11-06 on #950.
With the switch off, the stage reads no logline and calls no model.

With the switch on, the `pm-logline-descriptions` stage runs after both description stages (`autoscout/pm_logline_description.py`).
For each visible PM-only book with no description of its own, it finds the deal row in the Git-ignored PM pass result (same title, authors, rights lead and evidence link) and makes one Codex call with that row's logline alone, through the same Codex agent the description stages use.
The prompt forbids adding any fact the logline does not state, and leaves out pitch comparisons ("pitched as X meets Y"), the deal terms and the dealmakers.
A logline is used only when it is a sentence or more: at least 8 words and 40 characters, not starting with a joining word such as "and" (the second title of a two-book deal); a fragment gets no description, and the book keeps «Книга ещё не опубликована — описание появится позже».
An answer that is not safe Russian prose, or longer than 600 characters, writes nothing.
The stage makes at most one call per book and spends at most 15 minutes; a book it did not reach waits for the next run.
A logline that opens with a foreword credit ("with a foreword by A. Name, an introduction to …") is judged without the credit.

The Codex call runs on the `description` entry of `configs/codex-model-pin.json`, like description enrichment, adjudication and original-edition completion, and never on the model named in the operator's `~/.codex/config.toml`.
Stage 0 reads that entry too: an unreadable entry, or a model the account cannot use, stops the run before capture (`codex_model_pin_invalid`, `codex_pinned_model_unavailable`).
On 2026-11-10 the first run on the run server found `gpt-5.5` in the global config, every description call failed in about three seconds, and the run wrote no description for 14 PM books.

A call that fails is not a logline the model declined.
The stage counts it under `failed`, with the reason under `failure_reasons` (`model_unavailable`, `usage_limit`, `timeout`, `codex_exit_nonzero`, `codex_not_started`, `invalid_response`, `exception`, and on the Claude API backend `model_key_unusable` and `pw_text_not_permitted`), and never records it as declined, so the book is tried first in the next run.
After `model_unavailable`, `usage_limit`, `codex_not_started` or `model_key_unusable` the model is not called again in that run.
Any book left `failed` sets `attention_required` on the stage result, and the weekly run then ends as needing attention for descriptions.

The second weekly-config switch, `pm_logline_deepseek_fallback`, sends a book whose Codex call failed once to the DeepSeek API instead (`fallback_calls`, `written_by_fallback`).
It is off by default in code and **on** in both live configs, by Dima's decision of 2026-11-12; the permission is recorded in `docs/agents/contracts/pm-precollection.md`.
The fallback is never asked about a logline Codex answered and declined.
The key is read from `DEEPSEEK_API_KEY`, or from `~/.config/autoscout/deepseek.env` (mode `0600`, one line `DEEPSEEK_API_KEY=…`) on the machine that runs the weekly scan.
With the switch on and no key, the stage runs without a fallback and reports `fallback_unavailable: deepseek_key_missing`.
It reports through the shared heartbeat as «Описания по логлайнам PM: готово N, выполняется N, ожидают N.», and `./scripts/doctor` shows it as `run-in-progress` with its counts and budget.

The description carries its own provenance, `pm-deal-logline`, with the SHA-256 digest of its logline and no evidence URL.
The logline itself stays in the pass result: it never reaches Git, state or the review.
The description is cached by that digest in `data/pm-precollection/pm-logline-descriptions.json` (Git-ignored, beside the pass result), so the same logline is never sent again in a later week; a changed logline is described again.

The review labels the row «Описание по логлайну сделки PM, не полное описание книги» and shows no depth marker beside it.
It is not a gap: it is not counted under «Не подтверждены источниками», it is left out of «Описаний на русском: X из N», it produces no «Внимание … без описания» line, and it is reported on its own line «Описания по логлайну PM: N» (in the review summary: «по логлайну сделки PM — N»).

The book stays PM-only for #947.
When it reaches its re-check date, or another source cites it, the grounded and adjudication stages search it as usual, and a description grounded on its own (grounded, agent-adjudicated or backfilled) replaces the logline one.
A guess from the title or an unverified agent answer never does, and the logline stage never writes over a description grounded on its own.

## PW Issue Pre-Collection Step (#956)

Publishers Weekly reviews come from the subscriber's weekly "Your <date> Digital Edition Has Arrived" emails, which arrive in the `PW` label of the AutoScout mailbox (#953).
The pass that reads them is declared in `configs/pw-issue-pdf.json`, beside the weekly config.
PW gave written permission on 2026-11-03 for automated download and processing of the Digital Library issues (#935): no PDF or review text is republished or shared outside the organisation, nothing is used for training, and only the subscription's own credentials are used, with no workaround.

`./scripts/run-weekly` starts the pass itself after the PM pass and before the source check, on every weekly run.
The run announces it as «Читаю новый выпуск Publishers Weekly...» and reports `Сбор PW: обработано выпусков N.` through the shared heartbeat, at most once per 60 seconds and with no titles.
During the pass, `./scripts/doctor` reports the `pw-precollection` stage as `run-in-progress` with its counts, elapsed time against its budget and the last snapshot time, with no escalation question.
A week with no new issue email reads the mailbox only and reports `no_new_issue`; it opens no subscriber link.
A new issue is a 90–270 MB PDF and a minute or two of parsing. The pass starts no new issue after 15 minutes (`PW_PASS_RUNTIME_BUDGET_SECONDS`); an issue already started finishes, and the rest stay pending for the next pass.
No outcome of the pass stops the run.
A failed pass is named in the completion summary as a failed source; a pass that collected only some issues is named as incomplete coverage.
An issue whose PDF holds no Nonfiction review at all is the named failure `pw_zero_nonfiction_rows`, not an empty success.

**PW device sessions.** Each pass that opens a subscriber link counts with PW as one more device, and the account allows only a few.
When PW refuses the link (`pw_subscriber_link_rejected`, typically "You've exceeded your device session limit, please verify your account"), the pass stops at once, and the summary tells the editor to open the Digital Library from the email and verify the account.
That verification is a human step: AutoScout never automates it.
Run the pass from the weekly machine only, and avoid ad-hoc downloads elsewhere.

`./scripts/doctor` reports the last PW pass whether or not a run is going: its status, date and counts, the newest issue in the result, and how many issues wait for a retry.

The same pass can be run by hand, on the machine that runs the weekly scan:

```bash
./scripts/collect-pw-issue
```

It needs the optional PDF reader, installed once with `.venv/bin/python -m pip install -e '.[pw]'`.
It uses the PM mailbox key file (`~/.config/autoscout/pm-mailbox.env`) and the Gmail label `PW`, with a filter for mail from `delivery@mail.emagazines.com`.
The editor's own forwarding address is not kept in the repository: set `AUTOSCOUT_PW_FORWARDING_SENDER` in the environment or as a line in the same key file.
Without it the pass reads only the vendor's sender, so forwards from the editor are skipped.
It prints counts only and writes the result and attempt record under `data/pw-precollection/`, which Git ignores; the PDF is deleted after each issue.
The weekly capture then turns the new Nonfiction reviews into candidates (#955).
For the local 2026 PDF backfill, `./scripts/stage-pw-pdfs` stages bibliographic
rows and `./scripts/describe-pw-pdfs` can prepare Russian descriptions in
advance through a model listening only on `127.0.0.1`. This route never sends
the licensed review text to the normal Codex or search adapters. The one
exception is the DeepSeek provider of that command, which uploads review text
only with `--allow-review-text-upload`; its permission is recorded in
`docs/agents/contracts/pw-precollection.md`. The prepared
result is Git-ignored and keyed by ISBN, issue date, title and authors; the
weekly PW collector accepts only exact matches after the corresponding issue
rows enter its bibliographic collection. An absent prepared result preserves
the earlier metadata-only behavior. See `docs/cli-reference.md` for commands.
For a standalone archive import, `./scripts/backfill-pw-history` previews
the staged rows against main Candidate History and, after local descriptions
and identity conflicts are resolved, `--apply` adds new records and rerenders
the cumulative review without invoking `./scripts/run-weekly`.
The books it adds wait for the editor in their own block of the review
(«Архив PW 2026») until each has a decision; `docs/cli-reference.md` describes
the marker behind the block.
