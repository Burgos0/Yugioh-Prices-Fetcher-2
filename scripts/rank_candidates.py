#!/usr/bin/env python3
"""CLI for the explainable combined candidate score.

Reads the imported Meta Watch dataset and ``data/prices.db``, fuses the
tournament-adoption adapter with the read-only price-feature layer, and
emits a deterministic JSON document with one ranked row per
``product_id``.

Example:

    python scripts/rank_candidates.py --as-of 2026-08-30 --top 25

The command is read-only: it never mutates ``prices.db``,
``data/meta_watch_lists.json``, or any generated cache.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional, Sequence

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.candidate_score import build_candidates, rank_and_limit  # noqa: E402
from app.meta_watch import DEFAULT_DATASET_PATH  # noqa: E402


DEFAULT_DB_PATH = os.path.join("data", "prices.db")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rank tracked printings by an explainable combined score built from "
            "TCG_ADVANCED tournament adoption and read-only price features."
        )
    )
    parser.add_argument(
        "--dataset",
        default=DEFAULT_DATASET_PATH,
        help=f"Meta Watch dataset path (default: {DEFAULT_DATASET_PATH}).",
    )
    parser.add_argument(
        "--prices-db",
        default=DEFAULT_DB_PATH,
        help=f"Path to prices.db (default: {DEFAULT_DB_PATH}).",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="Optional YYYY-MM-DD cutoff applied to BOTH adoption and price layers.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=25,
        help="Return only the top-N candidates (default: 25; use 0 for all).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write JSON to this path instead of stdout.",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indentation (default: 2). Use 0 for compact output.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    report = build_candidates(
        dataset_path=args.dataset,
        prices_db_path=args.prices_db,
        as_of=args.as_of,
    )
    top = None if args.top is None or args.top <= 0 else args.top
    report["candidates"] = rank_and_limit(report["candidates"], top=top)
    report["returned_candidate_count"] = len(report["candidates"])

    indent = args.indent if args.indent and args.indent > 0 else None
    text = json.dumps(report, indent=indent, sort_keys=True, default=str)
    if args.output:
        directory = os.path.dirname(args.output)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.write("\n")
    else:
        sys.stdout.write(text)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - trivial dispatcher
    raise SystemExit(main())
