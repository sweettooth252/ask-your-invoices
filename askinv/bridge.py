"""
Explain why spend changed between two periods.

    change = price effect + volume effect + new products + stopped products

For every product bought in both periods:
    price effect  = (new price per unit - old price per unit) x new quantity
    volume effect = (new quantity - old quantity) x old price per unit

Those two add up exactly to the change in spend on that product, so the whole
bridge reconciles to the cent. That is checked, not assumed.

Everything is read through the semantic layer, so a bridge and a normal answer
can never disagree about what "spend" means.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from askinv.layer import SemanticError, SemanticLayer


@dataclass
class Bridge:
    period_dimension: str
    before: str
    after: str
    spend_before: float
    spend_after: float
    price: float
    volume: float
    new: float
    stopped: float
    by_product: pd.DataFrame
    reconciles: bool
    filters: list

    @property
    def change(self):
        return self.spend_after - self.spend_before

    def top_drivers(self, n=3):
        df = self.by_product.copy()
        df["abs_total"] = df["total"].abs()
        return df.sort_values("abs_total", ascending=False).head(n)


def _period_frame(sl, period_dim, period, filters):
    q = {"metrics": ["spend", "quantity"],
         "dimensions": ["product", "supplier", "unit"],
         "filters": list(filters) + [{"dimension": period_dim, "op": "=", "value": period}]}
    df = sl.query(q)
    return df.set_index(["product", "supplier"])


def explain_change(sl: SemanticLayer, period_dim: str, before: str, after: str,
                   filters: list | None = None) -> Bridge:
    filters = filters or []
    if period_dim not in ("month", "quarter", "week"):
        raise SemanticError("A comparison needs periods: month, quarter or week.")
    known = set(sl.values(period_dim))
    for p in (before, after):
        if p not in known:
            raise SemanticError(f"There is no {period_dim} '{p}' in the data. "
                                f"Available: {', '.join(sorted(known))}.")

    a = _period_frame(sl, period_dim, before, filters)
    b = _period_frame(sl, period_dim, after, filters)

    rows = []
    for key in sorted(set(a.index) | set(b.index)):
        s1 = float(a.loc[key, "spend"]) if key in a.index else 0.0
        s2 = float(b.loc[key, "spend"]) if key in b.index else 0.0
        q1 = float(a.loc[key, "quantity"]) if key in a.index and pd.notna(a.loc[key, "quantity"]) else 0.0
        q2 = float(b.loc[key, "quantity"]) if key in b.index and pd.notna(b.loc[key, "quantity"]) else 0.0
        unit = (b.loc[key, "unit"] if key in b.index else a.loc[key, "unit"])

        price = volume = new = stopped = 0.0
        if key not in a.index:
            new = s2
        elif key not in b.index:
            stopped = -s1
        elif q1 > 0 and q2 > 0:
            p1, p2 = s1 / q1, s2 / q2
            price = (p2 - p1) * q2
            volume = (q2 - q1) * p1
        else:
            volume = s2 - s1        # no usable quantity: treat as volume

        rows.append({"product": key[0], "supplier": key[1], "unit": unit,
                     "spend_before": s1, "spend_after": s2,
                     "qty_before": q1, "qty_after": q2,
                     "price_before": s1 / q1 if q1 else None,
                     "price_after": s2 / q2 if q2 else None,
                     "price": price, "volume": volume, "new": new, "stopped": stopped,
                     "total": s2 - s1})

    df = pd.DataFrame(rows)
    total_before = float(df["spend_before"].sum())
    total_after = float(df["spend_after"].sum())
    parts = df[["price", "volume", "new", "stopped"]].sum()
    reconciles = abs(parts.sum() - (total_after - total_before)) < 0.01

    return Bridge(period_dim, before, after, total_before, total_after,
                  float(parts["price"]), float(parts["volume"]),
                  float(parts["new"]), float(parts["stopped"]),
                  df, reconciles, filters)
