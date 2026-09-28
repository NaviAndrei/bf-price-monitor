# Product identity (T-38b, #49)

`bf_price_monitor/identity.py` decides when two listings are the same
product. It uses three layers, tried strongest first.

## 1. Retailer SKU adapters

Each retailer has an explicit adapter in `SKU_ADAPTERS`:

| Retailer | URL shape (from real history) | SKU |
|---|---|---|
| eMAG | `/<slug>/pd/<ID>[/...]` | `<ID>`, e.g. `DX1TPW3BM`; survives slug renames, `?ref=` and `/reviews` suffixes |
| PC Garage | `/<category>/<brand>/<slug>` | `<slug>`, lowercased. Real URLs have **no numeric id**, contrary to the `[slug]-[ID]` pattern assumed in #49 |
| Flanco | `/[category/...]/<slug>.html` | `<slug>.html`, lowercased; category prefixes are dropped |

A URL that doesn't fit its retailer's pattern raises `IdentityError`.
`scrape.py` (`offer_sku`) then falls back to the historical
last-path-segment rule and logs `URL_PATTERN_MISMATCH` with the fingerprint.
Retailers without an adapter use that same rule, labelled
`generic_last_segment`.

**Compatibility.** For all 307 URLs in `data/price_history.json`, every
adapter returns exactly the SKU that `_derive_sku` and the old `scrape.py`
code stored. SQLite offer ids (`uuid5("offer:<retailer>:<sku>")`) therefore
don't change, and a test enforces this against the full history file. This
replaces `_derive_sku` as the convention for live scraping. The one-off
migration script still uses `_derive_sku`, which gives identical results.

## 2. Versioned fingerprint

```
offer_fingerprint(retailer, sku) = "pid-v1:" + sha256(f"{retailer}:{sku}")
```

The fingerprint is deterministic, and the same SKU at two different
retailers never collides. A `:` in the retailer name is rejected, so two
different inputs can't produce the same hash string. Any change to an
adapter's output or to the hash input requires a new `IDENTITY_VERSION`
(`pid-v2`) and a migration plan for stored offer ids.

## 3. Fuzzy title matching (scored fallback only)

`match_titles(a, b)` is used only to link offers from **different**
retailers, and only when no exact fingerprint matches. It never merges two
listings from the same retailer. Each result carries a score from 0 to 1, a
confidence band (`high` at 0.85 or more, `medium` at 0.75 or more, otherwise
`low`), and reason codes. It is accepted only when the score is 0.75 or
more.

**Normalization** (`normalize_title`):
- Case folding and removal of Romanian diacritics, in both the comma and the
  cedilla forms: ș/ş, ț/ţ, ă, â, î.
- Removal of ™ ® © *before* Unicode folding. Otherwise "Ryzen™" would fold
  to "ryzentm".
- Removal of Romanian stopwords and listing noise such as `cu`, `pana`,
  `procesor`, `pentru`.
- Aliases for slug artifacts and synonyms: `intelr`→`intel`,
  `ryzentm`→`ryzen`, `notebook`→`laptop`, `negru`→`black`, `gri`→`grey`.
- Joined units (`16 GB`→`16gb`) and decimals (`15,6`→`15.6`).

**Scoring:**
- The base score is token Jaccard similarity.
- A shared model code (such as `x1504ma`; CPU and GPU family codes are
  excluded) adds a strong boost.
- Penalties apply for:
  - `MODEL_CODE_CONFLICT`: different model codes.
  - `COLOR_CONFLICT`: different colors.
  - `SPEC_CONFLICT_CPU`: different CPU model numbers, parsed from phrases
    like "Ryzen 5 7520U", "Core 3 304", "R5-150" or "i5-13420H".
  - `SPEC_CONFLICT_CAPACITY`: different RAM or storage sizes.
  - `SPEC_UNVERIFIED_CPU`: one side's CPU can't be read.

**Calibrated on real data.** Across all cross-retailer title pairs in
history, the first version accepted "E1504FA … Ryzen 5 7520U" and
"E1504FA … Ryzen 3 7320U" as the same product. The model code names a
chassis family, not a configuration, which is why the CPU and capacity
penalties exist. With them, 6 pairs are accepted. All six have the same
model code and the same CPU, and all are rated `medium`.

## Resolution and logging

`resolve_offer(retailer, url, title, known)` returns:
- `sku_exact`, with confidence 1.0, when the fingerprint matches a known
  offer;
- otherwise `fuzzy_title`, with its score, for the best match from another
  retailer;
- otherwise `new`, keyed by the offer's own fingerprint.

Every resolution is logged on the `bf_price_monitor.identity` logger as
method, confidence, reason codes, retailer and fingerprint. The log never
includes URLs, titles or user data.

## What is and isn't wired into production

- **Wired:** `scrape.py`'s SQLite write gets its SKU from the adapters,
  including the pattern-mismatch reason code.
- **Not wired:** `canonical_products` rows in SQLite are still keyed by
  title, the limitation inherited from T-19. Cross-retailer fuzzy links are
  not written to storage. `resolve_offer` is ready for that step, but
  changing product keys means migrating existing product ids, and that needs
  its own issue and decision.
