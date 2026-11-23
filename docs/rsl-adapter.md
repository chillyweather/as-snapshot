# RSL Adapter Spike

RSL is configured as a Russian check source family, not as a discovery source:

```json
{
  "name": "RSL",
  "family": "russian-library-catalog",
  "url": "https://search.rsl.ru/",
  "recorded_results_file": "../data/rsl-demo-results.json"
}
```

The catalog can enrich or exclude already-discovered Scouting Candidates, but it cannot create candidates. Discovery still requires an External Signal from a configured discovery family such as `media/curated list`, `award`, or `bestseller/ranking`.

RSL evidence is bibliographic catalog evidence, not proof of current commercial availability. It complements retailer checks and does not permanently replace Ozon, Labirint, LitRes, Chitai-gorod, or Book24 checks.

Recorded RSL results are metadata-only. They preserve query, record URL, result title, result author, language clue, publication data, ISBN when available, source name, check date, safe debug status, candidate identity, and match type. They must not store page bodies, full descriptions, reviews, samples, or long copied source text.

Run the deterministic demo without network access:

```bash
python -m autoscout run-mvp --config configs/rsl-demo.json --output-dir tmp/rsl-demo
```

Expected behavior:

- `Already Cataloged` is excluded from `editor-facing.csv` and written to `internal-exclusions.json` as `found in Russian`.
- `Needs Catalog Review` remains visible and appears in `russian-review-queue.csv` as `unclear`.
- `Open Catalog Opportunity` remains visible as `unclear` because the recorded RSL result set does not contain a candidate-scoped no-match record.
- `Open Catalog Opportunity` is included in `russian-review-queue.csv` with the operational reason that its RSL check was not run.
- `russian-edition-checks.json` includes the RSL run, candidate decisions, query attempts, record URL, source type, source name, decision reason, metadata-only evidence, and `russian_edition_verification` fields.

Opt-in live/capture workflow:

```json
{
  "name": "RSL",
  "family": "russian-library-catalog",
  "url": "https://search.rsl.ru/",
  "enable_live": true,
  "capture_recorded_results_file": "captured-rsl-results.json"
}
```

`enable_live` must be explicit. Default and recorded-pilot runs do not make live requests. When enabled without a fixture response, AutoScout performs bounded per-candidate queries against `https://search.rsl.ru/` using title, author, title-plus-author, ISBN, Search Translation, and Author Transliteration Note when those values exist. The adapter parses metadata only, normalizes it into the same RSL result shape used by recorded files, and can save captured records with `capture_recorded_results_file`.

Captured records can be reused later through `recorded_results_file` with `enable_live` omitted:

```json
{
  "name": "RSL",
  "family": "russian-library-catalog",
  "url": "https://search.rsl.ru/",
  "recorded_results_file": "captured-rsl-results.json"
}
```

For deterministic parser/client tests, `fixture_live_results`, `live_response_file`, or `live_response_files` may provide metadata records or fake response bodies. A fixture entry can also use a safe failure status such as `captcha`, `blocked`, `timeout`, `failed`, `changed-markup`, or `malformed` to verify run-summary warnings without network access.

If live RSL is blocked, CAPTCHA-protected, times out, returns changed markup, or cannot be parsed safely, the run records the provider execution as blocked or failed and the candidate status as `unclear`.
The provider outcome is blocked or failed when the corresponding execution result is closed.
It reports the failure in `run-summary.json` warnings instead of silently treating it as an empty successful result.
An empty or out-of-scope recorded result uses a provider execution status of `not_run` and a null provider outcome; it is not evidence of a Russian-edition absence.
Only complete candidate-scoped no-match evidence may produce `not found in Russian`.
