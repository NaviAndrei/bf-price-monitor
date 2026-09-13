---
description: Start work on a specific GitHub task issue, set status to In Progress, and plan
---

The user wants to start working on Issue #{{arg}}.

Follow this procedure:
1. Run the status update helper in PowerShell:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts/set_project_status.ps1 -IssueNumber {{arg}} -Status "In Progress"
   ```
2. Read the full issue description, acceptance criteria, and dependencies:
   ```powershell
   gh issue view {{arg}} --repo NaviAndrei/bf-price-monitor
   ```
3. Enter Plan mode. Inspect the affected files in the repo and produce a focused implementation plan that addresses all acceptance criteria.
4. State the planned file changes and test strategy, then ask for confirmation before writing code.