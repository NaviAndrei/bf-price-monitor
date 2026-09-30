---
name: check-bf-readiness
description: Pre-Black-Friday go/no-go checklist
disable-model-invocation: true
---

Run the Black Friday go/no-go checklist for `NaviAndrei/bf-price-monitor`.
This is a **read-only** audit: report, never fix, never push, never edit files.
Every item must end as `PASS` or `FAIL` with the evidence you actually observed
in this run (command output, counts, ids). If a check cannot be run, mark it
`FAIL (could not verify: <reason>)` -- never assume PASS.

Run independent checks in parallel where you can.

1. **Last CI run on master was green**
   ```bash
   gh run list --repo NaviAndrei/bf-price-monitor --branch master --limit 1 --json status,conclusion,databaseId,url
   ```
   PASS only if `status` is `completed` and `conclusion` is `success`. Show the run id and url.

2. **All three retailer selectors return > 0 products** (eMAG, PC Garage, Flanco)
   Delegate to the `selector-drift-detector` agent (read-only, live pages, capped turns).
   PASS per site only if it matched at least one real product card with title, price and
   a recognised stock marker. `DRIFT DETECTED` or `BLOCKED` is FAIL for that site. Print the card
   count per site. Do not retry aggressively against the live sites.

3. **`TELEGRAM_FEEDBACK_ALLOWED_USER_IDS` secret exists**
   ```bash
   gh secret list --repo NaviAndrei/bf-price-monitor
   ```
   PASS if that exact name appears in the output. Never try to read the secret's value.

4. **Stored feedback label count** (print the actual number)
   ```bash
   python scripts/feedback.py summary
   ```
   Report the `current labels: N` line plus the per-label counts. PASS if the command ran
   and produced a number; the number itself is judged in item 5.

5. **Label count vs the 30-label gate for #36**
   `min_labels` is 30 (see `docs/anomaly-pilot.md`). PASS if N >= 30. Otherwise FAIL and print
   the gap: `need <30 - N> more labels`. Note: the pilot applies the gate per data split, so 30 total
   is the floor, not a guarantee.

6. **No open P0 or P1 issues**
   `gh issue list --label a,b` means AND, so query each label separately:
   ```bash
   gh issue list --repo NaviAndrei/bf-price-monitor --state open --label "priority:P0" --json number,title
   gh issue list --repo NaviAndrei/bf-price-monitor --state open --label "priority:P1" --json number,title
   ```
   PASS only if both lists are empty. Otherwise list each issue number and title.

## Output

A table with columns `#`, `Check`, `Result`, `Evidence`, then one final line:
`VERDICT: GO` if all six are PASS, otherwise `VERDICT: NO-GO` followed by the failing item numbers.
