# Watchlist dashboard: end-to-end manual test cases

Scope: the local watchlist dashboard page (`bf_price_monitor/dashboard/static/`)
driven through a real browser against a real server and a scratch git origin.
It extends the six hand checks in `docs/runbooks/DASHBOARD.md` section 2 into a
full suite. Each case names the automated browser test that repeats it
(`tests/dashboard/test_browser_e2e.py`, opt-in with `-m browser`).

## How to run

1. Build a scratch origin and two clones (`clone` for the dashboard, `other`
   as a second writer), exactly as runbook section 2 describes. Nothing real is
   touched; every push lands in `%TEMP%`.
2. Start the server on the `clone` and open the `Dashboard: ...#token=...`
   address it prints.
3. Run the cases in order. Later cases rely on the watches earlier ones add.
4. Record each result in a dated run log under `docs/qa/runs/`: PASS, FAIL
   (with what was seen) or NOT RUN (with why).

"Origin log" below means:
`git -C <scratch>\origin.git log --format="%an <%ae> | %s" master -3`.

## Conventions

- **Priority:** P1 blocks a release (data loss, security, core flow).
  P2 is a visible defect with a workaround. P3 is cosmetic.
- **Expected** lists only what can be observed: page text, the address bar,
  or the origin log. "No commit" means the origin log's top line is unchanged.

## Test cases

### Access and token

| ID | P | Title | Steps | Expected | Automated by |
|---|---|---|---|---|---|
| TC-01 | P1 | Open from the token link | Open the printed `#token=` address. | Table lists every watch. Badge reads `clone ok (<branch> @ <sha>)`. Address bar has no `token=`. Token box hidden. | `test_tc01_token_link_loads_and_hides_token` |
| TC-02 | P1 | Open without the token | Open the address without `#token=...` in a fresh tab. | Table still lists the watches (reads need no token). Token box is shown. | `test_tc02_no_token_shows_box_and_reads_work` |
| TC-03 | P1 | Paste the token | From TC-02, paste the token, press `Use token`, toggle a watch off. | Token box hides. The toggle commits (origin log shows `disable`). | `test_tc03_pasted_token_allows_writes` |
| TC-04 | P2 | Reload keeps the token | After TC-01, reload the tab, toggle a watch. | No token box after reload. The toggle commits. | `test_tc04_reload_keeps_token` |
| TC-05 | P1 | Wrong token is refused and forgotten | Open with `#token=wrong`, toggle a watch. | One red banner `Token missing or wrong...`. Token box is back. No commit. A reload shows the token box again (token was forgotten). | `test_tc05_wrong_token_refused_and_forgotten` |

### Enable and disable

| ID | P | Title | Steps | Expected | Automated by |
|---|---|---|---|---|---|
| TC-06 | P1 | Disable a watch | Press `On` in a row. | Button reads `Off`, row greys out. Origin log top: `chore(watchlist): disable <site> "<query>"`, author `NaviAndrei <andrei.ivan1208@gmail.com>`. | `test_tc06_tc07_toggle_off_and_on` |
| TC-07 | P1 | Enable it again | Press `Off` in the same row. | Button reads `On`. Origin log top: `chore(watchlist): enable ...`. | `test_tc06_tc07_toggle_off_and_on` |

### Add

| ID | P | Title | Steps | Expected | Automated by |
|---|---|---|---|---|---|
| TC-08 | P1 | Add a watch with all fields | Open `Add a watch`; site `emag`, query `qa monitor 27`, target `999.5`, min drop `10`, tick `telegram`; press `Add`. | New row with `999.5`, `10`, `telegram`. Form is empty again. Origin log top: `chore(watchlist): add emag "qa monitor 27"`. | `test_tc08_add_watch_with_all_fields` |
| TC-09 | P2 | Duplicate query is refused | Add site `emag`, query `QA  Monitor 27` (other case, double space). | Under the form: `already watching emag "qa monitor 27" ...`. No new row. No commit. | `test_tc09_duplicate_add_refused` |
| TC-10 | P2 | Blank query is refused | Add with query of spaces only. | Under the form: `query must not be empty`. No commit. | `test_tc10_blank_query_refused` |
| TC-11 | P1 | Markup in a query stays text | Add query `<img src=x onerror=alert(1)>`. | The row shows the literal text. No image element in the row, no alert. | `test_tc11_markup_in_query_is_text` |

### Edit

