"""Model qualification and search-optimization evaluation commands: qualify-model, qualify-description-model, compare-search-optimization, preserve-search-optimization-run, editorial-fit-shadow, ranking-benchmark, work-age-report."""

from __future__ import annotations

import argparse
import json
import time
from datetime import date
from pathlib import Path

from autoscout.cli.common import CliCommand
from autoscout.cli.common import SubParsers
from autoscout.codex_model_pin import DEFAULT_BACKEND
from autoscout.codex_model_pin import KNOWN_BACKENDS
from autoscout.search_optimization_comparison import compare_search_optimization


def _register_qualify_model(subparsers: SubParsers) -> None:
    qualify_parser = subparsers.add_parser(
        "qualify-model",
        help="Qualify a model for extraction by replaying the frozen benchmark (Issues #734, #1149).",
    )
    qualify_mode = qualify_parser.add_mutually_exclusive_group(required=True)
    qualify_mode.add_argument("--model", help="Candidate model to qualify.")
    qualify_mode.add_argument(
        "--refresh-benchmark",
        action="store_true",
        help="Rebuild the benchmark from committed weekly runs. The only mode that reads Git.",
    )
    qualify_parser.add_argument("--effort", default="medium", help="Candidate reasoning effort (default: medium).")
    qualify_parser.add_argument(
        "--backend",
        default=DEFAULT_BACKEND,
        choices=KNOWN_BACKENDS,
        help=f"Backend that runs the candidate (default: {DEFAULT_BACKEND}).",
    )
    qualify_parser.add_argument("--repetitions", type=int, default=3)
    qualify_parser.add_argument("--workers", type=int, default=3)
    qualify_parser.add_argument("--source-name", action="append", default=[], dest="source_names")
    qualify_parser.add_argument(
        "--rerun-baseline",
        action="store_true",
        help="Run the pinned model too instead of reusing its stored qualification report.",
    )
    qualify_parser.add_argument("--per-attempt-timeout-seconds", type=float, default=600.0)
    qualify_parser.add_argument("--config", type=Path, default=None)
    qualify_parser.add_argument("--benchmark-dir", type=Path, default=None)
    qualify_parser.add_argument("--reports-dir", type=Path, default=None)


def _handle_qualify_model(args: argparse.Namespace) -> int:
    return _qualify_model_command(args)


def _register_qualify_description_model(subparsers: SubParsers) -> None:
    describe_qualify_parser = subparsers.add_parser(
        "qualify-description-model",
        help="Compare models for grounded description synthesis on frozen evidence (Issues #843, #1149).",
    )
    describe_qualify_mode = describe_qualify_parser.add_mutually_exclusive_group(required=True)
    describe_qualify_mode.add_argument(
        "--candidate",
        action="append",
        default=[],
        dest="candidates",
        help="Candidate as [backend/]model:effort, e.g. claude-api/claude-haiku-5-5:medium (repeatable).",
    )
    describe_qualify_mode.add_argument(
        "--refresh-benchmark",
        action="store_true",
        help="Rebuild the benchmark from committed stage artifacts. The only mode that reads Git.",
    )
    describe_qualify_mode.add_argument(
        "--score",
        type=Path,
        default=None,
        metavar="REPORT_DIR",
        help="Fold the blind-review ratings into the report in REPORT_DIR.",
    )
    describe_qualify_parser.add_argument("--repetitions", type=int, default=3)
    describe_qualify_parser.add_argument("--workers", type=int, default=3)
    describe_qualify_parser.add_argument("--items", nargs="+", default=[], help="Benchmark item slugs to run.")
    describe_qualify_parser.add_argument("--per-call-timeout-seconds", type=float, default=180.0)
    describe_qualify_parser.add_argument("--seed", default=None, help="Blind-sheet shuffle seed (default: timestamp).")
    describe_qualify_parser.add_argument("--ratings", type=Path, default=None, help="Ratings file for --score.")
    describe_qualify_parser.add_argument("--benchmark-dir", type=Path, default=None)
    describe_qualify_parser.add_argument("--reports-dir", type=Path, default=None)


def _handle_qualify_description_model(args: argparse.Namespace) -> int:
    return _qualify_description_model_command(args)


