"""
Find the price increases nobody noticed, and put a dollar figure on each one.

The rule this module lives by: never report a rise the venue cannot act on, and
never describe a rise as something it is not. Produce prices bounce around with
the season and that is not a supplier problem. A promotional price ending is not
an increase. A one-off freight charge is not a price at all. Reporting those as
increases is how a tool loses a customer in week two.

    from creep.detect import analyse
    findings = analyse(lines)
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime

from creep.units import strip_pack_from_text

# A finding has to clear all of these to be worth showing someone.
MIN_OBSERVATIONS = 4          # fewer than this and it is noise
MIN_DAYS_SPAN = 60            # a rise needs time to be a trend
MIN_RISE_PCT = 0.05           # below 5% is not worth an email
MIN_ANNUAL_IMPACT = 50.0      # below $50 a year, nobody cares
VOLATILE_DOWN_SHARE = 0.30    # if this share of price moves are DOWN, it is seasonal
DEADBAND = 0.005              # price moves smaller than this are noise, not moves
WINDOW_DAYS = 75              # length of the early and late comparison windows
SHRINK_PASSTHROUGH = 0.5      # a shrink is "hidden" when less than half of it
                              # shows up as a visible price change
CROSS_MIN_IMPACT = 25.0       # switching suppliers is nearly free, so a smaller
                              # saving is still worth telling someone about
SWITCH_MIN_GAP = 0.05         # two suppliers must differ by this to be worth it
DESC_SIMILARITY = 0.72        # how alike two descriptions must be to be one product


@dataclass
class Finding:
    ref: str
    kind: str
    severity: str
    title: str
    detail: str
    annual_impact: float = 0.0
    action: str = ""
    evidence: list = field(default_factory=list)


@dataclass
class Observation:
    day: date
    unit_price: float
    ppu: float
    base_qty: float
    base_unit: str
    qty: float
    invoice_no: str | None = None


def _as_date(value):
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _norm_desc(text):
    return " ".join(sorted(strip_pack_from_text(text or "").lower().split()))


def _similar(a, b):
    """Standard library only. difflib is slower than rapidfuzz but this runs
    over a few hundred products, not a few million, and it is one less
    dependency for someone cloning the repo."""
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio()


def _money(x):
    return f"${x:,.0f}" if abs(x) >= 100 else f"${x:,.2f}"


def _per_unit(x, unit):
    # A cup costs 19 cents. Two decimal places turns every finding about
    # packaging into "$0.18 to $0.19", which reads as noise.
    return (f"${x:,.4f}/{unit}" if abs(x) < 1 else f"${x:,.2f}/{unit}")


# ---------------------------------------------------------------------------
# Build a price history per product
# ---------------------------------------------------------------------------
def build_products(lines):
    """`lines` are dicts with supplier, invoice_date, code, description,
    unit_price_ex_gst, qty, base_qty, base_unit, price_per_base_unit."""
    products = {}
    for l in lines:
        if not l.get("price_per_base_unit") or not l.get("base_unit"):
            continue
        supplier = (l.get("supplier") or "unknown").strip()
        code = (l.get("code") or "").strip().upper()
        key = (supplier, code) if code else (supplier, _norm_desc(l.get("description")))
        p = products.setdefault(key, {
            "supplier": supplier,
            "code": code or None,
            "description": l.get("description") or "",
            "base_unit": l["base_unit"],
            "observations": [],
        })
        p["observations"].append(Observation(
            day=_as_date(l["invoice_date"]),
            unit_price=float(l["unit_price_ex_gst"]),
            ppu=float(l["price_per_base_unit"]),
            base_qty=float(l["base_qty"]),
            base_unit=l["base_unit"],
            qty=float(l.get("qty") or 0),
            invoice_no=l.get("invoice_no"),
        ))
    for p in products.values():
        p["observations"].sort(key=lambda o: o.day)
    return products


def _windows(obs):
    """Median price at the start and at the end. Medians, not first and last,
    because one odd invoice should not become a finding."""
    first, last = obs[0].day, obs[-1].day
    early = [o for o in obs if (o.day - first).days <= WINDOW_DAYS]
    late = [o for o in obs if (last - o.day).days <= WINDOW_DAYS]
    if len(early) < 2 or len(late) < 2:
        half = max(2, len(obs) // 3)
        early, late = obs[:half], obs[-half:]
    return early, late


def _annual_base_units(obs):
    span = max((obs[-1].day - obs[0].day).days, 1)
    total = sum(o.qty * o.base_qty for o in obs)
    return total / span * 365.0


def _cv(values):
    if len(values) < 2:
        return 0.0
    mean = statistics.fmean(values)
    return statistics.pstdev(values) / mean if mean else 0.0


def _down_share(obs):
    """What share of the price movements were downwards.

    This is the honest test for "is this a rise or is this a market". A price
    that creeps up almost never falls. A seasonal price falls about as often as
    it rises. Spread alone cannot tell them apart; direction can."""
    moves = []
    for i in range(1, len(obs)):
        if obs[i - 1].ppu <= 0:
            continue
        change = obs[i].ppu / obs[i - 1].ppu - 1
        if abs(change) > DEADBAND:
            moves.append(change)
    if len(moves) < 3:
        return 0.0
    return sum(1 for m in moves if m < 0) / len(moves)


def _is_volatile(obs):
    return _down_share(obs) >= VOLATILE_DOWN_SHARE


def _pack_change(obs):
    """Find the point where the pack size changed, and the median price either
    side of it. Comparing across the whole period instead would let an
    unrelated price rise hide the shrink."""
    for i in range(1, len(obs)):
        before, after = obs[i - 1].base_qty, obs[i].base_qty
        if before and abs(after - before) / before > 0.01:
            pre = [o for o in obs[max(0, i - 6):i]]
            post = [o for o in obs[i:i + 6]]
            if len(pre) < 2 or len(post) < 2:
                continue
            return {
                "day": obs[i].day,
                "old_pack": statistics.median([o.base_qty for o in pre]),
                "new_pack": statistics.median([o.base_qty for o in post]),
                "old_price": statistics.median([o.unit_price for o in pre]),
                "new_price": statistics.median([o.unit_price for o in post]),
                "old_ppu": statistics.median([o.ppu for o in pre]),
                "new_ppu": statistics.median([o.ppu for o in post]),
            }
    return None


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------
def _check_product(key, p, ref):
    obs = p["observations"]
    if len(obs) < MIN_OBSERVATIONS:
        return None
    span = (obs[-1].day - obs[0].day).days
    if span < MIN_DAYS_SPAN:
        return None

    early, late = _windows(obs)
    base_ppu = statistics.median([o.ppu for o in early])
    now_ppu = statistics.median([o.ppu for o in late])
    if base_ppu <= 0:
        return None
    change = now_ppu / base_ppu - 1
    annual_units = _annual_base_units(obs)
    impact = (now_ppu - base_ppu) * annual_units
    unit = p["base_unit"]
    label = f"{p['description']}" + (f" ({p['code']})" if p["code"] else "")

    # Pack size changed while the price on the invoice barely moved.
    # This is the one a venue cannot see for itself.
    pc = _pack_change(obs)
    if pc and pc["new_pack"] < pc["old_pack"]:
        # How much smaller the pack got, and how much of that the supplier
        # actually put on the invoice as a price change. When the invoice shows
        # far less movement than the pack lost, the rest was taken quietly.
        shrink = 1 - pc["new_pack"] / pc["old_pack"]
        price_move = (abs(pc["new_price"] - pc["old_price"]) / pc["old_price"]
                      if pc["old_price"] else 1)
        if price_move < shrink * SHRINK_PASSTHROUGH:
            hidden = pc["new_ppu"] / pc["old_ppu"] - 1
            shrink_impact = (pc["new_ppu"] - pc["old_ppu"]) * annual_units
            return Finding(
                ref=ref, kind="pack_shrink", severity="high",
                title=f"{label}: pack shrank, price did not",
                detail=(f"On {pc['day'].isoformat()} the pack went from "
                        f"{pc['old_pack']:g}{unit} to {pc['new_pack']:g}{unit}, "
                        f"{shrink:.0%} less product, while the case price moved only "
                        f"{price_move:+.1%} ({_money(pc['old_price'])} to "
                        f"{_money(pc['new_price'])}). Per {unit} you are paying "
                        f"{hidden:+.0%} more, {_per_unit(pc['old_ppu'], unit)} to "
                        f"{_per_unit(pc['new_ppu'], unit)}. None of that shows up as a "
                        f"price increase anywhere on the invoice."),
                annual_impact=shrink_impact,
                action=(f"Ask {p['supplier']} to confirm the pack change in writing and to "
                        f"quote the old size. Your recipe costings probably still assume "
                        f"{pc['old_pack']:g}{unit} a pack."),
                evidence=[(o.day.isoformat(), o.base_qty, o.unit_price, o.ppu) for o in obs],
            )

    if change < MIN_RISE_PCT or impact < MIN_ANNUAL_IMPACT:
        return None

    # Seasonal produce swings both ways. Say so rather than calling it a rise.
    if _is_volatile(obs):
        lo = min(o.ppu for o in obs)
        hi = max(o.ppu for o in obs)
        return Finding(
            ref=ref, kind="volatile", severity="info",
            title=f"{label}: price moves around a lot",
            detail=(f"Between {_per_unit(lo, unit)} and {_per_unit(hi, unit)} over "
                    f"{span} days, and it has fallen on {_down_share(obs):.0%} of "
                    f"deliveries. Currently {_per_unit(now_ppu, unit)}. A price that "
                    f"falls this often is a market, not a supplier decision."),
            annual_impact=0.0,
            action="Worth a menu review if it stays high, not a supplier conversation.",
            evidence=[(o.day.isoformat(), o.ppu) for o in obs],
        )

    # A price that started low and then settled is a promotion ending, not a rise.
    rest = [o.ppu for o in obs if (o.day - obs[0].day).days > WINDOW_DAYS]
    if rest and len(rest) >= 3:
        rest_med = statistics.median(rest)
        rest_cv = _cv(rest)
        if base_ppu < rest_med * 0.92 and rest_cv < 0.05:
            return Finding(
                ref=ref, kind="promo_ended", severity="medium",
                title=f"{label}: an opening price ended",
                detail=(f"The first invoices were around {_per_unit(base_ppu, unit)}, "
                        f"and the price has been steady at {_per_unit(rest_med, unit)} "
                        f"since. This looks like an introductory or promotional rate "
                        f"expiring rather than a price rise."),
                annual_impact=impact,
                action=(f"Worth asking {p['supplier']} whether the opening rate can be "
                        f"extended or matched on volume."),
                evidence=[(o.day.isoformat(), o.ppu) for o in obs],
            )

    severity = "high" if (change >= 0.15 or impact >= 400) else "medium"
    return Finding(
        ref=ref, kind="creep", severity=severity,
        title=f"{label}: up {change:.0%} per {unit}",
        detail=(f"{_per_unit(base_ppu, unit)} to {_per_unit(now_ppu, unit)} over "
                f"{span} days, in {len(obs)} deliveries. At your usage of about "
                f"{annual_units:,.0f}{unit} a year that is {_money(impact)} a year."),
        annual_impact=impact,
        action=(f"Ask {p['supplier']} to justify the increase, and get a quote from one "
                f"other supplier before your next order."),
        evidence=[(o.day.isoformat(), o.ppu) for o in obs],
    )


def _check_supplier_wide(products, findings):
    """When one supplier lifts many products on the same day, that is one
    conversation to have, not eight separate ones.

    Two rules keep this honest. Seasonal products are excluded, because a
    produce market moves several lines on the same morning every week and that
    is not a supplier decision. And the new price has to stick: a jump that
    falls back the following week was a spot price, not an increase.
    """
    by_supplier = {}
    for (supplier, _code), p in products.items():
        obs = p["observations"]
        if len(obs) < MIN_OBSERVATIONS or _is_volatile(obs):
            continue
        for i in range(1, len(obs)):
            prev, cur = obs[i - 1], obs[i]
            if prev.ppu <= 0 or abs(cur.base_qty - prev.base_qty) > 1e-9:
                continue
            step = cur.ppu / prev.ppu - 1
            if step < 0.04:
                continue
            after = [o.ppu for o in obs[i:i + 4]]
            if len(after) < 2 or statistics.median(after) < cur.ppu * 0.97:
                continue          # it did not stick
            by_supplier.setdefault(supplier, {}).setdefault(cur.day, []).append(
                (p["description"], step))

    out = []
    for supplier, days in by_supplier.items():
        events = [(day, items) for day, items in days.items() if len(items) >= 3]
        if not events:
            continue
        # One supplier, one finding: the biggest event they ran.
        day, items = max(events, key=lambda e: (len(e[1]), statistics.fmean(
            s for _d, s in e[1])))
        avg = statistics.fmean(s for _d, s in items)
        out.append(Finding(
            ref=f"SW-{len(out) + 1}",
            kind="supplier_wide", severity="high",
            title=f"{supplier} lifted {len(items)} products at once on {day.isoformat()}",
            detail=(f"An average of {avg:+.1%} across {len(items)} lines on a single "
                    f"day, and the new prices held afterwards. An across-the-board "
                    f"increase is normally one negotiation rather than {len(items)}."),
            annual_impact=0.0,
            action=(f"Ask {supplier} for the written increase notice and whether your "
                    f"volume qualifies for the previous pricing."),
            evidence=[(d, f"{s:+.1%}") for d, s in sorted(items, key=lambda x: -x[1])],
        ))
    return out


def _check_cross_supplier(products):
    """The same thing bought from two suppliers at two prices."""
    groups = []
    items = [(k, p) for k, p in products.items() if len(p["observations"]) >= 2]
    for key, p in items:
        placed = False
        norm = _norm_desc(p["description"])
        for g in groups:
            if g["unit"] == p["base_unit"] and _similar(norm, g["norm"]) >= DESC_SIMILARITY:
                g["members"].append((key, p))
                placed = True
                break
        if not placed:
            groups.append({"norm": norm, "unit": p["base_unit"], "members": [(key, p)]})

    out = []
    for g in groups:
        suppliers = {p["supplier"] for _k, p in g["members"]}
        if len(suppliers) < 2:
            continue
        priced = []
        for _k, p in g["members"]:
            recent = [o.ppu for o in p["observations"][-4:]]
            annual = _annual_base_units(p["observations"])
            priced.append((p["supplier"], p["description"],
                           statistics.median(recent), annual))
        priced.sort(key=lambda r: r[2])
        cheapest, dearest = priced[0], priced[-1]
        if cheapest[2] <= 0:
            continue
        gap = dearest[2] / cheapest[2] - 1
        if gap < SWITCH_MIN_GAP:
            continue
        saving = (dearest[2] - cheapest[2]) * dearest[3]
        if saving < CROSS_MIN_IMPACT:
            continue
        unit = g["unit"]
        out.append(Finding(
            ref=f"CS-{len(out) + 1}",
            kind="cross_supplier", severity="medium",
            title=f"{dearest[1]}: {gap:.0%} cheaper from your other supplier",
            detail=(f"{cheapest[0]} charges {_per_unit(cheapest[2], unit)} and "
                    f"{dearest[0]} charges {_per_unit(dearest[2], unit)} for what looks "
                    f"like the same item. On the volume you buy from {dearest[0]}, the "
                    f"gap is {_money(saving)} a year."),
            annual_impact=saving,
            action=(f"Check the two products really are equivalent, then move volume or "
                    f"use the cheaper price to renegotiate."),
            evidence=[(s, d, _per_unit(ppu, unit), f"{a:,.0f}{unit}/yr")
                      for s, d, ppu, a in priced],
        ))
    return out


def _check_one_offs(products):
    out = []
    for _key, p in products.items():
        obs = p["observations"]
        if len(obs) == 1 and obs[0].unit_price >= 25:
            out.append(Finding(
                ref=f"OO-{len(out) + 1}",
                kind="one_off", severity="info",
                title=f"{p['description']}: charged once",
                detail=(f"{_money(obs[0].unit_price)} on {obs[0].day.isoformat()} from "
                        f"{p['supplier']}, and never before or since. Not a price rise, "
                        f"but worth knowing it was charged."),
                annual_impact=0.0,
                action="Check it was agreed.",
                evidence=[(obs[0].day.isoformat(), obs[0].unit_price)],
            ))
    return out


SEVERITY_RANK = {"high": 0, "medium": 1, "info": 2}


def analyse(lines):
    products = build_products(lines)

    findings = []
    for n, (key, p) in enumerate(sorted(products.items()), start=1):
        f = _check_product(key, p, ref=f"P-{n:03d}")
        if f:
            findings.append(f)

    findings += _check_supplier_wide(products, findings)
    findings += _check_cross_supplier(products)
    findings += _check_one_offs(products)

    findings.sort(key=lambda f: (SEVERITY_RANK[f.severity], -f.annual_impact))
    return findings, products


def total_impact(findings):
    return sum(f.annual_impact for f in findings
               if f.kind in {"creep", "pack_shrink", "cross_supplier", "promo_ended"})