| ID | P | Title | Steps | Expected | Automated by |
|---|---|---|---|---|---|
| TC-12 | P1 | Edit target price and channels | `Edit` on the TC-08 row; dialog shows `999.5`, `10`, `telegram` ticked. Set target `850`, tick `email` too; `Save`. | Dialog closes. Row shows `850` and `telegram, email`. Origin log top: `chore(watchlist): update emag "qa monitor 27"`. | `test_tc12_edit_price_and_channels` |
| TC-13 | P3 | Save with no change | `Edit`, then `Save` without changes. | Dialog stays open with `Nothing changed.` No commit. | `test_tc13_edit_without_change` |
| TC-14 | P3 | Cancel an edit | `Edit`, change the price, `Cancel`. | Dialog closes, row unchanged, no commit. | `test_tc14_edit_cancel` |
| TC-15 | P2 | Clear a price | `Edit`, empty the target price, `Save`. | Row shows `–` for target price. Commit `update`. | `test_tc15_clear_target_price` |
| TC-16 | P2 | Out-of-range value is refused | `Edit`, set min drop `0`, `Save`. | Dialog stays open with `min_drop_percent must be between 0 and 100`. No commit. | `test_tc16_out_of_range_refused_with_the_right_reason` |

### Remove

| ID | P | Title | Steps | Expected | Automated by |
|---|---|---|---|---|---|
| TC-17 | P1 | Cancel a remove | `Remove` on the TC-08 row, dismiss the confirm box. | Row stays. No commit. | `test_tc17_tc18_remove_cancel_then_confirm` |
| TC-18 | P1 | Confirm a remove | `Remove`, accept. | Row gone. Origin log top: `chore(watchlist): remove emag "qa monitor 27"`. | `test_tc17_tc18_remove_cancel_then_confirm` |

### Failure handling

| ID | P | Title | Steps | Expected | Automated by |
|---|---|---|---|---|---|
| TC-19 | P1 | Saved locally when origin is gone (runbook step 4) | Rename `origin.git` to `origin.off`; toggle a watch. | Yellow banner `Saved locally, not pushed ...` with `Retry push`. `Saved locally, waiting to be pushed` lists the edit. No new commit on origin. | `test_tc19_tc20_offline_save_then_retry` |
| TC-20 | P1 | Retry after origin returns (runbook step 4) | Rename it back; press `Retry push`. | Banner and pending list are gone. Origin log top has the toggle commit. | `test_tc19_tc20_offline_save_then_retry` |
| TC-21 | P1 | Conflict is not overwritten (runbook step 5) | Load the page. From `other`, change the first watch's target price and push. In the page, toggle that watch. | Red banner `This watch was changed somewhere else...` with `Reload`. Origin keeps the other clone's value; no dashboard commit. `Reload` shows the new price. | `test_tc21_conflict_not_overwritten` |
| TC-22 | P2 | Refresh picks up remote edits | From `other`, change a watch's min drop and push. Press `Refresh from GitHub`. | The row shows the new value without a page reload. | `test_tc22_refresh_shows_remote_edit` |

### Double submit

| ID | P | Title | Steps | Expected | Automated by |
|---|---|---|---|---|---|
| TC-25 | P3 | Double press of `Save` | `Edit`, change the target price, press `Save` twice quickly (double-click). | Dialog closes, row shows the new price. Exactly one `update` commit on origin. No banner, no pending entry, no conflict entry. (Two requests are sent; the second finds the edit already applied. See finding F-02.) | `test_tc25_double_press_save_makes_one_commit` |
| TC-26 | P3 | Changed price between two presses of `Save` | `Edit`, set the target price to `850`, press `Save`, then at once set `900` and press `Save` again (before the first answer arrives). | The first value wins: origin has one `update` commit and the price `850`. The second request is refused (HTTP 409): one red banner `This watch was changed somewhere else...` and the same refusal under `Edits that could not be applied`. Nothing queued. The `900` is lost. See finding F-07. | `test_tc26_changed_price_between_two_presses_of_save` |

### Display

| ID | P | Title | Steps | Expected | Automated by |
|---|---|---|---|---|---|
| TC-23 | P3 | Read-only settings | Open `not used yet` in a row. | Shows `cadence_minutes`, `owner`, `id`, and so on. The visible line shows `seller_policy`. No input for them. | `test_tc23_readonly_details` |
| TC-24 | P2 | No console errors | Watch the browser console during TC-01 to TC-23. | No uncaught errors. (Failed requests that the page reports in a banner are expected.) | every test, via the `console_errors` fixture |

## Not covered here

- A real GitHub remote (auth failures, network timeouts): covered by the
  `classify_failure` unit tests, not by this suite.
- Host-header and content-type guards: no page action can send a wrong Host;
  `tests/dashboard/test_server_guards.py` covers them through the API.
- Two dashboard processes on one clone (HTTP 423): covered by
  `test_dashboard_infra.py`.
