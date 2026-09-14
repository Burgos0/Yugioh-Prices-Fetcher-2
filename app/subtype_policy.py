"""
Explicit, stable printing/subtype selection policy for imported prices.

TCGCSV's daily archive can list multiple printings (subtypes -- e.g.
"1st Edition" vs "Unlimited") for the same productId on the same date.
The live `prices` table has no printing column, so the previous importer
behavior was "whichever row happens to come last in the archive's file
order wins" -- an unstable, undocumented policy that can silently jump
between printings from one day to the next.

This module makes that choice explicit and stable:
- The first time a product is seen, prefer "1st Edition"; otherwise pick
  the alphabetically-first available subtype (deterministic, not file
  order) and record it as the product's tracked subtype going forward.
- Once a subtype is tracked for a product, keep using that same subtype
  on every later import.
- If the tracked subtype is missing from a later day's data, NEVER
  silently substitute a different printing's price -- report it instead.
"""
PREFERRED_SUBTYPE = "1st Edition"


def select_subtype(items, tracked_subtype=None):
    """
    Choose which of a product's same-day printing rows to keep.

    Args:
        items: list of raw price dicts for one productId on one date, each
            optionally with a "subTypeName" key.
        tracked_subtype: the subtype previously established for this
            product, or None if it has never been seen before.

    Returns a dict:
        {"status": "tracked" | "established" | "missing_tracked",
         "subtype": str or None, "item": dict or None}

        "tracked": the previously tracked subtype is present today; use it.
        "established": no subtype was tracked yet; one was just chosen
            (1st Edition preferred, else the deterministic fallback below)
            and should be persisted as this product's tracked subtype.
        "missing_tracked": a subtype was already tracked for this product,
            but it is absent from today's data. Per policy, never
            silently substitute a different printing's price -- the
            caller should skip this product for this date instead.
    """
    if not items:
        return {"status": "missing_tracked" if tracked_subtype else "established",
                "subtype": tracked_subtype, "item": None}

    by_subtype = {}
    for item in items:
        key = item.get("subTypeName")
        by_subtype.setdefault(key, item)

    if tracked_subtype is not None:
        if tracked_subtype in by_subtype:
            return {"status": "tracked", "subtype": tracked_subtype, "item": by_subtype[tracked_subtype]}
        return {"status": "missing_tracked", "subtype": tracked_subtype, "item": None}

    if len(by_subtype) == 1:
        (only_key, only_item), = by_subtype.items()
        return {"status": "established", "subtype": only_key, "item": only_item}

    if PREFERRED_SUBTYPE in by_subtype:
        return {"status": "established", "subtype": PREFERRED_SUBTYPE, "item": by_subtype[PREFERRED_SUBTYPE]}

    # Deterministic fallback: alphabetically first non-null subtype name,
    # never "whatever came last in the file".
    named = sorted(k for k in by_subtype if k is not None)
    chosen_key = named[0] if named else next(iter(by_subtype))
    return {"status": "established", "subtype": chosen_key, "item": by_subtype[chosen_key]}
