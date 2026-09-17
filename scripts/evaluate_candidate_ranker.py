#!/usr/bin/env python3
"""Read-only CLI for retrospective evaluation of the candidate ranker."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

from app.candidate_evaluation import DEFAULT_HORIZONS, evaluate_candidate_ranker
from app.meta_watch import DEFAULT_DATASET_PATH


DEFAULT_DB_PATH = "data/prices.db"


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a retrospective, offline rolling-cutoff evaluation of the "
            "combined adoption + price candidate ranker."
        )
    )
    parser.add_argument("--dataset", default=DEFAULT_DATASET_PATH)
    parser.add_argument("--prices-db", default=DEFAULT_DB_PATH)
    parser.add_argument("--start", help="Optional first cutoff (YYYY-MM-DD).")
    parser.add_argument("--end", help="Optional last cutoff (YYYY-MM-DD).")
    parser.add_argument("--step-days", type=int, default=7)
    parser.add_argument(
        "--horizons",
        type=int,
        nargs="+",
        default=list(DEFAULT_HORIZONS),
        help="Forward horizons in days (default: 7 14).",
    )
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument(
        "--hit-threshold-pct",
        type=float,
        default=0.0,
        help="A return strictly above this percentage is a hit (default: 0).",
    )
    parser.add_argument("--output", help="Write JSON here instead of stdout.")
    parser.add_argument("--indent", type=int, default=2)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    report = evaluate_candidate_ranker(
        dataset_path=args.dataset,
        prices_db_path=args.prices_db,
        start=args.start,
        end=args.end,
        step_days=args.step_days,
        horizons=args.horizons,
        top_k=args.top_k,
        hit_threshold_pct=args.hit_threshold_pct,
    )
    text = json.dumps(
        report,
        indent=args.indent if args.indent > 0 else None,
        sort_keys=True,
    )
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