def _register_compare_search_optimization(subparsers: SubParsers) -> None:
    compare_parser = subparsers.add_parser(
        "compare-search-optimization",
        help="Generate a deterministic three-run Search Optimization Comparison.",
    )
    compare_parser.add_argument(
        "--review",
        required=True,
        action="append",
        type=Path,
        dest="reviews",
        help="Path to a search-optimization-review.json artifact (exactly three required).",
    )
    compare_parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Directory to write comparison artifacts.",
    )


def _handle_compare_search_optimization(args: argparse.Namespace) -> int:
    return _compare_search_optimization_command(args.reviews, args.output_dir)


def _register_preserve_search_optimization_run(subparsers: SubParsers) -> None:
    preserve_trial_parser = subparsers.add_parser(
        "preserve-search-optimization-run",
        help="Preserve a Search Optimization trial run into an archive directory.",
    )
    preserve_trial_parser.add_argument("--run-dir", required=True, type=Path)
    preserve_trial_parser.add_argument("--archive-dir", required=True, type=Path)


def _handle_preserve_search_optimization_run(args: argparse.Namespace) -> int:
    return _preserve_search_optimization_run_command(args.run_dir, args.archive_dir)


def _register_editorial_fit_shadow(subparsers: SubParsers) -> None:
    fit_shadow_parser = subparsers.add_parser(
        "editorial-fit-shadow",
        help="Score editorial fit with the fit-v2 Jev question set in shadow mode, or report on it.",
    )
    fit_shadow_subparsers = fit_shadow_parser.add_subparsers(dest="fit_shadow_command", required=True)
    fit_shadow_score = fit_shadow_subparsers.add_parser(
        "score", help="Score the run's editor-facing candidates and write the advisory artifact."
    )
    fit_shadow_score.add_argument("--run-date", required=True)
    fit_shadow_score.add_argument(
        "--adapter",
        required=True,
        choices=("live", "replay"),
        help="live is separately billed TypeSafe usage and needs TYPESAFE_API_KEY; replay needs --replay-file.",
    )
    fit_shadow_score.add_argument("--replay-file", type=Path)
    fit_shadow_report = fit_shadow_subparsers.add_parser(
        "report", help="Print the comparison of recorded shadow scores with the editor's decisions."
    )
    fit_shadow_report.add_argument(
        "--write", action="store_true", help="Also write report.json and report.md beside the scores."
    )
    fit_shadow_subparsers.add_parser(
        "preview", help="Print the exact request for the first candidate. Nothing is sent."
    )
    for fit_shadow_action in fit_shadow_subparsers.choices.values():
        fit_shadow_action.add_argument("--output-dir", required=True, type=Path)
        fit_shadow_action.add_argument("--state-dir", required=True, type=Path)


def _handle_editorial_fit_shadow(args: argparse.Namespace) -> int:
    return _editorial_fit_shadow_command(args)


def _register_ranking_benchmark(subparsers: SubParsers) -> None:
    benchmark_parser = subparsers.add_parser(
        "ranking-benchmark",
        help="Compare each ordering with the current review order on the same books. Read-only.",
    )
    benchmark_parser.add_argument("--output-dir", required=True, type=Path)
    benchmark_parser.add_argument("--state-dir", required=True, type=Path)
    benchmark_parser.add_argument("--json", action="store_true", help="Print the report as JSON.")
    benchmark_parser.add_argument(
        "--max-positives-moved-down-share",
        type=float,
        help="Fail an ordering that moves more than this share of positive books down inside their run.",
    )


def _handle_ranking_benchmark(args: argparse.Namespace) -> int:
    """CLI delegate for ranking-benchmark; returns 0 on success, 2 on input errors."""
    import json

    from autoscout import ranking_benchmark

    share = args.max_positives_moved_down_share
    if share is not None and not 0.0 <= share <= 1.0:
        print("error=--max-positives-moved-down-share must be between 0 and 1")
        return 2
    try:
        report = ranking_benchmark.run_benchmark(args.output_dir, args.state_dir, max_moved_down_share=share)
    except (OSError, ValueError) as exc:
        print(f"error={exc}")
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(ranking_benchmark.render_benchmark(report), end="")
    return 0


def _register_work_age_report(subparsers: SubParsers) -> None:
    report_parser = subparsers.add_parser(
        "work-age-report",
        help="Measure on decided books whether the age of the work would improve the review order. Read-only.",
    )
    report_parser.add_argument("--output-dir", required=True, type=Path)
    report_parser.add_argument("--state-dir", required=True, type=Path)
    report_parser.add_argument(
        "--report-file",
        required=True,
        type=Path,
        help="Where to write the Markdown report; refused inside the state directory.",
    )


