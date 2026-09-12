# Early-alert evaluation: first implementation

This change makes the existing momentum rule measurable. It does not introduce
a trained forecasting model or establish that alerts outperform ordinary cards.

## Changes

- Require the latest three calendar dates, not three potentially stale readings.
- Allow historical selection with `as_of`; all filters use only data up to that date.
- Keep historical replay in a temporary signal database, separate from live history.
- Show exact-horizon target hits (at least 20% AND $1), misses, median return,
  pending observations, and unavailable observations on the results page.
- Reject invalid starting and outcome prices during evaluation.
- Aggregate set relevance in groups instead of repeatedly scanning all history.

## Run in Codespaces

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m scripts.replay_early_movers --as-of 2026-09-04
```

Restart Flask and visit `/backtest-early-movers` to see the updated live results.
The existing cached Early Movers list updates when the normal analysis job runs;
this change does not rewrite the existing saved alerts or cached JSON files.

## First historical result

Source checkout: 3ad34de6a14c851e57095f694267dd08893cfd85.
Prices cover August 28 through September 11, 2026 (15 daily snapshots).
Live signal records contain 50 alerts on September 10 and 50 on September 11;
none are mature at the first 3-day horizon in this checkout.

September 4 replay, top 50 alerts under the existing rule with the freshness fix:

| Exact horizon | Completed | Target hits | Mean price return | Median price return |
| --- | ---: | ---: | ---: | ---: |
| 3 days | 50 | 0 (0%) | 1.36% | 0% |
| 7 days | 50 | 2 (4%) | 2.83% | 0% |
| 14 days | 0 | Pending | — | — |

These are historical research results, not predictions issued on September 4.
The rule already requires a 10–50% rise before entry. It detects momentum after
an initial move; the test asks whether meaningful additional gains follow.
The 20% and $1 thresholds are provisional research targets, not customer-validated
requirements. A target is measured on the exact horizon, not an intraperiod peak.

## Limits and next experiment

There is no matched control group, execution model, or evidence of forecast
calibration. Repeated product alerts are correlated. Market price does not
guarantee available stock or an executable sale; fees and shipping are excluded.
Missing exact-date data is unavailable, never counted as success or failure.
Historical data corrections can also make replay differ from what was observable
at the time; price snapshot collection timestamps are not available here.

Next: retain this baseline, define a reseller-relevant target and costs, compare
candidate rules against a price-matched control group, then freeze the chosen
rule and evaluate on new future data. Do not tune repeatedly on these 15 days
and advertise the resulting historical hit rate as predictive accuracy.
