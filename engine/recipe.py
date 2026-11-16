"""
A recipe is the small, saved description of ONE field's text. It is plain data (JSON), checked when it is saved and
run when an order is built — never executed as code.

    {"parts": [{"src": "bl_number"}, {"src": "itos_no"}], "sep": "/"}        BL no./ITOS no.
    {"parts": [{"text": "Import"}]}                                          a fixed text
    {"parts": [{"src": "closing_date"}], "time": "10:00"}                    11/25/2026 10:00 (no "time" = date only)

    parts   what to take: {"src": <a name of catalog.SOURCE_VALUES>} or {"text": "..."}      (1 - 8 of them)
    mode    "join" (default): the parts that have a value, joined with `sep`;  "first": the first part that has one
    sep     the separator for "join" (default "")
    time    "HH:MM", added after every date part (a part whose source is a date is typed month/day/year, MM/dd/yyyy)
    case    "" | "upper" | "lower"
"""
import re

from engine import catalog
from helpers.convert import date_time

MODES = ("join", "first")
CASES = ("", "upper", "lower")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class MappingError(ValueError):
    """A mapping that cannot be saved or used; the message says what to fix."""


def _value(v, name: str):
    """One source value from the values object the builder gives a client function ("depot_out.date" = v.depot_out.date)."""
    obj = v
    for attr in name.split("."):
        obj = getattr(obj, attr)
    return "" if obj is None else obj


def render(recipe: dict | None, v) -> str:
    """The text this recipe gives for container values `v` ("" when it has no recipe or nothing to show)."""
    if not recipe:
        return ""
    time = recipe.get("time") or ""
    texts = []
    for part in recipe["parts"]:
        if "text" in part:
            texts.append(part["text"])
            continue
        raw = _value(v, part["src"])
        texts.append(date_time(raw, time) if catalog.VALUE_KIND[part["src"]] == "date" else str(raw))
    filled = [t for t in texts if t != ""]
    out = (filled[0] if filled else "") if recipe.get("mode") == "first" else (recipe.get("sep") or "").join(filled)
    return {"upper": out.upper(), "lower": out.lower()}.get(recipe.get("case") or "", out)


def check_recipe(key: str, recipe) -> list[str]:
    """Reasons this recipe is not valid for this field (none when it is)."""
    if key not in catalog.KEYS:
        return [f"{key}: not a field of the catalogue"]
    if not isinstance(recipe, dict):
        return [f"{key}: the recipe must be an object"]
    problems = []
    parts = recipe.get("parts")
    if not isinstance(parts, list) or not 1 <= len(parts) <= 8:
        return [f"{key}: it needs 1 to 8 parts"]
    for p in parts:
        if not isinstance(p, dict) or len(p) != 1 or next(iter(p)) not in ("src", "text"):
            problems.append(f"{key}: every part is either a source value or a fixed text")
        elif "src" in p and p["src"] not in catalog.VALUE_KIND:
            problems.append(f'{key}: "{p["src"]}" is not a value that can be chosen')
        elif "text" in p and (not isinstance(p["text"], str) or len(p["text"]) > 100):
            problems.append(f"{key}: a fixed text is at most 100 characters")
    if recipe.get("mode", "join") not in MODES:
        problems.append(f"{key}: mode must be one of {', '.join(MODES)}")
    sep = recipe.get("sep", "")
    if not isinstance(sep, str) or len(sep) > 5:
        problems.append(f"{key}: the separator is at most 5 characters")
    time = recipe.get("time", "")
    if time and not (isinstance(time, str) and _TIME.match(time)):
        problems.append(f'{key}: the time must look like "10:00"')
    if recipe.get("case", "") not in CASES:
        problems.append(f"{key}: case must be empty, upper or lower")
    if key in catalog.CHOICES and not problems:
        only = parts[0].get("text") if len(parts) == 1 else None
        if only not in catalog.CHOICES[key]:
            problems.append(f"{key}: pick one of the TMS values ({', '.join(catalog.CHOICES[key])})")
    extra = set(recipe) - {"parts", "mode", "sep", "time", "case"}
    if extra:
        problems.append(f"{key}: unknown setting(s) {', '.join(sorted(extra))}")
    return problems


DEFAULT_SETTINGS = {
    "same_for_all": ["container_type"],     # container values that must be equal on every container
    "depot_out_until_same": True,           # Depot Out line: Booked Until = Booked From
    "depot_in_until_same": False,           # Depot In line:  Booked Until = Booked From
    "others_enabled": False,                # fill the booked date on every other Lines row
    "others_until_same": True,              # ... and its Booked Until = Booked From
}


def check_settings(settings) -> list[str]:
    if not isinstance(settings, dict):
        return ["settings must be an object"]
    problems = [f"settings: unknown setting {k}" for k in settings if k not in DEFAULT_SETTINGS]
    for k, default in DEFAULT_SETTINGS.items():
        if k in settings and k != "same_for_all" and not isinstance(settings[k], bool):
            problems.append(f"settings: {k} must be true or false")
    same = settings.get("same_for_all", DEFAULT_SETTINGS["same_for_all"])
    if not isinstance(same, list) or any(s not in catalog.SAME_FOR_ALL_CHOICES for s in same):
        problems.append("settings: same_for_all can only list " + ", ".join(catalog.SAME_FOR_ALL_CHOICES))
    return problems


def check_mapping(rows: dict, settings: dict) -> list[str]:
    """Every reason this mapping cannot be saved (none when it is fine)."""
    problems = check_settings(settings)
    for key, recipe in rows.items():
        problems.extend(check_recipe(key, recipe))
    return problems
