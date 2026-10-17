"""Operation-based watchlist edits (pure: no I/O, never mutates its input).

An operation carries the watch exactly as the user saw it (``expected``).
Applying it to the current document gives one of:

* applied          current == expected  -> the edit is made
* already applied  current == result    -> nothing to do (a replay whose push
                                           actually landed earlier)
* Conflict         anything else        -> somebody changed the watch first

Fields the form does not show (cadence_minutes, owner, atl_policy, unknown
keys, ...) are never rebuilt from form data: edits are applied to the current
watch dict, so they pass through untouched.
"""

from __future__ import annotations

import copy
import math
from typing import Any

from bf_price_monitor.dashboard.ids import duplicate_key, watch_id
from bf_price_monitor.dashboard.sites import CHANNELS, KNOWN_SITES

Op = dict[str, Any]
Doc = dict[str, Any]

APPLIED = "applied"
ALREADY_APPLIED = "already_applied"

DEFAULT_OWNER = "NaviAndrei"
MAX_QUERY_LENGTH = 200
MAX_PRICE = 10_000_000
EDITABLE_FIELDS = ("target_price", "min_drop_percent", "channels")


class OperationError(Exception):
    pass


class InvalidOperation(OperationError):
    """The operation itself is malformed or its values are out of range."""


class Conflict(OperationError):
    """The watch changed (or vanished) since the user looked at it."""


# --- field validation ------------------------------------------------------


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidOperation(f"{name} must be a number")
    if not math.isfinite(value):
        raise InvalidOperation(f"{name} must be a finite number")
    return float(value)


def _check_price(value: Any, name: str = "target_price") -> None:
    number = _number(value, name)
    if not 0 < number <= MAX_PRICE:
        raise InvalidOperation(f"{name} must be greater than 0")


def _check_percent(value: Any) -> None:
    number = _number(value, "min_drop_percent")
    if not 0 < number <= 100:
        raise InvalidOperation("min_drop_percent must be between 0 and 100")


def _check_channels(value: Any) -> None:
    if not isinstance(value, list) or not value:
        raise InvalidOperation("channels must be a non-empty list")
    if len(set(map(str, value))) != len(value):
        raise InvalidOperation("channels must not repeat")
    unknown = [c for c in value if c not in CHANNELS]
    if unknown:
        raise InvalidOperation(f"unknown channel: {unknown[0]!r}")


