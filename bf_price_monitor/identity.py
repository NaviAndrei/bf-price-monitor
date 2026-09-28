"""Cross-store product identity (T-38b, #49).

Three layers, strongest first:

1. **Retailer SKU** — an explicit per-retailer adapter extracts the stable
   product id from the offer URL. Adapters are compatible with the SKU that
   ``scrape.py`` and ``migrate_history_to_sqlite._derive_sku`` already wrote
   to SQLite for every real URL, so SQLite offer ids (``uuid5(retailer:sku)``)
   do not churn.
2. **Offer fingerprint** — ``pid-v1:`` + sha256(``retailer:sku``):
   deterministic and versioned. Changing any rule that alters a SKU or the
   hash input requires a new ``IDENTITY_VERSION``.
3. **Fuzzy title match** — only a scored fallback for linking offers from
   *different* retailers. Every fuzzy result carries a confidence score and
   reason codes and is logged; it never overrides an exact SKU match.

Logs contain only retailer names, fingerprints, scores and reason codes: no
URLs, titles or user data.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

IDENTITY_VERSION = "pid-v1"
MATCH_THRESHOLD = 0.75
HIGH_CONFIDENCE = 0.85


class IdentityError(ValueError):
    """The URL does not match its retailer's product URL pattern."""


@dataclass(frozen=True)
class SkuExtraction:
    retailer: str
    sku: str
    method: str  # "adapter:<retailer>" or "generic_last_segment"


# -- 1. retailer SKU adapters ---------------------------------------------------


def _path_segments(url: str) -> list[str]:
    return [s for s in urlsplit(url).path.split("/") if s]


_EMAG_ID = re.compile(r"^[A-Z0-9]{6,16}$")


def _emag_sku(url: str) -> str:
    # https://www.emag.ro/<slug>/pd/<ID>/[...] -- the id after "pd" is stable
    # when eMAG renames the slug or appends tracking/review suffixes.
    segments = _path_segments(url)
    if "pd" not in segments:
        raise IdentityError("eMAG URL has no /pd/<id> segment")
    index = segments.index("pd")
    if index + 1 >= len(segments) or not _EMAG_ID.match(segments[index + 1]):
        raise IdentityError("eMAG URL has a malformed /pd/<id> segment")
    return segments[index + 1]


def _pcgarage_sku(url: str) -> str:
    # https://www.pcgarage.ro/<category>/<brand>/<slug>/ -- real PC Garage
    # URLs carry no numeric id, so the product slug is the identity; the
    # category/brand prefix is dropped so re-categorized listings still match.
    segments = _path_segments(url)
    if len(segments) < 2:
        raise IdentityError("PC Garage URL has no product slug")
    return segments[-1].lower()


def _flanco_sku(url: str) -> str:
    # https://www.flanco.ro/[category/...]/<slug>.html -- category prefixes
    # are dropped; the ".html" file name is kept verbatim because that is
    # the SKU already stored for every existing Flanco offer.
    segments = _path_segments(url)
    if not segments or not segments[-1].endswith(".html"):
        raise IdentityError("Flanco URL is not a <slug>.html product page")
    return segments[-1].lower()


SKU_ADAPTERS: dict[str, Callable[[str], str]] = {
    "emag": _emag_sku,
    "pcgarage": _pcgarage_sku,
    "flanco": _flanco_sku,
}


def extract_retailer_sku(retailer: str, url: str) -> SkuExtraction:
    """Explicit adapter for known retailers (raises IdentityError when the
    URL doesn't match that retailer's pattern); other retailers fall back to
    the historical last-path-segment rule, labelled as such."""
    adapter = SKU_ADAPTERS.get(retailer)
    if adapter is not None:
        return SkuExtraction(retailer, adapter(url), f"adapter:{retailer}")
    segments = _path_segments(url)
    if not segments:
        raise IdentityError("URL has no path to derive a SKU from")
    return SkuExtraction(retailer, segments[-1], "generic_last_segment")


# -- 2. fingerprint -----------------------------------------------------------


def offer_fingerprint(retailer: str, sku: str) -> str:
    if not retailer or not sku or ":" in retailer:
        raise IdentityError("retailer and sku must be non-empty; retailer without ':'")
    digest = hashlib.sha256(f"{retailer}:{sku}".encode()).hexdigest()
    return f"{IDENTITY_VERSION}:{digest}"


# -- 3. title normalization and fuzzy matching ----------------------------------