def _handle_work_age_report(args: argparse.Namespace) -> int:
    """CLI delegate for work-age-report; returns 0 when the report was written, 2 on input errors."""
    from autoscout import work_age_report

    try:
        text = work_age_report.write_report(args.output_dir, args.state_dir, args.report_file)
    except (OSError, ValueError) as exc:
        print(f"error={exc}")
        return 2
    print(text, end="")
    print(f"report_file={args.report_file}")
    return 0


def _qualify_model_command(args: argparse.Namespace) -> int:
    from autoscout import model_qualification as mq

    config_path = args.config or mq.DEFAULT_CONFIG_PATH
    benchmark_dir = args.benchmark_dir or mq.DEFAULT_BENCHMARK_DIR
    try:
        if args.refresh_benchmark:
            manifest = mq.refresh_benchmark(
                config_path=config_path,
                benchmark_dir=benchmark_dir,
                source_names=args.source_names or None,
            )
            print(f"benchmark_cases={len(manifest['cases'])}")
            for entry in manifest["uncovered"]:
                print(f"uncovered={entry['source_name']}: {entry['reason']}")
            return 0
        report = mq.qualify_model(
            args.model,
            args.effort,
            backend=args.backend,
            repetitions=args.repetitions,
            workers=args.workers,
            source_names=args.source_names or None,
            rerun_baseline=args.rerun_baseline,
            per_attempt_timeout=args.per_attempt_timeout_seconds,
            config_path=config_path,
            benchmark_dir=benchmark_dir,
            reports_dir=args.reports_dir or mq.DEFAULT_REPORTS_DIR,
        )
    except (mq.QualificationInputError, OSError, ValueError) as exc:
        print(f"error={exc}")
        return mq.EXIT_INPUT_ERROR
    print(f"verdict={report['verdict']}")
    for criterion in report["criteria"]:
        print(f"criterion={criterion['name']}:{'pass' if criterion['passed'] else 'fail'}")
    print(f"report_dir={report['report_dir']}")
    return mq.EXIT_PASS if report["verdict"] == "PASS" else mq.EXIT_FAIL


def _qualify_description_model_command(args: argparse.Namespace) -> int:
    from autoscout import description_qualification as dq

    benchmark_dir = args.benchmark_dir or dq.DEFAULT_BENCHMARK_DIR
    try:
        if args.refresh_benchmark:
            manifest = dq.refresh_benchmark(benchmark_dir=benchmark_dir)
            print(f"benchmark_items={len(manifest['items'])}")
            for name, value in manifest["coverage"].items():
                print(f"coverage_{name}={json.dumps(value, ensure_ascii=False)}")
            for name in manifest["uncovered"]:
                print(f"uncovered={name}")
            return dq.EXIT_OK
        if args.score is not None:
            report = dq.score_blind_review(args.score, args.ratings)
        else:
            last_printed = [0.0]

            def _progress(done: int, total: int) -> None:
                now = time.monotonic()
                if done == total or now - last_printed[0] >= 60:
                    last_printed[0] = now
                    print(f"calls_done={done}/{total}", flush=True)

            report = dq.qualify_description_models(
                args.candidates,
                repetitions=args.repetitions,
                workers=args.workers,
                items=args.items or None,
                per_call_timeout=args.per_call_timeout_seconds,
                seed=args.seed,
                benchmark_dir=benchmark_dir,
                reports_dir=args.reports_dir or dq.DEFAULT_REPORTS_DIR,
                progress=_progress,
            )
    except (dq.QualificationInputError, OSError, ValueError) as exc:
        print(f"error={exc}")
        return dq.EXIT_INPUT_ERROR
    for item in report["candidates"]:
        print(f"candidate={item['label']}:{item['verdict']}")
    print(f"blind_review={report['blind_review']['status']}")
    print(f"ranking={','.join(report['ranking'])}")
    print(f"report_dir={report['report_dir']}")
    return dq.exit_code(report)


