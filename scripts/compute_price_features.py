#!/usr/bin/env python3
"""CLI for the read-only price-feature layer.

Reads ``data/prices.db`` (or a path passed via ``--db``) and prints a JSON
document containing per-product features suitable for a downstream ranker.

Examples
--------
    # All products, using the full history in data/prices.db
    python -m scripts.compute_price_features

    # Cutoff to avoid future leakage during replay/backtest
    python scripts/compute_price_features.py --as-of 2026-01-15

    # Restrict to specific product_ids and write to a file
    python scripts/compute_price_features.py --product-id 12345 --product-id 67890 \\
        --output /tmp/features.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional, Sequence

# Ensure the repository root is on ``sys.path`` when the script is executed
# directly (e.g. ``python scripts/compute_price_features.py``).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.price_features import (  # noqa: E402  (import after sys.path tweak)
    compute_features,
    features_to_json_payload,
)


DEFAULT_DB_PATH = os.path.join("data", "prices.db")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compute read-only per-product price features from data/prices.db."
        )
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help=f"Path to the prices SQLite database (default: {DEFAULT_DB_PATH}).",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help=(
            "Optional YYYY-MM-DD cutoff. Only rows with date <= as_of are used; "
            "omit to use the full available history."
        ),
    )
    parser.add_argument(
        "--product-id",
        action="append",
        type=int,
        default=None,
        help=(
            "Restrict output to this product_id. May be passed multiple times. "
            "If omitted, all products with history are included."
        ),
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write JSON to this file instead of stdout.",
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
    product_ids: Optional[List[int]] = args.product_id if args.product_id else None
    features = compute_features(
        args.db, as_of=args.as_of, product_ids=product_ids
    )
    payload = features_to_json_payload(features, db_path=args.db, as_of=args.as_of)
    indent = args.indent if args.indent and args.indent > 0 else None
    text = json.dumps(payload, indent=indent, sort_keys=False, default=str)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.write("\n")
    else:
        sys.stdout.write(text)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - trivial dispatcher
    raise SystemExit(main())
