# Undated recurring rankings prove freshness by row movement

Issue: #589. Retention point clarified by #716. Cadence window added after the 2026-11-03 weekly run.

## Context

`ranking-table` assumed that a recurring ranking prints its own reporting period.

`validate_ranking_contract_metadata` required `ranking_reporting_period_pattern`, `ranking_reporting_period_date_format`, and a non-negative `ranking_reporting_period_max_age_days`.
`ranking_page_identity` then rejected a period that parsed in the future or fell outside the declared window.
That trio is how a dated source proves that the page in hand is current.

A recurring list that names only its cadence, for example `Los 100 más vendidos de la semana`, prints no date at all.
There is no period text for a pattern to match, no token for a date format to parse, and no period end from which to measure an age.
A declared pattern would be fiction, and a declared maximum age would be a lie dressed as a declaration.

The contract also had no frozen-list detection.
A page that stopped being rebuilt would be re-emitted as a current signal every run, and nothing in the pipeline would say so.

Cúspide, recorded in `docs/issue-584-argentina-source-feasibility.md`, is the concrete case.
A storefront that rebuilds one weekly list in place, with no date, is an ordinary retailer shape rather than a single awkward source.

## Decision

A recurring ranking whose page carries no date declares `ranking_reporting_period_undated: true` and proves currency by row movement.

The declaration is exclusive.
An undated source must declare none of the dated reporting trio, and a source that declares any of them keeps the dated contract unchanged.
The scaffold and `validate_ranking_contract_metadata` apply the same predicate, so a declaration that the validator accepts is one the scaffold accepts.

Freshness comes from one fingerprint of the bounded rows.
The fingerprint is a SHA-256 digest over the rank, title, author, and identifier of every bounded row, in page order.
It covers the whole bounded set rather than a sample, because a stale list that gains one row is still a stale list.
It covers page order, because a reordered ranking is real movement.

The verdict is fail-closed in three ways:

An unchanged fingerprint is a frozen list.
The source run fails with reason `undated ranking list is unchanged since <date>` and safe failure status `ranking_list_frozen`, and it publishes no mentions.
A frozen verdict outranks a collector drop reason, because a frozen list cannot be reported as current whatever else went wrong in the same run.

An unchanged fingerprint inside the source's declared cadence is not yet a frozen list.
When the run date is fewer days after the record's last change than the cadence of `check_frequency` (or `refresh_cadence`) allows — 1 for daily, 7 for weekly, 31 for monthly, 366 for annual — the next edition has simply not been published.
That run is skipped with reason `undated ranking list has not been refreshed since <date>, within its <n>-day cadence` and safe status `ranking_list_not_yet_refreshed`, publishes no mentions, and retains nothing, so the cadence keeps counting from the real last change.
The editor reads it under the skipped sources as «новый выпуск списка ещё не вышел».
A source with no known cadence keeps the strict rule.
Before this, the near-daily weekly runs of 2026-11-01 to 2026-11-03 failed Cúspide one day after its list changed, and the review listed it under the sources that could not be collected.

Absent freshness state is a failure, not a pass.
An undated source cannot prove movement without a retained previous fingerprint, so a run with no state directory fails with status `missing_undated_freshness_state`.

An absent fingerprint is a failure.
Only a collector that observes the bounded rows can supply one, so an undated source collected by a collector that does not observe fails closed rather than collecting unverified.

The source's first accepted observation has no previous fingerprint.
It is allowed through and records what it observed.

## State and layering

The retained fingerprints live in `ranking-freshness.json` in the state directory, one record per configured source, keyed by a digest of source name and URL, sorted by that key.
Each record keeps the fingerprint, the date it was first seen, and the date it last changed.

Three layers stay separate.
A collector observes the fingerprint and writes it into its debug metadata.
`SourceCollection` judges the observation against the retained ledger and returns the updated ledger without writing anything.
The pipeline persists the ledger where the run's other state is written.

That keeps collectors stateless, keeps the verdict beside the decision to publish mentions, and keeps every write in one place.

Retention follows acceptance.
`SourceCollection` decides the Source Run Status first and retains the observed fingerprint only for a status that collected, because the retained record asserts that this was the current list on that date and a failed run asserts nothing.
There is one retention point, after the status is known, so a source that fails for any reason — an empty extraction, a refused page shape, a blocked response — leaves the ledger untouched.

## Consequences

Dated ranking sources are unaffected.
PublishNews, Publishers Weekly, and the Audible and Amazon charts keep their declarations and their verdicts.

An undated source needs a state directory.
A run without one reports the source as failed rather than collecting it, which is why the artifact parity fixture, which uses no state directory, is unchanged: it configures no undated source.

A failed run leaves the ledger exactly as it found it.
A source that had already been accepted keeps the fingerprint that accepted run retained, and its next run is judged against that fingerprint as usual: an unchanged list is still frozen, because a list that has not moved across the outage has still not moved.
Only a source whose failure was its first observation has nothing retained, and that run is the first-run case above, allowed through once without proving movement.
Either way the failure writes nothing of its own.
Before #716 it wrote the run date anyway, which dated the ledger to a run that collected nothing: the next run met the same page, was refused for a reason it could not clear, and the frozen verdict hid the failure that caused it.

A genuinely unchanged bestseller list is reported as frozen.
For a bounded list of any size, byte-identical rank, title, author, and identifier across two runs is far more likely to be a stale page than a stable market, so the honest default is to report rather than publish.

An undated source's `source_week` is the capture's retained observation date (Issue #733).
The page states no reporting period, so the extraction contract does not ask the extractor for a week, and names Circana BookScan only for the generic ranking tables that grounding checks against that statement.
Collection sets the week from the capture's `reference_date` and discards any week the extractor returned, so a model-supplied date is never persisted as if the source had published it.
Replay fixtures already used the same observation date.
Before #733 the contract demanded "the dated source_week stated by the table source note" from these pages, and the extractor either invented a week or returned no rows.
