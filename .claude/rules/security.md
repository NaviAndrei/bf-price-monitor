# Security and operational rules

Loads every session (no `paths`).

- **Least privilege:** Workflow scraping jobs must use read-only permissions (`permissions: {}`). Only persistence jobs may hold `contents: write`.
- **Action pinning:** All GitHub Actions references must be pinned to 40-character commit SHAs, never floating version tags (e.g., `actions/checkout@b4ffde... # v4.1.7`).
- **Secrets hygiene:** Never log full URLs containing tokens or webhook secrets. Mask tokens and UUIDs in log output. Never commit `.env` or temporary databases.
