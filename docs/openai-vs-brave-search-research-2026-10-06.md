# OpenAI web search versus Brave for AutoScout

Research date: 2026-10-06. Scope: current official documentation and repository inspection; no paid API benchmark, production change, or source onboarding was performed. Prices are public USD list prices, not verified account-specific charges.

## Recommendation

Keep Brave for routine URL discovery. Evaluate OpenAI Responses web search for difficult description cases, against the **existing Codex web-search adjudicator**, before changing the production pipeline. AutoScout already combines Brave retrieval with agentic web research; Responses would introduce a different integration and operating model, not the first such capability.

OpenAI could improve difficult identity resolution or cross-language research, but documentation establishes capabilities, not better book coverage or accepted descriptions. A blanket replacement is presently unsupported. The useful question is which route produces more independently verified, editor-useful descriptions within our latency and cost limits.

## What AutoScout actually needs

Candidate discovery is restricted to configured sources. General search may enrich an existing candidate but cannot create one. Consequently, claims about finding more books through unrestricted search do not justify replacing Brave under the current product rules. See [ADR 0005](adr/0005-configured-source-discovery-only.md) and [architecture](architecture.md).

The [Brave adapter](../autoscout/grounded_search_adapter.py) supplies up to 20 URLs with metadata. Titles and snippets are discovery hints, never identity or description evidence. AutoScout fetches pages and applies its own checks. The 20-result choice has a concrete origin: an independent Library Journal review ranked eighth, while several earlier retail results repeated one publisher's blurb. A short synthesized answer can omit exactly the independent evidence we need.