def _clean_query(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidOperation("query must be text")
    query = value.strip()
    if not query:
        raise InvalidOperation("query must not be empty")
    if len(query) > MAX_QUERY_LENGTH:
        raise InvalidOperation(f"query is longer than {MAX_QUERY_LENGTH} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in query):
        raise InvalidOperation("query must not contain control characters")
    return query


def _check_site(value: Any) -> None:
    if value not in KNOWN_SITES:
        raise InvalidOperation(f"unknown site: {value!r}")


def _check_edit_fields(fields: dict[str, Any]) -> None:
    for key, value in fields.items():
        if key == "target_price" and value is not None:
            _check_price(value)
        elif key == "min_drop_percent" and value is not None:
            _check_percent(value)
        elif key == "channels":
            _check_channels(value)


# --- constructors ----------------------------------------------------------


def make_add(
    site: str,
    query: str,
    *,
    target_price: Any = None,
    min_drop_percent: Any = None,
    channels: Any = None,
    owner: str = DEFAULT_OWNER,
) -> Op:
    _check_site(site)
    clean = _clean_query(query)
    watch: dict[str, Any] = {
        "id": watch_id(site, clean),
        "owner": owner,
        "site": site,
        "query": clean,
    }
    if target_price is not None:
        _check_price(target_price)
        watch["target_price"] = target_price
    if min_drop_percent is not None:
        _check_percent(min_drop_percent)
        watch["min_drop_percent"] = min_drop_percent
    watch.update(
        track_all_time_low=True,
        seller_policy="any",
        cadence_minutes=120,
        enabled=True,
    )
    if channels is not None:
        _check_channels(channels)
        watch["channels"] = list(channels)
    return {"op": "add", "watch": watch}


def _expected(watch: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(watch, dict) or not isinstance(watch.get("id"), str):
        raise InvalidOperation("expected watch must be an object with an id")
    return copy.deepcopy(watch)


def make_update(expected: dict[str, Any], changes: dict[str, Any]) -> Op:
    forbidden = sorted(set(changes) - set(EDITABLE_FIELDS))
    if forbidden:
        raise InvalidOperation(
            f"cannot change {forbidden[0]!r}; editable fields are "
            f"{', '.join(EDITABLE_FIELDS)} (enabled has its own operation, "
            "site and query are fixed after creation)"
        )
    _check_edit_fields(changes)
    base = _expected(expected)
    return {
        "op": "update",
        "id": base["id"],
        "expected": base,
        "changes": copy.deepcopy(changes),
    }


def make_set_enabled(expected: dict[str, Any], enabled: bool) -> Op:
    if not isinstance(enabled, bool):
        raise InvalidOperation("enabled must be true or false")
    base = _expected(expected)
    return {"op": "set_enabled", "id": base["id"], "expected": base, "enabled": enabled}


def make_delete(expected: dict[str, Any]) -> Op:
    base = _expected(expected)
    return {"op": "delete", "id": base["id"], "expected": base}


# --- inspection ------------------------------------------------------------


def check(op: Op) -> None:
    """Shape and value checks that need no document (also run on replay)."""
    kind = op.get("op")
    if kind == "add":
        watch = op.get("watch")
        if not isinstance(watch, dict):
            raise InvalidOperation("add needs a watch")
        _check_site(watch.get("site"))
        if _clean_query(watch.get("query")) != watch.get("query"):
            raise InvalidOperation("query must not have surrounding whitespace")
        if watch.get("id") != watch_id(watch["site"], watch["query"]):
            raise InvalidOperation("watch id does not match site and query")
        _check_edit_fields({k: watch[k] for k in EDITABLE_FIELDS if k in watch})
        return
    if kind in ("update", "set_enabled", "delete"):
        base = _expected(op.get("expected", {}))
        if op.get("id") != base["id"]:
            raise InvalidOperation("operation id does not match the expected watch")
        if kind == "update":
            changes = op.get("changes")
            if not isinstance(changes, dict) or set(changes) - set(EDITABLE_FIELDS):
                raise InvalidOperation("update changes are not editable fields")
            _check_edit_fields(changes)
        elif kind == "set_enabled" and not isinstance(op.get("enabled"), bool):
            raise InvalidOperation("enabled must be true or false")
        return
    raise InvalidOperation(f"unknown operation: {kind!r}")


def op_watch_id(op: Op) -> str:
    check(op)
    watch_id_value = op["watch"]["id"] if op["op"] == "add" else op["id"]
    return str(watch_id_value)


def describe(op: Op) -> str:
    """Commit subject: ``chore(watchlist): <verb> <site> "<query>"``."""
    check(op)
    kind = op["op"]
    if kind == "add":
        target = op["watch"]
        verb = "add"
    else:
        target = op["expected"]
        verb = {
            "update": "update",
            "delete": "remove",
            "set_enabled": "enable" if op.get("enabled") else "disable",
        }[kind]
    label = target.get("query") or target.get("direct_url") or target["id"]
    return f'chore(watchlist): {verb} {target.get("site", "?")} "{label}"'


# --- application -----------------------------------------------------------


def _patched(watch: dict[str, Any], op: Op) -> dict[str, Any]:
    out = copy.deepcopy(watch)
    if op["op"] == "update":
        for key, value in op["changes"].items():
            if value is None:
                out.pop(key, None)
            else:
                out[key] = copy.deepcopy(value)
    else:
        out["enabled"] = op["enabled"]
    return out


def apply(doc: Doc, op: Op) -> tuple[str, Doc]:
    check(op)
    new = copy.deepcopy(doc)
    watches: list[dict[str, Any]] = new["watches"]
    kind = op["op"]
    if kind == "add":
        return _apply_add(new, watches, op["watch"])

    wid = op["id"]
    index = next((i for i, w in enumerate(watches) if w.get("id") == wid), None)
    if kind == "delete":
        return _apply_delete(new, watches, index, op["expected"])
    if index is None:
        raise Conflict("this watch no longer exists")
    current = watches[index]
    if current == op["expected"]:
        # Patch the current dict (not a rebuilt one) so every field the form
        # does not show, and the key order, pass through untouched.
        watches[index] = _patched(current, op)
        return APPLIED, new
    if current == _patched(op["expected"], op):
        return ALREADY_APPLIED, new
    raise Conflict("this watch changed since it was shown")


def _apply_add(
    new: Doc, watches: list[dict[str, Any]], watch: dict[str, Any]
) -> tuple[str, Doc]:
    for existing in watches:
        if existing.get("id") == watch["id"]:
            if existing == watch:
                return ALREADY_APPLIED, new
            raise Conflict("a different watch with this id already exists")
    key = duplicate_key(watch["site"], watch["query"])
    for existing in watches:
        query = existing.get("query")
        if (
            isinstance(query, str)
            and duplicate_key(existing.get("site", ""), query) == key
        ):
            raise InvalidOperation(
                f'already watching {existing["site"]} "{query}" '
                "(queries match ignoring case and spacing)"
            )
    watches.append(copy.deepcopy(watch))
    return APPLIED, new


def _apply_delete(
    new: Doc,
    watches: list[dict[str, Any]],
    index: int | None,
    expected: dict[str, Any],
) -> tuple[str, Doc]:
    if index is None:
        return ALREADY_APPLIED, new
    if watches[index] != expected:
        raise Conflict("this watch changed since it was shown")
    del watches[index]
    return APPLIED, new
