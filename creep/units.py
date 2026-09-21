"""
Turn a pack size written by a human into a number and a unit.

This is the heart of the product. A supplier can raise your price without
changing the price on the invoice, simply by putting less in the box. The only
way to see it is to reduce everything to price per litre, per kilo or per each,
which means reading "4 x 4L", "12.5KG", "CTN 500" and "6x750ml" correctly.

    parse_pack("4 x 4L")   -> (16.0, "L")
    parse_pack("2.5KG")    -> (2.5, "kg")
    parse_pack("CTN 500")  -> (500.0, "ea")
"""

from __future__ import annotations

import re

# Everything reduces to one of three base units.
BASE_UNITS = ("L", "kg", "ea")

# unit token -> (base unit, multiplier to reach the base unit)
UNIT_MAP = {
    "l": ("L", 1.0), "lt": ("L", 1.0), "ltr": ("L", 1.0), "litre": ("L", 1.0),
    "litres": ("L", 1.0), "liter": ("L", 1.0),
    "ml": ("L", 0.001), "mls": ("L", 0.001),
    "cl": ("L", 0.01),

    "kg": ("kg", 1.0), "kgs": ("kg", 1.0), "kilo": ("kg", 1.0),
    "kilos": ("kg", 1.0), "kilogram": ("kg", 1.0),
    "g": ("kg", 0.001), "gm": ("kg", 0.001), "gms": ("kg", 0.001),
    "gr": ("kg", 0.001), "gram": ("kg", 0.001), "grams": ("kg", 0.001),
    "mg": ("kg", 0.000001),

    "ea": ("ea", 1.0), "each": ("ea", 1.0), "ct": ("ea", 1.0),
    "pc": ("ea", 1.0), "pcs": ("ea", 1.0), "piece": ("ea", 1.0),
    "unit": ("ea", 1.0), "units": ("ea", 1.0), "pk": ("ea", 1.0),
    "pack": ("ea", 1.0), "sleeve": ("ea", 1.0), "roll": ("ea", 1.0),
    "rolls": ("ea", 1.0), "sht": ("ea", 1.0), "bag": ("ea", 1.0),
    "punnet": ("ea", 1.0), "tray": ("ea", 1.0), "bottle": ("ea", 1.0),
    "btl": ("ea", 1.0), "tin": ("ea", 1.0), "can": ("ea", 1.0),
    "jar": ("ea", 1.0), "doz": ("ea", 12.0), "dozen": ("ea", 12.0),
}

# Words that mean "a box of N" with no unit of their own
COUNT_WORDS = {"ctn", "carton", "case", "box", "crate", "cs"}

NUM = r"\d+(?:[.,]\d+)?"

# "4 x 4L", "6x750ml", "2 X 1 KG"
MULTI_RE = re.compile(
    rf"(?P<count>{NUM})\s*[x×*]\s*(?P<size>{NUM})\s*(?P<unit>[a-zA-Z]+)",
    re.IGNORECASE)
# "20L", "12.5KG", "500 G"
SINGLE_RE = re.compile(rf"(?P<size>{NUM})\s*(?P<unit>[a-zA-Z]+)", re.IGNORECASE)
# "CTN 500", "CASE 24"
COUNT_RE = re.compile(rf"(?P<word>[a-zA-Z]+)\s*(?P<count>\d+)", re.IGNORECASE)
# "500 CTN"
COUNT_REV_RE = re.compile(rf"(?P<count>\d+)\s*(?P<word>[a-zA-Z]+)", re.IGNORECASE)


def _num(text: str) -> float:
    return float(text.replace(",", ""))


def parse_pack(text: str | None) -> tuple[float | None, str | None]:
    """Return (quantity in base units, base unit) or (None, None).

    Returning None is a real answer. A pack size the parser does not understand
    must not be guessed, because a wrong denominator produces a confident wrong
    price per kilo, which is worse than no answer at all.
    """
    if not text:
        return None, None
    s = str(text).strip()
    if not s:
        return None, None

    # "4 x 4L" -> 16 L
    m = MULTI_RE.search(s)
    if m:
        unit = m.group("unit").lower()
        if unit in UNIT_MAP:
            base, mult = UNIT_MAP[unit]
            return round(_num(m.group("count")) * _num(m.group("size")) * mult, 6), base
        if unit in COUNT_WORDS:
            return round(_num(m.group("count")) * _num(m.group("size")), 6), "ea"
        # "6 x 500" with no unit at all - a count of things
        return round(_num(m.group("count")) * _num(m.group("size")), 6), "ea"

    # "CTN 500" -> 500 ea
    m = COUNT_RE.search(s)
    if m and m.group("word").lower() in COUNT_WORDS:
        return _num(m.group("count")), "ea"
    m = COUNT_REV_RE.search(s)
    if m and m.group("word").lower() in COUNT_WORDS:
        return _num(m.group("count")), "ea"

    # "20L", "12.5KG"
    m = SINGLE_RE.search(s)
    if m:
        unit = m.group("unit").lower()
        if unit in UNIT_MAP:
            base, mult = UNIT_MAP[unit]
            return round(_num(m.group("size")) * mult, 6), base

    # A bare number with no unit is a count
    if re.fullmatch(NUM, s):
        return _num(s), "ea"

    # "dozen", "each" on their own
    low = s.lower().strip()
    if low in UNIT_MAP:
        base, mult = UNIT_MAP[low]
        return mult, base

    return None, None


# Matches a pack size sitting inside a longer description, e.g.
# "Avocado Hass Class 1 5kg tray"
EMBEDDED_RE = re.compile(
    rf"(?:{NUM}\s*[x×*]\s*)?{NUM}\s*"
    r"(?:kgs?|kilos?|g|gm|gms|gr|grams?|l|lt|ltr|litres?|ml|mls|cl|ea|each|pk|pack|doz|dozen)"
    r"(?![a-zA-Z])",
    re.IGNORECASE)


def find_pack_in_text(text: str) -> str | None:
    """Pull a pack size out of a description when the invoice has no pack
    column. Takes the last match: 'Chicken 2 pc 2.5kg' means 2.5kg."""
    if not text:
        return None
    matches = list(EMBEDDED_RE.finditer(text))
    if not matches:
        return None
    return matches[-1].group(0).strip()


def strip_pack_from_text(text: str) -> str:
    """Remove pack sizes so two descriptions of the same product compare
    equal even when the size moved or changed."""
    return re.sub(r"\s+", " ", EMBEDDED_RE.sub(" ", text or "")).strip()