# Romanian function words (after diacritics are stripped) plus listing noise
# that carries no product identity.
ROMANIAN_STOPWORDS = frozenset(
    """a al ale ca care catre cea cel cu de din dupa este fara in la mai o pe
    pentru pana prin sau si spre sub sunt un una procesor""".split()
)

# Registered/trademark signs become a trailing "r"/"tm" in retailer slugs
# and some titles ("intelr", "ryzentm"); category and OS synonyms collapse.
TOKEN_ALIASES = {
    "intelr": "intel",
    "coretm": "core",
    "ryzentm": "ryzen",
    "radeontm": "radeon",
    "windowsr": "windows",
    "win": "windows",
    "notebook": "laptop",
    "freedos": "noos",
    "gray": "grey",
    "gri": "grey",
    "negru": "black",
    "alb": "white",
    "albastru": "blue",
    "argintiu": "silver",
    "auriu": "gold",
    "rosu": "red",
    "verde": "green",
    "roz": "pink",
    "mov": "purple",
}
COLORS = frozenset(
    {
        "black",
        "white",
        "blue",
        "silver",
        "grey",
        "gold",
        "red",
        "green",
        "pink",
        "purple",
    }
)
_UNITS = frozenset({"gb", "tb", "mb", "ghz", "mhz", "hz", "inch", "w", "mah"})
# Shared component families, not product models (many laptops share a CPU/GPU).
_COMPONENT_PREFIXES = ("rtx", "gtx", "rx", "mx", "arc", "ultra")
_MODEL_CODE = re.compile(r"^(?=(?:.*[a-z]){2})(?=(?:.*\d){2})[a-z0-9]{6,}$")


_MARKS = str.maketrans({"®": " ", "™": " ", "©": " ", "�": " "})


