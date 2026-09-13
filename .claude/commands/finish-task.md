---
description: Run test suites, commit changes as NaviAndrei, push to master, and mark task Done
---

The user wants to finalize and ship Issue #{{arg}}.

Follow this strict procedure:
1. **Verification Gate:**
   Run the full lint and test suites:
   ```powershell
   ruff check .
   ruff format --check .
   pytest -v
   ```
   If any check fails, stop immediately and report the error. Do not commit or push broken code.

2. **Commit on master:**
   Stage the modified files (never stage secrets, `.env`, or temporary files):
   ```powershell
   git status
   git add -u
   git commit --author="NaviAndrei <andrei.ivan1208@gmail.com>" -m "feat/fix: <concise summary> (Closes #{{arg}})"
   ```
   *Strict Rule:* Do NOT add `Co-Authored-By` or any AI attribution lines in the commit message.

3. **Push to remote:**
   Push directly to origin master:
   ```powershell
   git push origin master
   ```

4. **Update Project Status & Close Issue:**
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts/set_project_status.ps1 -IssueNumber {{arg}} -Status "Done"
   gh issue close {{arg}} --repo NaviAndrei/bf-price-monitor --comment "Completed via commit on master. All acceptance criteria verified."
   ```

5. **Log Progress:**
   Append a clean 2-line summary to `docs/progress.md` indicating that Issue #{{arg}} was completed, tests passed, and changes were pushed.