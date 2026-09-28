"""T-38b (#49): per-retailer SKU adapters, versioned fingerprint, title
normalization and scored fuzzy fallback. Fixtures are real public listing
URLs/titles from data/price_history.json (no user data)."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from urllib.parse import urlparse

import pytest

from bf_price_monitor.identity import (
    IDENTITY_VERSION,
    MATCH_THRESHOLD,
    IdentityError,
    KnownProduct,
    extract_retailer_sku,
    match_titles,
    normalize_title,
    offer_fingerprint,
    resolve_offer,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE = json.loads(
    (REPO / "tests/fixtures/identity/real_offers.json").read_text(encoding="utf-8")
)

EMAG_ID = "DX1TPW3BM"
EMAG_URL = f"https://emag.ro/laptop-lenovo-v15-g4-amn/pd/{EMAG_ID}"


# -- adapters -----------------------------------------------------------------


@pytest.mark.parametrize("retailer", ["emag", "pcgarage", "flanco"])
def test_adapters_extract_the_stored_sku_for_real_fixture_urls(retailer):
    for offer in FIXTURE[retailer]:
        extraction = extract_retailer_sku(retailer, offer["url"])
        assert extraction.sku == offer["legacy_sku"]
        assert extraction.method == f"adapter:{retailer}"


def test_adapters_match_the_legacy_sku_for_every_url_in_history():
    # Compatibility guarantee: SQLite offer ids are uuid5(retailer:sku), so
    # an adapter that changed any existing SKU would split offer history.
    products = json.loads(
        (REPO / "data/price_history.json").read_text(encoding="utf-8")
    )["products"]
    assert products
    for url, product in products.items():
        legacy = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        assert extract_retailer_sku(product["site"], url).sku == legacy, url


@pytest.mark.parametrize(
    "alias",
    [
        f"https://www.emag.ro/laptop-lenovo-v15-g4-amn/pd/{EMAG_ID}/",
        f"https://www.emag.ro/renamed-slug-after-relisting/pd/{EMAG_ID}/?ref=hp_prod",
        f"https://emag.ro/laptop-lenovo-v15-g4-amn/pd/{EMAG_ID}/#reviews-section",
        f"https://www.emag.ro/laptop-lenovo-v15-g4-amn/pd/{EMAG_ID}/reviews",
    ],
)
def test_emag_url_aliases_resolve_to_one_offer(alias):
    base = offer_fingerprint("emag", extract_retailer_sku("emag", EMAG_URL).sku)
    assert offer_fingerprint("emag", extract_retailer_sku("emag", alias).sku) == base


def test_pcgarage_aliases_across_category_and_case_resolve_to_one_offer():
    slug = "156-vivobook-15-r1504va-fhd-procesor-intel-core-i3-1315u"
    urls = [
        f"https://pcgarage.ro/notebook-laptop/asus/{slug}",
        f"https://www.pcgarage.ro/notebook-laptop/asus/{slug}/",
        f"https://www.pcgarage.ro/laptopuri-reduse/asus/{slug.upper()}/",
    ]
    assert {extract_retailer_sku("pcgarage", u).sku for u in urls} == {slug}


def test_flanco_category_prefixes_are_stripped():
    page = "laptop-asus-vivobook-m1607ka-mb224-16-amd-ryzen-ai-5-330.html"
    urls = [
        f"https://flanco.ro/{page}",
        f"https://www.flanco.ro/laptop-it/laptop/{page}?utm_source=x",
    ]
    assert {extract_retailer_sku("flanco", u).sku for u in urls} == {page}


@pytest.mark.parametrize(
    ("retailer", "url"),
    [
        ("emag", "https://www.emag.ro/search/laptop"),
        ("emag", "https://www.emag.ro/laptop/pd/"),
        ("emag", "https://www.emag.ro/laptop/pd/bad id!/"),
        ("pcgarage", "https://www.pcgarage.ro/"),
        ("flanco", "https://www.flanco.ro/laptop-it/laptop"),
    ],
)
def test_urls_outside_the_retailer_pattern_are_rejected(retailer, url):
    with pytest.raises(IdentityError):
        extract_retailer_sku(retailer, url)


def test_unknown_retailer_uses_labelled_generic_fallback():
    extraction = extract_retailer_sku("altex", "https://altex.ro/laptop-x/cpd/LAP123/")
    assert (extraction.sku, extraction.method) == ("LAP123", "generic_last_segment")
    with pytest.raises(IdentityError):
        extract_retailer_sku("altex", "https://altex.ro/")


# -- fingerprint ----------------------------------------------------------------


def test_fingerprint_is_deterministic_versioned_and_matches_documented_formula():
    expected = hashlib.sha256(f"emag:{EMAG_ID}".encode()).hexdigest()
    fingerprint = offer_fingerprint("emag", EMAG_ID)
    assert fingerprint == f"{IDENTITY_VERSION}:{expected}"
    assert fingerprint == offer_fingerprint("emag", EMAG_ID)
    assert fingerprint.startswith("pid-v1:")


def test_same_sku_at_different_retailers_does_not_collide():
    assert offer_fingerprint("emag", "ABC123") != offer_fingerprint("flanco", "ABC123")


@pytest.mark.parametrize(("retailer", "sku"), [("", "x"), ("emag", ""), ("em:ag", "x")])
def test_fingerprint_rejects_ambiguous_inputs(retailer, sku):
    # A ':' in the retailer would let "a:b"+"c" collide with "a"+"b:c".
    with pytest.raises(IdentityError):
        offer_fingerprint(retailer, sku)


def test_all_fixture_fingerprints_are_unique():
    fingerprints = [
        offer_fingerprint(r, extract_retailer_sku(r, o["url"]).sku)
        for r, offers in FIXTURE.items()
        for o in offers
    ]
    assert len(set(fingerprints)) == len(fingerprints) == 12


# -- normalization ----------------------------------------------------------------


def test_romanian_diacritics_both_comma_and_cedilla_forms_fold():
    a = normalize_title("Laptop cu tastatură românească, ș ț ă î â")
    b = normalize_title("Laptop cu tastatura romaneasca, ş ţ a i a")
    assert a.tokens == b.tokens


def test_romanian_stopwords_and_listing_noise_are_removed():
    tokens = normalize_title(
        "Laptop cu procesor Intel pana la 4.3GHz pentru birou"
    ).tokens
    assert not tokens & {"cu", "procesor", "pana", "la", "pentru"}
    assert {"laptop", "intel", "4.3ghz", "birou"} <= tokens


def test_trademark_slug_artifacts_and_symbols_alias_to_plain_names():
    slugged = normalize_title("procesor amd ryzentm 5 intelr coretm windowsr 11")
    symbols = normalize_title("Procesor AMD Ryzen™ 5 Intel® Core™ Windows® 11")
    assert slugged.tokens == symbols.tokens
    assert {"ryzen", "intel", "core", "windows"} <= symbols.tokens


def test_units_decimals_and_colors_are_normalized():
    tokens = normalize_title('15,6" 16 GB RAM, 1 TB SSD, Negru').tokens
    assert {"15.6", "16gb", "1tb", "black"} <= tokens
    assert (
        normalize_title("Notebook Gri").tokens == normalize_title("laptop grey").tokens
    )


def test_model_codes_exclude_cpu_and_gpu_family_numbers():
    title = "Laptop ASUS Vivobook 15 X1504MA-BQ200 Intel Core i5-13420H RTX4060"
    assert normalize_title(title).model_codes == frozenset({"x1504ma"})


# -- fuzzy matching (real cross-retailer titles) ------------------------------

PCG_X1504MA = (
    "Laptop ASUS 15.6'' Vivobook 15 X1504MA, FHD, Procesor Intel® Core™ 3 304 "
    "(6M Cache, up to 4.30 GHz), 8GB DDR5, 512GB SSD, Intel Graphics, No OS, Blue"
)
FLANCO_X1504MA = (
    'Laptop Asus Vivobook 15 X1504MA-BQ200, 15.6" FHD, Intel Core 3 304, 8GB, '
    "512GB SSD, Free Dos, Quiet Blue"
)
FLANCO_E1504FA_R5 = (
    "Laptop ASUS Vivobook Go 15 E1504FA cu procesor AMD Ryzen 5 7520U pana la "
    "4.3GHz, 15.6'', Full HD, 8GB, 512GB SSD"
)
FLANCO_E1504FA_R3 = (
    'Laptop Asus Vivobook Go 15 E1504FA, 15.6", Full HD, AMD Ryzen 3 7320U, '
    "256GB SSD, 8GB RAM"
)


def test_same_configuration_across_retailers_is_accepted_with_confidence():
    result = match_titles(PCG_X1504MA, FLANCO_X1504MA)
    assert result.accepted
    assert result.score >= MATCH_THRESHOLD
    assert result.confidence in {"medium", "high"}
    assert "MODEL_CODE_MATCH" in result.reasons


def test_same_chassis_different_cpu_is_rejected():
    # Found on real data: the model code names a chassis family.
    result = match_titles(FLANCO_E1504FA_R5, FLANCO_E1504FA_R3)
    assert not result.accepted
    assert "SPEC_CONFLICT_CPU" in result.reasons
    assert "SPEC_CONFLICT_CAPACITY" in result.reasons


def test_unreadable_cpu_is_penalized_as_unverified():
    truncated = (
        "Laptop ASUS Vivobook Go 15 E1504FA, FHD, Procesor AMD Ryzen™ 5 40 (4M Cache"
    )
    result = match_titles(truncated, FLANCO_E1504FA_R5)
    assert "SPEC_UNVERIFIED_CPU" in result.reasons
    assert not result.accepted


def test_conflicting_model_codes_and_colors_score_low():
    result = match_titles(
        "Laptop Lenovo IdeaPad 82YU00SJRM Ryzen 5 7520U 16GB Negru",
        "Laptop Lenovo IdeaPad 83GW00ALRI Ryzen 5 7520U 16GB Argintiu",
    )
    assert {"MODEL_CODE_CONFLICT", "COLOR_CONFLICT"} <= set(result.reasons)
    assert result.confidence == "low"


def test_scores_are_bounded_and_symmetric():
    for a, b in [(PCG_X1504MA, FLANCO_X1504MA), ("", ""), ("x", FLANCO_E1504FA_R3)]:
        ab, ba = match_titles(a, b), match_titles(b, a)
        assert 0.0 <= ab.score <= 1.0
        assert ab.score == ba.score


# -- resolution ------------------------------------------------------------------


def _known(retailer, url, title, key="canon-1"):
    sku = extract_retailer_sku(retailer, url).sku
    return KnownProduct(key, retailer, title, {offer_fingerprint(retailer, sku)})


PCG_URL = "https://pcgarage.ro/notebook-laptop/asus/156-vivobook-15-x1504ma-fhd"
FLANCO_URL = "https://flanco.ro/laptop-asus-vivobook-15-x1504ma-bq200.html"


def test_exact_sku_wins_over_fuzzy(caplog):
    known = [_known("pcgarage", PCG_URL, PCG_X1504MA)]
    alias = PCG_URL.replace("https://", "https://www.") + "/"
    with caplog.at_level(logging.INFO, logger="bf_price_monitor.identity"):
        resolution = resolve_offer("pcgarage", alias, "totally different title", known)
    assert (resolution.method, resolution.canonical_key, resolution.confidence) == (
        "sku_exact",
        "canon-1",
        1.0,
    )
    assert "method=sku_exact" in caplog.text


def test_cross_retailer_fuzzy_match_is_logged_with_confidence_and_no_private_data(
    caplog,
):
    known = [_known("pcgarage", PCG_URL, PCG_X1504MA)]
    with caplog.at_level(logging.INFO, logger="bf_price_monitor.identity"):
        resolution = resolve_offer("flanco", FLANCO_URL, FLANCO_X1504MA, known)
    assert resolution.method == "fuzzy_title"
    assert resolution.canonical_key == "canon-1"
    assert MATCH_THRESHOLD <= resolution.confidence < 1.0
    assert any(r.startswith("CONFIDENCE_") for r in resolution.reasons)
    assert f"confidence={resolution.confidence:.4f}" in caplog.text
    assert resolution.offer_fingerprint in caplog.text
    for private in (FLANCO_URL, "flanco.ro/", "Vivobook", "X1504MA"):
        assert private not in caplog.text


def test_same_retailer_titles_are_never_fuzzy_merged():
    known = [_known("flanco", "https://flanco.ro/other-listing.html", FLANCO_X1504MA)]
    resolution = resolve_offer("flanco", FLANCO_URL, FLANCO_X1504MA, known)
    assert resolution.method == "new"
    assert resolution.canonical_key == resolution.offer_fingerprint


def test_below_threshold_creates_a_new_canonical_product():
    known = [_known("flanco", FLANCO_URL, FLANCO_E1504FA_R3)]
    resolution = resolve_offer(
        "pcgarage", PCG_URL, FLANCO_E1504FA_R5.replace("Asus", "ASUS"), known
    )
    assert resolution.method == "new"
    assert resolution.reasons == ("NO_MATCH",)
