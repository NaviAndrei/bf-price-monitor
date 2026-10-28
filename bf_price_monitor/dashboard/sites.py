"""Sites and channels the dashboard offers.

``altex`` is deliberately absent: it is a placeholder scraper that never
returns products (scripts/scrape.py), so a watch on it could never fire.
``all`` is scrape.py's fan-out sentinel (FANOUT_SITE).
"""

from __future__ import annotations

KNOWN_SITES: tuple[str, ...] = ("emag", "pcgarage", "flanco", "itgalaxy", "all")
CHANNELS: tuple[str, ...] = ("telegram", "email", "ntfy")
