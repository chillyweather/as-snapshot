# Issue #426 portfolio simulation options

The saved Issue #422 observations were passed through `autoscout.timeout_calibration._analyze_calibration_document`, which is the same post-processing call path used by `calibrate-timeouts`, without running the workflow or changing the recorded artifacts.

The real call-path result for 28 active sources, 3 workers, no recommendations, and 2 attempts is a default source ceiling of 1800.0 seconds, a production portfolio budget of 1800.0 seconds because the current call path uses `PRODUCTION_PORTFOLIO_BUDGET_SECONDS` from `PORTFOLIO_RUNTIME_BUDGET_SECONDS`, a first-attempt makespan of 18000.0 seconds, a first-attempt outcome of `budget_conflict`, a retry-envelope makespan of 36000.0 seconds, retry-envelope headroom of -34200.0 seconds, and an outcome of `budget_conflict`.

The earlier 7200.0 and -28800.0 figures used an explicit weekly budget and are not what the current `calibrate-timeouts` artifact records.

## Option 1: Keep full-ceiling worst case and change only the budget basis

With a 7200.0-second weekly hard budget, the same full-ceiling arithmetic is a first-attempt makespan of 18000.0 seconds, a retry makespan of 36000.0 seconds, and retry headroom of -28800.0 seconds.

If the 600.0-second finalization reserve is deducted first, the available extraction budget is 6600.0 seconds and retry headroom is -29400.0 seconds.

This measures a total worst-case safety bound using the absolute ceiling for every source attempt.

It does not represent observed cost, it remains a permanent conflict for a portfolio larger than three sources under three workers, and a total-only budget does not model per-segment continuation.

Segment continuation is not needed for a total-only 7200.0-second calculation, but it is needed to enforce the 1800.0-second scheduling-segment boundary.

## Option 2: Report observed-duration and full-ceiling envelopes side by side

The 28 production-shaped Issue #422 subprocess durations schedule to a first-attempt makespan of 2294.395775 seconds with 3 workers, and two identical observed-duration rounds schedule to 4588.79155 seconds.

The observed retry envelope has 2611.20845 seconds of headroom against 7200.0 seconds, or 2011.20845 seconds against 6600.0 seconds after the finalization reserve, and is `feasible_with_headroom` with the 180.0-second required headroom.

The full-ceiling side remains 18000.0 seconds for the first attempt, 36000.0 seconds for the retry envelope, and `budget_conflict` against either total budget.

This measures actual replayed duration evidence beside a conservative safety bound.

It does not catch one observation per source establishing tail behavior, future slowdowns, or repeated-run stability, and the observed envelope alone is not a worst-case guarantee.

Segment continuation is needed if the report must explain how the 2294.395775-second observed round crosses the 1800.0-second segment boundary.

## Option 3: Add a segment-aware hybrid simulation

It would keep the observed-duration and full-ceiling envelopes side by side while scheduling work across 1800.0-second segments, a 7200.0-second weekly ceiling, and the 600.0-second finalization reserve.

The Issue #422 arithmetic is that observed first and retry makespans remain 2294.395775 and 4588.79155 seconds, fitting within 6600.0 seconds with 2011.20845 seconds before any additional policy reserve, while full-ceiling first and retry makespans remain 18000.0 and 36000.0 seconds, exceeding 6600.0 seconds by 29400.0 seconds.

This measures operational feasibility under observed costs and worst-case safety exposure with segment continuation represented.

It does not prove future tail probabilities or stability from one observation per source, and it requires a more detailed segment scheduling model.

Segment continuation is required.

## Recommendation

Recommend Option 3.

It preserves the safety warning, removes the false choice between a permanent worst-case alarm and an observed-duration claim, and makes the 1800.0-second segment and 7200.0-second weekly boundaries explicit.

This is a proposal only and is not implemented in Issue #426.
