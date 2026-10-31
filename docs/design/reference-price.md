# Reference price rule, as implemented (T-51)

This describes what the code does today. It is not legal advice and does not
claim the rule matches the law; the legal citation is a separate open item
(T-50, owner decision). `tests/test_reference_price_window.py` pins every
statement below.

## Where it lives
- Window and `thirty_day_low`: `scripts/scrape.py`, the alert block inside
  `_run` (search for `thirty_day_cutoff`).
- Verdict: `scripts/analyze.py`, `evaluate_omnibus_rule`.

## Rule
1. History is stored per product URL (canonical form). The same product at
   two retailers has two separate histories, so windows never mix across
   retailers. The window is per retailer product, not per sales channel.
2. When a price drop triggers an alert, the window is every earlier
   observation of that URL dated within 30 days before today. Today's own
   observation is excluded. Rows older than 30 days are ignored (history is
   kept for 90 days by `prune_history`).
3. `thirty_day_low` is the lowest price in that window. If the window is
   empty it falls back to the previous observed price.
4. `history_days` is today minus the date of the oldest stored observation
   (not the window length). Under 14 days the verdict is
   `INSUFFICIENT_HISTORY`, whatever the drop.
5. Verdict order in `evaluate_omnibus_rule`: insufficient history, then
   `FALSE_DISCOUNT` (new price at or above the low), then
   `INFLATED_REFERENCE` (shop reference price above 1.25 times the low),
   then `GENUINE_DEAL` (at least 5% below the low), else `NORMAL_DROP`.
6. The shop's own struck-through price (`reference_price`, Flanco only) is
   kept for display and the inflated-reference check; it never replaces the
   observed low.

## Known limits
- Observed, not declared: the low comes from this monitor's own scrapes. A
  price the shop held between two scrapes is invisible.
- Per retailer product, so a shop's other sales channels are not covered.
- A zero or missing price in the window is not guarded (T-49, analysis only).
