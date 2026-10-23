# Manual run: watchlist dashboard, 2026-10-22

Suite: `docs/qa/dashboard-e2e-test-cases.md`. Build: local HEAD `87b7bde`,
not pushed, no uncommitted changes (defects were fixed after the run, see below).
Environment: Windows 11, the in-app browser, server on `127.0.0.1:8791`, scratch
origin and clones under `%TEMP%\bf-dash-qa` (deleted afterwards). Nothing real
was touched. The scratch clone held the repo's real four watches.

## Result

Of the 24 manual cases: 23 PASS, 0 FAIL, 1 NOT RUN, and 6 findings (see
Findings). The case not run by hand is TC-24 (console errors): the manual
browser session did not capture them, so it is covered by the automated suite
only. TC-25 (double press of `Save`) was added after the manual run and has only
been run in the automated suite.

| ID | Result | Observed |
|---|---|---|
| TC-01 | PASS | 4 rows, badge `clone ok (detached @ 87b7bde)`, URL `http://127.0.0.1:8791/` (no token), token box hidden |
| TC-02 | PASS | with a forgotten token the rows still loaded (5) and the token box was shown |
| TC-03 | PASS | pasted token: box hid, toggle committed `disable flanco "laptop asus vivobook"` |
| TC-04 | PASS | after reload: box hidden, token still stored, 5 rows |
| TC-05 | PASS | one red banner `Token missing or wrong...`, box visible, stored token null, no commit |
| TC-06 | PASS | `NaviAndrei <andrei.ivan1208@gmail.com> \| chore(watchlist): disable emag "iphone 15"`, row class `off`, button `Off` |
| TC-07 | PASS | `chore(watchlist): enable emag "iphone 15"` |
| TC-08 | PASS | row `emag \| qa monitor 27 \| 999.5 \| 10 \| telegram`, form cleared, commit `add emag "qa monitor 27"` |
| TC-09 | PASS (F-01) | form: `already watching emag "qa monitor 27" (queries match ignoring case and spacing)`; no row, no commit |
| TC-10 | PASS | `query must not be empty`, no commit |
| TC-11 | PASS | cell text is the literal markup, 0 `img` elements, commit made |
| TC-12 | PASS (F-02) | dialog showed 999.5 / 10 / telegram; after save row `850 \| 10 \| telegram, email`, commit `update` |
| TC-13 | PASS | `Nothing changed.`, dialog open, no commit |
| TC-14 | PASS | dialog closed, row unchanged, no commit |
| TC-15 | PASS | target price shows `–`, commit `update` |
| TC-16 | PASS (F-03) | `min_drop_percent must be between 0 and 100`; price 20000000 gave `target_price must be greater than 0` |
| TC-17 | PASS | after pressing `Remove` the row stayed and no commit was made; the confirm dialog itself was not seen or read |
| TC-18 | PASS | with `window.confirm` replaced by a test-only stub that records the text and returns true: text `Remove emag "qa monitor 27"? ...`, row gone, commit `remove` |
| TC-19 | PASS | origin renamed away: two yellow banners (`Saved locally, not pushed: 1 change(s)...` and `... (network). Your change is queued...`), pending list shows the edit |
| TC-20 | PASS | origin back, `Retry push`: banners gone, pending hidden, commit `disable emag "iphone 15"` on origin |
| TC-21 | PASS | other clone set price 2222 and pushed; toggle in page gave red `This watch was changed somewhere else...`; origin kept `other: price 2222`, no dashboard commit; `Reload` showed 2222 |
| TC-22 | PASS | `Reload` showed the other writer's price without a page reload |
| TC-23 | PASS | `not used yet` lists cadence_minutes, track_all_time_low, owner, id; no inputs |
| TC-24 | NOT RUN | not captured in the manual browser; see automated suite |
| TC-25 | NOT RUN by hand | automated only: `test_tc25_double_press_save_makes_one_commit` passed 3 of 3 runs (see F-02) |

## Findings

| ID | Severity | What happened | Status |
|---|---|---|---|
| F-01 | P2 | A refused duplicate add (TC-09) was shown on the form and then also appeared later under "Edits that could not be applied" (`add emag "QA  Monitor 27" — already watching ...`). The same refusal reported twice, the second time as if it were a lost edit. Cause: `GitSync.submit` journals the operation before replay; replay parks a rejected operation in the conflicts list even though the caller already got the 422. | Fixed: `sync.py` `_finish` dismisses the parked entry when the caller's own operation is refused as invalid. Test: `test_tc09_duplicate_add_refused`. |
| F-02 | P3 | After pressing `Save` the dialog stayed open and the row stayed stale for about 3 to 5 seconds while git fetched and pushed, with no busy indication. TC-25 (double press of `Save` on the same edit, automated, 3 of 3 runs) showed that a second press does send a second request: two `update` requests, both answered 200. The server found the edit already applied, so there was one commit on origin, nothing queued and no conflict entry; the dialog closed and the price showed 850. The only effect is the missing busy state. Not tested: a second press after the user changed a field. | Not fixed: needs a design choice (disable the button, or show a spinner). No data risk was found. Logged for a follow-up. |
| F-03 | P2 | A target price above the 10,000,000 limit was refused with `target_price must be greater than 0`, which is false for 20000000. | Fixed: the message now names both limits (`... and at most 10,000,000`). Test: `test_tc16_out_of_range_refused_with_the_right_reason`. |
| F-04 | P3 | One queued edit (TC-19, origin unreachable) produced two yellow banners at once: `Saved locally, not pushed: 1 change(s) are waiting.` and `Saved locally, not pushed (network). Your change is queued and nothing is lost.` Both have a `Retry push` button. The message is true but repeated. | Open. Not fixed. |
| F-05 | none (intentional) | After a disable was queued offline (TC-19), the row still showed `On`. This is by design: the page shows the clone's last known state, and the "Saved locally, waiting to be pushed" list shows the queued edit. The row changes once the push succeeds (TC-20 showed `Off`). | Closed, no change planned. |
| F-06 | P3 | The pending list names the operation with its internal name: `set_enabled emag "iphone 15"` instead of `disable emag "iphone 15"` (or `enable`). Cause: `describeOp` in `app.js` prints `op.op` as it is. | Open. Not fixed. |

## Not checked

A real GitHub remote (credentials, network timeouts), and any browser other than
the in-app one. The text of the TC-17 confirm dialog was not read. TC-18 used a
test-only `window.confirm` override, so a real click on the browser's own
`OK` button was not exercised.
