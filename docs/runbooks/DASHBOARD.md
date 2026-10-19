# Watchlist dashboard runbook

The dashboard is a local web page for editing `data/watchlist.json`: add,
edit, enable, disable and remove watches. It runs on your machine only
(`127.0.0.1`), saves each edit as one commit authored by
`NaviAndrei <andrei.ivan1208@gmail.com>`, and pushes it. Design and reasons:
`docs/design/retailers-config.md` (Trap 2, Trap 4) and the 2026-10-17 and
2026-10-20 entries in `docs/DECISIONS.md`.

## 1. Install the server dependencies

The server needs the optional `dashboard` extra (FastAPI and uvicorn). The
`dev` extra includes it, so this is enough:

```powershell
uv sync --frozen --extra dev --extra anomaly
```

(`--extra anomaly` keeps the anomaly-pilot packages; a bare `--extra dev`
removes them.) The monitor workflow and the Docker image install without
extras and never see the server.

## 2. Smoke run against a scratch clone (nothing real is touched)

This builds a throwaway "origin" from a copy of your repository, so every
push goes to a folder in `%TEMP%`, never to GitHub.

```powershell
$scratch = Join-Path $env:TEMP "bf-dash-scratch"
git clone --bare D:\Projects\bf-price-monitor "$scratch\origin.git"
git clone "$scratch\origin.git" "$scratch\clone"
git -C "$scratch\clone" config user.email "andrei.ivan1208@gmail.com"
```

Start the server (run from `D:\Projects\bf-price-monitor`):

```powershell
$env:BF_DASHBOARD_CLONE = "$scratch\clone"
.\.venv\Scripts\python.exe -m bf_price_monitor.dashboard.server --port 8765
```

It prints one line to the terminal, `Dashboard: http://127.0.0.1:8765/#token=...`.
Open that exact address in your browser. The token is the part after
`token=`; it is new on every launch and is never written to a log. The page
removes it from the address bar after reading it. If you open the page
without the fragment, paste the token into the box at the top.

Check these by hand:

1. The table shows the watches and the badge says `clone ok`.
2. Toggle a watch off and on. After each click, look at the scratch origin:
   ```powershell
   git -C "$scratch\origin.git" log --format="%an <%ae> | %s" master -3
   ```
   Each toggle is one commit with a subject like
   `chore(watchlist): disable emag "laptop lenovo v15"`.
3. Add a watch, then edit its target price and channels.
4. Saved locally, not pushed: with the server running, move the origin
   away, make an edit, and confirm the yellow banner says
   `Saved locally, not pushed`:
   ```powershell
   Rename-Item "$scratch\origin.git" "origin.off"
   ```
   Move it back with `Rename-Item "$scratch\origin.off" "origin.git"` and
   press `Retry push`. The commit appears in the log above.
5. Conflict: edit a watch in the page without saving, change the same watch
   from another clone and push it, then save in the page. You get a red
   banner asking you to reload; nothing is overwritten.
6. Stop the server with Ctrl+C.

Clean up when done:

```powershell
Remove-Item -Recurse -Force "$scratch"
```

## 3. Use it for real

Make a dedicated clone. It must not be your development checkout
(`D:\Projects\bf-price-monitor`); the server refuses to start on it.

```powershell
git clone https://github.com/NaviAndrei/bf-price-monitor.git D:\Projects\bf-price-monitor-dashboard
$env:BF_DASHBOARD_CLONE = "D:\Projects\bf-price-monitor-dashboard"
.\.venv\Scripts\python.exe -m bf_price_monitor.dashboard.server
```

Settings (environment variables, all optional except the first):

| Variable | Meaning |
|---|---|
| `BF_DASHBOARD_CLONE` | Path of the dedicated clone (required) |
| `BF_DASHBOARD_GIT_NAME` | Commit author name (default `NaviAndrei`) |
| `BF_DASHBOARD_GIT_EMAIL` | Commit author email (default: `user.email` of the clone) |

## 4. What the page tells you

| You see | It means | What to do |
|---|---|---|
| `clone ok (detached @ abc1234)` | Normal. The clone detaches after each save on purpose. | Nothing |
| `Saved locally, not pushed (network)` | The edit is in the journal under `.git/bf-dashboard/`, not lost. | Fix the connection, press `Retry push` |
| `Saved locally, not pushed (auth)` | Git could not sign in to GitHub. | Fix credentials, press `Retry push` |
| `Saved locally, not pushed (rejected)` | Three pushes in a row lost a race. | Press `Retry push` |
| Red banner `changed somewhere else` | Another writer changed that watch first. | Press `Reload`, look, edit again |
| `Edits that could not be applied` | A queued edit conflicted after a reload. | Read the reason, then `Dismiss` |
| `clone problem` | The clone has uncommitted changes or is on another branch. | In the clone: `git restore --staged --worktree .` and `git switch --detach origin/master` |
| HTTP 423 / "clone busy" | Another dashboard process is using the clone. | Close the other one |

## 5. What the dashboard will not do

- It never runs `analyze.py` or `notify.py`, never reads secrets, and never
  force-pushes, rebases or hard-resets.
- It edits only `target_price`, `min_drop_percent`, `channels` and the
  on/off switch. `seller_policy`, `atl_policy` and `cooldown_hours` are
  shown read-only; `cadence_minutes`, `track_all_time_low`, `drop_rule`,
  `drop_threshold`, `owner` and `id` are under "not used yet". Site and query
  are fixed after creation; to change them, remove the watch and add a new one.
- Writes need the token. Reads do not, but every request must carry a
  `127.0.0.1:<port>` or `localhost:<port>` Host header.