def _compare_search_optimization_command(review_paths: list[Path], output_dir: Path) -> int:
    """CLI delegate for compare-search-optimization.

    Loads exactly three review JSON artifacts, validates, compares,
    and writes comparison artifacts. Returns 0 on success, nonzero on error.
    """
    import json

    if len(review_paths) != 3:
        print(f"error=exactly three --review arguments required; got {len(review_paths)}")
        return 2

    reviews: list[dict[str, object]] = []
    for path in review_paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"error=cannot_load_review: {path}: {exc}")
            return 1
        reviews.append(data)

    try:
        compare_search_optimization(reviews, output_dir)
    except ValueError as exc:
        print(f"error={exc}")
        return 1

    return 0


def _editorial_fit_shadow_command(args: argparse.Namespace) -> int:
    """CLI delegate for editorial-fit-shadow; returns 0 on success, 2 on input errors."""
    import json

    from autoscout import editorial_fit_shadow as fit_shadow
    from autoscout.jev_adapter import JevAdapter
    from autoscout.jev_adapter import LiveJevAdapter
    from autoscout.jev_adapter import ReplayJevAdapter
    from autoscout.jev_adapter import load_api_key

    try:
        if args.fit_shadow_command == "preview":
            candidates = fit_shadow.read_candidates(args.output_dir)
            if not candidates:
                print("error=no candidates to preview")
                return 2
            print(json.dumps(fit_shadow.preview_request(candidates[0]), ensure_ascii=False, indent=2))
            return 0
        if args.fit_shadow_command == "score":
            try:
                date.fromisoformat(args.run_date)
            except ValueError:
                print("error=--run-date must be an ISO date")
                return 2
            adapter: JevAdapter
            if args.adapter == "replay":
                if args.replay_file is None:
                    print("error=--adapter replay requires --replay-file")
                    return 2
                adapter = ReplayJevAdapter.from_file(args.replay_file)
            else:
                api_key = load_api_key()
                if not api_key:
                    print("error=TYPESAFE_API_KEY is not set; live scoring was not attempted")
                    return 2
                adapter = LiveJevAdapter(api_key)
            result = fit_shadow.score_weekly_run(args.output_dir, args.state_dir, args.run_date, adapter)
            print(
                f"scored={result['scored']} unavailable={result['unavailable']} "
                f"reused={result['reused']} candidates={result['candidates']}"
            )
            for reason, count in result["unavailable_reason_counts"].items():
                print(f"unavailable_reason={reason} count={count}")
        if args.fit_shadow_command == "report" and not args.write:
            # Read-only by default: an on-demand report must not leave the
            # working tree dirty and block the next weekly run.
            report = fit_shadow.build_shadow_report(args.output_dir, args.state_dir)
            print(fit_shadow.render_shadow_report(report), end="")
            return 0
        report = fit_shadow.write_shadow_report(args.output_dir, args.state_dir)
    except (OSError, ValueError) as exc:
        print(f"error={exc}")
        return 2
    print(
        f"shadow_weeks_scored={report['shadow_weeks_scored']} "
        f"shadow_weeks_with_decisions={report['shadow_weeks_with_decisions']} "
        f"activation_evidence={report['activation']['evidence']}"
    )
    return 0


def _preserve_search_optimization_run_command(run_dir: Path, archive_dir: Path) -> int:
    """Preserve a Search Optimization trial run; returns 0 on success."""
    from autoscout.search_optimization_trial import preserve_search_optimization_run

    try:
        preserved_dir = preserve_search_optimization_run(run_dir, archive_dir)
    except (OSError, ValueError) as exc:
        print(f"error={exc}")
        return 1
    print(f"preserved_dir={preserved_dir}")
    return 0


COMMANDS: dict[str, CliCommand] = {
    "qualify-model": CliCommand(_register_qualify_model, _handle_qualify_model),
    "qualify-description-model": CliCommand(_register_qualify_description_model, _handle_qualify_description_model),
    "compare-search-optimization": CliCommand(
        _register_compare_search_optimization, _handle_compare_search_optimization
    ),
    "preserve-search-optimization-run": CliCommand(
        _register_preserve_search_optimization_run, _handle_preserve_search_optimization_run
    ),
    "editorial-fit-shadow": CliCommand(_register_editorial_fit_shadow, _handle_editorial_fit_shadow),
    "ranking-benchmark": CliCommand(_register_ranking_benchmark, _handle_ranking_benchmark),
    "work-age-report": CliCommand(_register_work_age_report, _handle_work_age_report),
}
