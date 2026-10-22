---
name: manual-qa
description: Walk through the manual smoke checks of a runbook in the real browser, one numbered step at a time, and report PASS, FAIL or NOT RUN per step with evidence. Use for docs/runbooks/DASHBOARD.md steps 4 and 5 or any runbook with a numbered "check these by hand" list. Only runs against the local dashboard and the scratch clone.
argument-hint: <runbook path> <step numbers, e.g. 4 5>
disable-model-invocation: true
allowed-tools: Bash, Read, Glob, Grep, Write, mcp__Claude_Browser__navigate, mcp__Claude_Browser__computer, mcp__Claude_Browser__find, mcp__Claude_Browser__read_page, mcp__Claude_Browser__get_page_text, mcp__Claude_Browser__javascript_tool, mcp__Claude_Browser__read_console_messages, mcp__Claude_Browser__read_network_requests
---

# Manual QA walk-through

You run the manual checks a human would do, and you report what the screen and
the repository showed. You do not judge a step from memory or from the code.

## Hard limits
- Only the local dashboard on `127.0.0.1` and a scratch clone built under
  `%TEMP%`. Refuse any other URL or any clone that is not scratch.
- Never type the real token anywhere except the local page. The token comes from
  the server's terminal output in this session.
- Never click a control the runbook does not name. Never sign in, never change
  credentials, never download files.
- Writes go only to the scratch origin. Check its path before each save.

## Procedure
1. Read the runbook section named in the arguments. List the steps you will run
   and their expected results. Write nothing yet.
2. Confirm the scratch clone exists and the dashboard server is up. If either is
   missing, mark every step NOT RUN with the reason and stop. Do not build the
   setup yourself unless the runbook's own steps say to.
3. For each step, in order:
   - Do the action with the browser tools.
   - Read the result with `read_page` or `get_page_text`, not only a screenshot.
   - For a write step, read the scratch origin log with git (the runbook gives the
     command) and quote the subject line.
   - Mark PASS only when the observed text or log matches the expected result.
4. A step that cannot be run (no browser, server down, a click that does nothing)
   is NOT RUN, with the reason. Never mark it PASS.

## Report
- First line: `N PASS, N FAIL, N NOT RUN` for the steps named.
- Then one row per step: step number | result | what was observed (quoted text
  or the log subject) | what the runbook expected.
- List console errors seen during the walk-through, quoted, even on PASS.
- Say what was not checked (for example, the real GitHub clone).