def _fold(text: str) -> str:
    # Trademark signs go first: NFKD would otherwise turn "Ryzen™" into
    # "ryzentm". Then NFKD + drop combining marks: ă/â->a, î->i, ș/ş->s, ț/ţ->t.
    decomposed = unicodedata.normalize("NFKD", text.translate(_MARKS).casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


@dataclass(frozen=True)
class NormalizedTitle:
    tokens: frozenset[str]
    model_codes: frozenset[str]
    colors: frozenset[str]


def normalize_title(title: str) -> NormalizedTitle:
    text = _fold(title)
    text = re.sub(r"(\d)[.,](\d)", r"\1.\2", text)  # keep 15,6 / 15.6 as one number
    raw = re.findall(r"[a-z0-9.]+", text)
    tokens: list[str] = []
    for token in raw:
        token = token.strip(".")
        if not token:
            continue
        token = TOKEN_ALIASES.get(token, token)
        if token in ROMANIAN_STOPWORDS:
            continue
        if token in _UNITS and tokens and re.fullmatch(r"\d+(\.\d+)?", tokens[-1]):
            tokens[-1] = tokens[-1] + token  # "16 gb" -> "16gb"
            continue
        tokens.append(token)
    token_set = frozenset(tokens)
    model_codes = frozenset(
        t
        for t in token_set
        if _MODEL_CODE.match(t) and not t.startswith(_COMPONENT_PREFIXES)
    )
    return NormalizedTitle(token_set, model_codes, token_set & COLORS)


# CPU model numbers as retailers write them: "Ryzen 5 7520U", "Ryzen R5-150",
# "Core 3 304" (no suffix on newer Intel/AMD parts), "i5-13420H", "N150".
# The trailing model number is the identity; the tier digit is redundant.
_CPU_PHRASE = re.compile(
    r"\b(?:core|ryzen|athlon|celeron|pentium)\s+(?:(?:ultra|ai)\s+)?"
    r"(?:r?\d|i\d)?[\s-]*(\d{3,5}[a-z]{0,2})\b"
    r"|\bi\d[\s-]+(\d{4,5}[a-z]{0,2})\b"
    r"|\bn(\d{2,3})\b"
)
_CAPACITY = re.compile(r"^\d+(gb|tb)$")


def _cpu_codes_from_text(title: str) -> frozenset[str]:
    text = _fold(title)
    return frozenset(
        next(g for g in m.groups() if g) for m in _CPU_PHRASE.finditer(text)
    )


def _capacities(tokens: frozenset[str]) -> frozenset[str]:
    return frozenset(t for t in tokens if _CAPACITY.match(t))


@dataclass(frozen=True)
class MatchResult:
    score: float
    confidence: str  # "high" | "medium" | "low"
    reasons: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return self.score >= MATCH_THRESHOLD


def match_titles(a: str, b: str) -> MatchResult:
    na, nb = normalize_title(a), normalize_title(b)
    union = na.tokens | nb.tokens
    jaccard = len(na.tokens & nb.tokens) / len(union) if union else 0.0
    reasons: list[str] = []
    if na.model_codes and nb.model_codes:
        if na.model_codes & nb.model_codes:
            score = 0.6 + 0.4 * jaccard
            reasons.append("MODEL_CODE_MATCH")
        else:
            score = 0.4 * jaccard
            reasons.append("MODEL_CODE_CONFLICT")
    else:
        score = 0.9 * jaccard
        reasons.append("TOKEN_JACCARD_ONLY")
    if na.colors and nb.colors and not (na.colors & nb.colors):
        score -= 0.2
        reasons.append("COLOR_CONFLICT")
    # A model code names a chassis family, not one configuration: on real
    # data "E1504FA ... Ryzen 5 7520U" and "E1504FA ... Ryzen 3 7320U" share
    # it, so conflicting CPU models or capacities must pull the score down.
    cpu_a, cpu_b = _cpu_codes_from_text(a), _cpu_codes_from_text(b)
    if cpu_a and cpu_b and not (cpu_a & cpu_b):
        score -= 0.3
        reasons.append("SPEC_CONFLICT_CPU")
    elif not (cpu_a and cpu_b):
        # One side's CPU can't be read (truncated or damaged title), so the
        # configuration is unverified: never enough on its own to accept.
        score -= 0.1
        reasons.append("SPEC_UNVERIFIED_CPU")
    cap_a, cap_b = _capacities(na.tokens), _capacities(nb.tokens)
    if cap_a and cap_b and cap_a != cap_b:
        score -= 0.15
        reasons.append("SPEC_CONFLICT_CAPACITY")
    score = round(max(0.0, min(1.0, score)), 4)
    confidence = (
        "high"
        if score >= HIGH_CONFIDENCE
        else "medium"
        if score >= MATCH_THRESHOLD
        else "low"
    )
    return MatchResult(score, confidence, tuple(reasons))


# -- resolution -------------------------------------------------------------------


@dataclass
class KnownProduct:
    canonical_key: str
    retailer: str
    title: str
    offer_fingerprints: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Resolution:
    offer_fingerprint: str
    retailer_sku: str
    canonical_key: str
    method: str  # "sku_exact" | "fuzzy_title" | "new"
    confidence: float
    reasons: tuple[str, ...]


def resolve_offer(
    retailer: str, url: str, title: str, known: Iterable[KnownProduct]
) -> Resolution:
    """Exact SKU fingerprint first; otherwise the best-scoring product from a
    *different* retailer above MATCH_THRESHOLD; otherwise a new canonical
    product keyed by this offer's fingerprint."""
    extraction = extract_retailer_sku(retailer, url)
    fingerprint = offer_fingerprint(retailer, extraction.sku)
    candidates = list(known)
    for product in candidates:
        if fingerprint in product.offer_fingerprints:
            resolution = Resolution(
                fingerprint,
                extraction.sku,
                product.canonical_key,
                "sku_exact",
                1.0,
                ("SKU_FINGERPRINT_MATCH", extraction.method.upper()),
            )
            _log(resolution, retailer)
            return resolution

    best: tuple[MatchResult, KnownProduct] | None = None
    for product in candidates:
        if product.retailer == retailer:
            continue  # same-store duplicates are distinct offers, not aliases
        result = match_titles(title, product.title)
        if result.accepted and (best is None or result.score > best[0].score):
            best = (result, product)
    if best is not None:
        result, product = best
        resolution = Resolution(
            fingerprint,
            extraction.sku,
            product.canonical_key,
            "fuzzy_title",
            result.score,
            (*result.reasons, f"CONFIDENCE_{result.confidence.upper()}"),
        )
    else:
        resolution = Resolution(
            fingerprint, extraction.sku, fingerprint, "new", 1.0, ("NO_MATCH",)
        )
    _log(resolution, retailer)
    return resolution


def _log(resolution: Resolution, retailer: str) -> None:
    logger.info(
        "identity method=%s confidence=%.4f reasons=%s retailer=%s fingerprint=%s",
        resolution.method,
        resolution.confidence,
        ",".join(resolution.reasons),
        retailer,
        resolution.offer_fingerprint,
    )
