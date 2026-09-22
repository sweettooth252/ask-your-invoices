"""
Turn invoices into cost per drink.

For every drink, every month and every quarter:

    cost per serve = sum over the spec of  amount x price per unit paid

"Price per unit paid" comes from the purchases side of the semantic layer:
spend divided by quantity for the products behind that ingredient, in that
period. It is volume weighted, so if oat milk came from two suppliers, the
price is what was actually paid across both.

If an ingredient was not bought in a period, the last price paid is carried
forward and the row is marked, because a bar does not stop pouring tequila in
a month it did not reorder.
"""

from __future__ import annotations

import pandas as pd
import yaml

from askinv.db import ROOT
from askinv.layer import SemanticLayer
from creep.units import parse_pack

RECIPES = ROOT / "semantic" / "recipes.yml"


class RecipeError(Exception):
    pass


def load_recipes(path=RECIPES):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _amount(text):
    qty, unit = parse_pack(text)
    if qty is None:
        raise RecipeError(f"Can't read the amount '{text}' in a recipe.")
    return qty, unit


def ingredient_prices(sl: SemanticLayer, grain: str) -> pd.DataFrame:
    """Price per base unit for every product code, per period - read through
    the semantic layer, so it uses exactly the same definition of spend and
    quantity as every other answer in the app."""
    df = sl.query({"metrics": ["spend", "quantity"],
                   "dimensions": ["product_code", grain]})
    return df.rename(columns={grain: "period"})


def build_costs(sl: SemanticLayer, recipes=None):
    recipes = recipes or load_recipes()
    ingredients, drinks = recipes["ingredients"], recipes["drinks"]
    detail, summary = [], []

    for grain in ("month", "quarter"):
        prices = ingredient_prices(sl, grain)
        periods = sorted(prices["period"].unique())

        # per ingredient, per period: volume-weighted price, or carried forward
        ing_price = {}
        for name, spec in ingredients.items():
            codes = [str(c) for c in spec["products"]]
            rows = prices[prices["product_code"].isin(codes)]
            if rows.empty:
                raise RecipeError(f"Ingredient '{name}' matches no purchased product {codes}.")
            units = set(rows["unit"])
            if len(units) > 1:
                raise RecipeError(f"'{name}' mixes units {units} across its products.")
            unit = units.pop()
            last = None
            for p in periods:
                r = rows[rows["period"] == p]
                if len(r) and r["quantity"].sum() > 0:
                    last = (r["spend"].sum() / r["quantity"].sum(), False)
                    ing_price[(name, p)] = (last[0], unit, False)
                elif last is not None:
                    ing_price[(name, p)] = (last[0], unit, True)
            first = next(((n, p) for (n, p) in ing_price if n == name), None)
            if first:   # back-fill periods before the first purchase
                for p in periods:
                    if (name, p) not in ing_price:
                        ing_price[(name, p)] = (ing_price[first][0], unit, True)

        for drink, d in drinks.items():
            price_ex = round(d["price"] / 1.1, 4)
            for p in periods:
                total, carried_any = 0.0, False
                for ing, amount_text in d["spec"].items():
                    if ing not in ingredients:
                        raise RecipeError(f"{drink} uses unknown ingredient '{ing}'.")
                    ppu, unit, carried = ing_price[(ing, p)]
                    qty, qty_unit = _amount(amount_text)
                    y = ingredients[ing].get("yield")
                    if qty_unit != unit:
                        if y is None:
                            raise RecipeError(
                                f"{drink}: '{ing}' is used in {qty_unit} but bought in "
                                f"{unit}. Add a yield to recipes.yml.")
                        qty = qty / y          # e.g. litres of juice -> kilos of fruit
                    cost = qty * ppu
                    total += cost
                    carried_any = carried_any or carried
                    detail.append({"drink": drink, "drink_type": d["type"], "grain": grain,
                                   "period": p, "ingredient": ing,
                                   "amount_label": str(amount_text), "used_qty": qty,
                                   "unit": unit, "price_per_unit": ppu, "cost": cost,
                                   "carried_forward": int(carried)})
                summary.append({"drink": drink, "drink_type": d["type"], "grain": grain,
                                "period": p, "cost_per_serve": total,
                                "menu_price_inc_gst": d["price"],
                                "menu_price_ex_gst": price_ex,
                                "any_carried_forward": int(carried_any)})

    return pd.DataFrame(summary), pd.DataFrame(detail)


def explain_drink_change(sl: SemanticLayer, drink: str, before: str, after: str):
    """Why did this drink get dearer to make? The spec is fixed, so every cent
    of the change is an ingredient price moving - split by ingredient."""
    grain = "quarter" if "-Q" in before else "month"
    q = {"metrics": ["cost_per_serve"], "dimensions": [grain],
         "filters": [{"dimension": "drink", "value": drink},
                     {"dimension": grain, "op": "in", "value": [before, after]}]}
    lines = sl.lineage(q)
    if lines.empty:
        return None
    pivot = lines.pivot_table(index="ingredient", columns="period", values="cost",
                              aggfunc="sum")
    if before not in pivot or after not in pivot:
        return None
    out = pd.DataFrame({"ingredient": pivot.index,
                        "cost_before": pivot[before].values,
                        "cost_after": pivot[after].values})
    out["change"] = out["cost_after"] - out["cost_before"]
    out = out.sort_values("change", key=lambda s: -s.abs()).reset_index(drop=True)
    out.attrs.update(drink=drink, before=before, after=after,
                     total_before=float(out["cost_before"].sum()),
                     total_after=float(out["cost_after"].sum()))
    return out
