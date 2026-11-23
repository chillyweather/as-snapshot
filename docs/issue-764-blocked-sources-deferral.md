# Issue #764 — sources refused at fetch: deferral record

Run audited: the 2026-10-05 weekly run, and bounded requests made on 2026-10-06.

Three sources were lost at page fetch in the 2026-10-05 run.
Two are deferred here.
The third, USA TODAY, collects again after a change to the collector's request headers (PR #773).

## Decision

Operator decision, 2026-10-06, recorded in #764.
The editor was not consulted.
Both deferrals are soft retirements (`active: false`, `lifecycle_status: "retired"`) and are reversible.

| Source | Decision | Reason |
|---|---|---|
| Die Zeit Sachbuch-Bestenliste | deferred | HTTP 403 to the collector; the list is subscriber content |
| PublishNews Nielsen Não Ficção Trade | deferred | HTTP 403 with a Cloudflare JavaScript challenge |
| USA TODAY Bestselling Books Nonfiction | kept, repaired in PR #773 | HTTP 402 to the collector's earlier request headers; see below |

## Die Zeit Sachbuch-Bestenliste

- `https://www.zeit.de/sachbuch-bestenliste` and the article URL in the live overlay both return HTTP 403 (5,439 bytes) to the collector.
- A user agent that names the collector and a contact address gets the same 403.
- The article in the live overlay is the July/August 2026 list, published 2026-06-24, so it was out of date for a monthly source before the block.
- The list is `Z+` subscriber content, cut off by the paywall.
- The last retained extraction held no entries, and the source showed one repeated book in six runs (#766).
- The source has been absent from weekly runs since 2026-09-13.

To restore: a subscribed or licensed route to the current monthly list, then onboarding under the runbook.

## PublishNews Nielsen Não Ficção Trade

- `https://www.publishnews.com.br/ranking-nielsen/semanal/9` returns HTTP 403 with a Cloudflare "Just a moment…" JavaScript challenge.
- The result is the same from two machines (the 2026-10-05 run and `./scripts/check-sources` on the same day), and the same for a user agent that names the collector.
- The source collected in 7 of the 8 runs before 2026-10-05.
- The configured URL and the page contract were found sound on 2026-09-13 (`docs/issue-700-publishnews-nielsen-feasibility.md`), so this is an access change, not a source defect.

To restore: `./scripts/check-sources` reports a retired source as `inactive` without fetching it.
Set the source active with its probation status in a separate worktree, run `./scripts/check-sources` there, and merge that change only if the source captures.
The replay fixtures, helper sidecar, and overlay entries are kept for that.

## USA TODAY Bestselling Books Nonfiction (kept)

- `https://www.usatoday.com/booklist/nonfiction` returned HTTP 402 and a 254-byte `Access Restricted` page to the collector's earlier request: user agent `AutoScout metadata collector`, no `Accept` header.
- The site's `robots.txt` does not disallow `/booklist` for general agents; it names AI crawlers separately.
- The response depends on the request headers, and the site publishes no rule for them.
- `AutoScout/1.0 (+https://github.com/chillyweather)` with `Accept: */*` gets HTTP 200 and the full page (363 KB); the same user agent without an `Accept` header gets HTTP 402.
- Other strings that name the collector were refused, for example `AutoScout/1.0 (+https://example.org/autoscout; metadata collector)` and `Mozilla/5.0 (compatible; AutoScout/1.0; +https://github.com/chillyweather/auto-scout)`.
- No feed or API route for the list was found: `/feeds/` is disallowed in `robots.txt`, and `booklist.usatoday.com` redirects to the same page.

Operator decision, 2026-10-06: the collector sends an identifying user agent with a contact address, and does not present itself as a browser.
With the new headers `./scripts/check-sources` captured the source on 2026-10-06, and no other source regressed.
The accepted string may be refused later without notice; the source would then show as failed in the weekly summary.

## Model-qualification benchmark

The frozen benchmark holds a case for Die Zeit.
Qualification refused a benchmark whose source was no longer active.
It now accepts a retired source that is still in the configuration, and refuses only a source that was removed from it.
The next `./scripts/qualify-model --refresh-benchmark` rebuilds from active sources only and will drop the Die Zeit case.
