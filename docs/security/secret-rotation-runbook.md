# Secret rotation runbook

This repo uses four secrets, all stored as GitHub Actions repository
secrets (**Settings → Secrets and variables → Actions**) and consumed only
by `.github/workflows/monitor.yml`. This runbook covers what each one is,
where it lives, and the exact steps to revoke and reissue it.

Never write a real secret value into this file, an issue, a commit, or a
workflow log. Every example below uses an obvious placeholder such as
`<YOUR_NEW_TOKEN>`.

## First 15 minutes after suspected exposure

1. **Identify which secret leaked** (bot token, HF token, chat ID, or
   healthcheck URL) from whatever surfaced it — a Gitleaks alert, a public
   Actions log, a screenshot, etc.
2. **Revoke it at the issuer immediately**, before doing anything else in
   this document. Revocation is what stops the exposure; rotating the
   GitHub secret only updates what this repo uses going forward.
3. **Reissue and update the GitHub secret** using the per-secret steps
   below.
4. **Check for misuse** in the issuer's own activity/usage log (Telegram
   bot activity, Hugging Face token usage, Healthchecks.io ping history)
   for anything that isn't this repo's own `monitor.yml` schedule.
5. **Re-run `monitor.yml` manually** (`workflow_dispatch`) once the new
   secret is in place to confirm the pipeline still works end to end.

## `HF_TOKEN`

- **Used by:** `scripts/analyze.py`, passed only to
  `InferenceClient(api_key=...)` — never logged or printed.
- **Issuer:** Hugging Face → Settings → Access Tokens.
- **Scope needed:** a "read" token is sufficient (Serverless Inference API
  only); do not issue a "write" token for this use.

**Revoke and reissue:**
1. Go to https://huggingface.co/settings/tokens.
2. Find the token currently used by this repo and click **Revoke**.
3. Click **New token**, name it (e.g. `bf-price-monitor-monitor-yml`),
   select **Read**, and create it.
4. Copy the new value — Hugging Face only shows it once.

**Update the GitHub secret:**
```
gh secret set HF_TOKEN --repo NaviAndrei/bf-price-monitor --body "<YOUR_NEW_TOKEN>"
```

**Verify:** trigger `monitor.yml` manually and confirm the "Analyze alerts"
step completes without falling back to Ollama (a fallback there after a
rotation usually means the new token wasn't picked up or is scoped wrong).

## `TELEGRAM_BOT_TOKEN`

- **Used by:** `scripts/notify.py`, embedded directly in the Telegram Bot
  API URL path (`https://api.telegram.org/bot<TOKEN>/sendMessage`) — this
  is Telegram's own API shape, not something this repo can avoid. Treat any
  logged exception referencing that URL as a potential leak vector (see
  `_redact_secrets` in `scripts/notify.py`, added under T-17/#25).
- **Issuer:** [@BotFather](https://t.me/BotFather) on Telegram.

**Revoke and reissue:**
1. Message `@BotFather`, send `/mybots`, select the bot used by this repo.
2. Choose **API Token → Revoke current token**. This immediately
   invalidates the old token; the bot itself and its chat history are
   unaffected.
3. BotFather returns a new token in the same conversation. Copy it.

**Update the GitHub secret:**
```
gh secret set TELEGRAM_BOT_TOKEN --repo NaviAndrei/bf-price-monitor --body "<YOUR_NEW_TOKEN>"
```

**Verify:** trigger `monitor.yml` manually and confirm the "Send Telegram
alerts" step delivers a message (or exits cleanly with zero alerts) rather
than failing with a 401.

## `TELEGRAM_CHAT_ID`

- **Used by:** `scripts/notify.py`, as the destination chat for every
  alert. This is not a secret in the same sense as the other three — it
  identifies a chat, not a credential — but it's still worth keeping
  private, since it can be used to identify which chat/channel receives
  these alerts.
- **Issuer:** derived from your own Telegram account, not reissued by a
  third party. Rotation here means "point the bot at a different chat," not
  "revoke and replace."

**Re-derive:**
1. Message the bot (or add it to the target group/channel) so it has at
   least one message to read.
2. Visit `https://api.telegram.org/bot<CURRENT_TOKEN>/getUpdates` (do this
   in a browser you control, never paste the resulting URL anywhere) and
   read `message.chat.id` from the JSON response.

**Update the GitHub secret:**
```
gh secret set TELEGRAM_CHAT_ID --repo NaviAndrei/bf-price-monitor --body "<NEW_CHAT_ID>"
```

**Verify:** same as `TELEGRAM_BOT_TOKEN` above — confirm the next manual
run delivers to the intended chat.

## `HEALTHCHECK_URL`

- **Used by:** the "Ping healthcheck" step in `monitor.yml`'s `persist`
  job, as a bare capability URL (`https://hc-ping.com/<uuid>`). Per
  `README.md`'s own warning, anyone holding this URL can spoof successful
  runs or spam the check into a false "down" alert — there is no separate
  auth token to leak here, the URL *is* the credential.
- **Issuer:** [Healthchecks.io](https://healthchecks.io).

**Revoke and reissue:**
1. Log into Healthchecks.io and open the check used by this repo.
2. There is no "revoke in place" — the ping URL is derived from the
   check's UUID. Either delete and recreate the check (fastest, but you
   lose its ping history), or contact Healthchecks.io support if you need
   the UUID rotated without losing history.
3. If you recreate the check, set its schedule to match `monitor.yml`'s
   cron (`0 */2 * * *`, every 2 hours) as described in `README.md`.
4. Copy the new ping URL from the check's detail page.

**Update the GitHub secret:**
```
gh secret set HEALTHCHECK_URL --repo NaviAndrei/bf-price-monitor --body "<NEW_PING_URL>"
```

**Verify:** trigger `monitor.yml` manually and confirm the new check on
Healthchecks.io shows a successful ping ("Last Ping: a few seconds ago",
status Up).

## Post-rotation verification checklist

- [ ] Old credential/URL confirmed revoked or deleted at the issuer (not
      just replaced in GitHub).
- [ ] New value set via `gh secret set` (or the GitHub UI), not committed
      or pasted into an issue/PR/log anywhere.
- [ ] `monitor.yml` triggered manually (`workflow_dispatch`) and completed
      successfully end to end.
- [ ] Issuer's own activity/usage log checked for use that predates this
      rotation and doesn't match `monitor.yml`'s own schedule.
- [ ] If the leak was via a git commit (not just a log), confirm
      `pre-commit run --all-files` / the CI secret-scan step now flags that
      commit's content, and that the corresponding history has been
      purged or the repo's exposure otherwise accepted and documented.