The [grounded stage](../autoscout/grounded_description.py) allows at most two discovery requests sharing a 10-second discovery budget, three page fetches, and three workers. The adapter itself uses a 10-second vendor timeout. Host, path, language, and owner constraints remain controlled by source policies (then `configs/grounded-source-policies.md`; retired in #1123 in favour of `configs/source-owner-policy.json`). Domain filtering alone cannot enforce this contract.

The [description adjudicator](../autoscout/description_adjudication.py) already invokes Codex with `tools.web_search=true`. Its prompt requires independent searching, exact title and author evidence, two distinct owners, and an unresolved outcome when evidence is inadequate. Starting URLs are explicitly not evidence. Comparing Responses with Brave alone would therefore miss an important part of the incumbent system.

Russian-edition verification has its own [provider and evidence model](../autoscout/russian_editions.py); it is not simply the Brave description lookup. A search answer must not replace catalogue evidence or convert failure to find an edition into proof that no edition exists.

## Documented capabilities

OpenAI's `web_search` is a hosted tool inside the Responses API. A model can plan multiple searches and synthesize a cited answer. `include: ["web_search_call.action.sources"]` exposes consulted URLs beyond inline citations. Responses supports allowed/blocked domain filters and live access control; `external_web_access` defaults to true. `tool_choice: "auto"` permits no search, so a benchmark must require it. The guide does not promise a Brave-style, fixed-size ranked result page, pagination, or an equivalent explicit `search_lang` filter. These differences make it a different interface rather than a drop-in endpoint. [OpenAI web-search guide](https://developers.openai.com/api/docs/guides/tools-web-search?api-mode=responses).

Brave's Web API provides structured results, up to 20 per page, pagination, language/country parameters, freshness filtering, and optional extra snippets. Freshness concerns the page's date, not necessarily a book's publication date. AutoScout currently uses count and language controls but does not request extra snippets. [Brave Web API reference](https://api-dashboard.search.brave.com/api-reference/web/search/post).

Brave also offers LLM Context for model-oriented retrieval and Answers for synthesized responses. This comparison should therefore avoid treating synthesis as uniquely available from OpenAI. Neither alternative automatically satisfies our evidence checks. [Brave LLM Context documentation](https://api-dashboard.search.brave.com/documentation/services/llm-context), [Brave product announcement](https://brave.com/blog/most-powerful-search-api-for-ai/).

| AutoScout task | Assessment |
| --- | --- |
| Routine title/author URL lookup | Brave fits the current bounded adapter with the fewest changes. |
| Ambiguous identities and descriptions | Responses is worth testing against current Codex adjudication; quality gain is unmeasured. |
| Multilingual evidence | Test explicitly. Our existing language handling is valuable; prompting in a language does not establish equivalent filtering. |
| Configured-source collection | Neither search API replaces configured-source provenance and capture contracts. |
| Russian-edition checks | Search can suggest evidence; preserve native provider outcomes and uncertainty. |
| Research into proposed sources | Potential agent-assistance use, subject to the separate source-onboarding gates. |

Engineering inference: keep a Responses trial outside the current synchronous, serial HTTP adapter until measured latency demonstrates compatibility. Model planning within a 10-second URL-discovery phase could reduce throughput or hit deadlines. A source URL list is also not a retained page capture, proof of ownership independence, or proof that the cited page supports the claim.

## Costs

OpenAI lists **$10 per 1,000 web-search calls**, plus retrieved search-content tokens at model rates and ordinary model input/output usage. With non-preview search on `gpt-4o-mini` or `gpt-4.1-mini`, search content is billed as a fixed 8,000 input-token block per call. One user request can involve several searches; request count is not a reliable cost estimate. [OpenAI pricing](https://developers.openai.com/api/docs/pricing).

Brave currently lists **$5 per 1,000 Search requests** and $5 monthly credit. Answers has a different schedule: $4 per 1,000 requests plus $5 per million input/output tokens. The public announcement describes attribution as a condition for free credit; verify the project's actual subscription and eligibility before budgeting credits. [Brave pricing](https://brave.com/search/api/), [Brave plan announcement](https://brave.com/blog/most-powerful-search-api-for-ai/).

The following arithmetic assumes one Brave request or one OpenAI search call per lookup, before credits, taxes, retries, model tokens, and all downstream work:

| Lookup volume | Brave Search | OpenAI search-call fee only |
| --- | ---: | ---: |
| 100 | $0.50 | $1.00 |
| 170 | $0.85 | $1.70 |
| 1,000 | $5.00 | $10.00 |
| 170 candidates × 3 searches each | $2.55 | $5.10 |

Thus OpenAI has twice the public per-call search fee, but this is **not** an end-to-end cost ratio. Brave requires downstream processing; Responses might consolidate some work or add redundant work. Current Codex CLI usage and separate API billing must be measured separately. Compare total cost per newly accepted description, including retries, fetches, generation, and adjudication. No exact weekly cost is established by this report.

## What the retained run tells us

The [grounded-description stage artifact](../runs/current-weekly-pilot-russian/grounded-description-stage.json) for operation date 2026-10-05 records 170 attempted candidates: 25 grounded, 5 single-source descriptions, and 140 without descriptions. It used approximately 831 of its 900 seconds. This is one stage snapshot, **not final description coverage after adjudication**.

The 140 failures break down as 54 source-identity disagreements, 25 title mismatches, 20 author mismatches, 13 search/fetch failures, 5 no-results cases, 6 insufficient-depth cases, 1 subject-content disagreement, 11 invalid descriptions, and 5 insufficient-subject-text cases.

These observations motivate testing identity and evidence quality, not assuming search coverage is the main bottleneck. Only five are explicitly labelled no-results. Other failures could involve retrieval, page extraction, strict matching, or downstream generation. They do not establish that Brave is responsible, nor that a more fluent answer would pass the evidence gate.

## Proposed evaluation

Prepare a frozen, read-only benchmark of 60 existing candidate identities: 15 previously successful controls, 15 identity/title/author mismatches, 10 weak-evidence cases, 10 multilingual cases, 5 no-results cases, and 5 search/fetch failures. Use disjoint strata and include title collisions, subtitles, translated titles, and publisher/retailer duplicates. Independently verify the reference evidence; a previous rejection is not automatically ground truth.

Run two comparisons separately:

1. **Retrieval:** Brave versus Responses-generated URL candidates, followed by the identical local fetch, policy, identity, and description gates. Hold fetch allowance and total time budget constant. Deduplicate consulted URLs; do not rely only on the answer's citations. Count all searches and tokens.
2. **Adjudication:** existing Codex CLI web adjudication versus a Responses implementation of the same work-item and response contract. Reuse the same input, hints, owner rules, and apply gate. Pin and record model/effort where supported; if models differ, report this as an end-to-end product comparison rather than an isolated search-engine comparison.

Use same-day paired runs, alternate provider order, and repeat three times. Retain bounded URLs, evidence notes, parameters, timing, usage, and sanitized failures outside canonical state; do not retain full copyrighted pages. Require search, set explicit application time/spend limits, and count partial or timed-out results as failures. No production writes or new-candidate creation belong in the benchmark.

Measure accepted-description yield, independently verified identity errors, two-owner evidence yield, policy rejections, unresolved cases, p50/p95 latency, stage-budget completion, run-to-run variance, and total cost per accepted description. Have a reviewer assess results without provider labels.

Suggested pilot gate, requiring agreement before adoption: at least a 10-percentage-point gain in accepted descriptions on difficult cases, no observed new false identity matches, no control-set regression, and compliance with agreed time and spending limits. Sixty cases provide pilot evidence, not proof of a zero-error rate. Confirm a win on a larger held-out set before switching defaults.

If the gain appears only in difficult cases, prefer a selective fallback. If Responses only matches existing Codex adjudication, decide based on reliability, observability, and total operating cost. If there is no meaningful gain, retain the current hybrid and address the measured extraction or identity bottleneck.
