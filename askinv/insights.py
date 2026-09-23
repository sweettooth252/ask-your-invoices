"""
The numbers a venue owner wants before they ask a question.

Everything here goes through the semantic layer or the warehouse, so a figure
on the dashboard and the same figure in a chat answer can never disagree - they
are the same definition, loaded from semantic/model.yml.

Nothing in this file talks to Streamlit. The pages in pages/ are only layout;
the arithmetic lives here, where tests can reach it.
"""

from __future__ import annotations

import re

import pandas as pd

from askinv.bridge import explain_change
from askinv.db import DB_PATH, connect
from askinv.layer import SemanticLayer
from askinv.menu import load_recipes

_QUARTER_LABEL = {"01": "Jan-Mar", "02": "Apr-Jun", "03": "Jul-Sep", "04": "Oct-Dec"}


# --------------------------------------------------------------------- periods
def periods(sl: SemanticLayer, grain: str = "quarter") -> list[str]:
    """Every period in the data, oldest first."""
    return sl.values(grain, "purchases")


def last_two(sl: SemanticLayer, grain: str = "quarter") -> tuple[str, str]:
    ps = periods(sl, grain)
    if len(ps) < 2:
        raise ValueError(f"Need at least two {grain}s of invoices.")
    return ps[-2], ps[-1]


# -------------------------------------------------------------------- headline
def headline(sl: SemanticLayer, before: str | None = None, after: str | None = None,
             grain: str = "quarter") -> dict:
    """The top of the dashboard: spend now vs then, and what it cost to pour.

    Returns plain numbers, not sentences, so the page decides the wording.
    """
    if before is None or after is None:
        before, after = last_two(sl, grain)

    def _spend(period):
        df = sl.query({"metrics": ["spend", "invoices", "products_bought"],
                       "filters": [{"dimension": grain, "value": period}]})
        return df.iloc[0]

    now, then = _spend(after), _spend(before)
    change = float(now["spend"]) - float(then["spend"])
    pct = change / float(then["spend"]) if then["spend"] else 0.0

    pour = sl.query({"metrics": ["pour_cost_pct"], "dimensions": ["drink_type"],
                     "filters": [{"dimension": grain, "value": after}]})
    pour_before = sl.query({"metrics": ["pour_cost_pct"], "dimensions": ["drink_type"],
                            "filters": [{"dimension": grain, "value": before}]})
    pour = pour.merge(pour_before, on="drink_type", suffixes=("", "_before"))
    pour["change_pts"] = (pour["pour_cost_pct"] - pour["pour_cost_pct_before"]) * 100

    b = explain_change(sl, grain, before, after)

    return {
        "grain": grain,
        "before": before,
        "after": after,
        "spend": float(now["spend"]),
        "spend_before": float(then["spend"]),
        "change": change,
        "change_pct": pct,
        "price_effect": b.price,
        "volume_effect": b.volume,
        "invoices": int(now["invoices"]),
        "products": int(now["products_bought"]),
        "pour_cost": pour,
        "bridge": b,
    }


def movers(sl: SemanticLayer, before: str, after: str, grain: str = "quarter",
           n: int = 5) -> pd.DataFrame:
    """The products that moved the venue's money the most, and why: a price
    change, or simply buying more or less of it."""
    b = explain_change(sl, grain, before, after)
    df = b.top_drivers(n).copy()
    df["reason"] = [("price" if abs(r["price"]) >= abs(r["volume"]) else "volume")
                    for _, r in df.iterrows()]
    return df


# ------------------------------------------------------------------- the menu
def menu_board(sl: SemanticLayer, period: str | None = None,
               grain: str = "quarter") -> pd.DataFrame:
    """Every drink: what it costs to pour, what it sells for, what's left."""
    period = period or periods(sl, grain)[-1]
    f = [{"dimension": grain, "value": period}]
    df = sl.query({"metrics": ["cost_per_serve", "menu_price", "margin_per_serve"],
                   "dimensions": ["drink", "drink_type"], "filters": f})
    pour = sl.query({"metrics": ["pour_cost_pct"], "dimensions": ["drink"], "filters": f})
    df = df.merge(pour, on="drink")
    return df.sort_values("pour_cost_pct", ascending=False).reset_index(drop=True)


def drink_cost_history(sl: SemanticLayer, drink: str, grain: str = "month") -> pd.DataFrame:
    return sl.query({"metrics": ["cost_per_serve"], "dimensions": [grain],
                     "filters": [{"dimension": "drink", "value": drink}]})


# ------------------------------------------------------------------ analytics
def spend_by(sl: SemanticLayer, dimensions, filters=None,
             metrics=("spend",)) -> pd.DataFrame:
    return sl.query({"metrics": list(metrics), "dimensions": list(dimensions),
                     "filters": filters or []})


def price_trend(sl: SemanticLayer, product: str, grain: str = "month") -> pd.DataFrame:
    """Price per litre or per kilo over time - the view that shows a pack-size
    change, which the invoice's own price column hides."""
    return sl.query({"metrics": ["avg_price_per_unit", "quantity"],
                     "dimensions": [grain, "unit"],
                     "filters": [{"dimension": "product", "value": product}]})


# --------------------------------------------------------------- data quality
def data_quality(sl: SemanticLayer) -> dict:
    con = connect(DB_PATH)
    try:
        inv = con.execute("SELECT COUNT(*) AS n, SUM(reconciles) AS ok, "
                          "MIN(business_date) AS first_day, MAX(business_date) AS last_day "
                          "FROM fct_invoice f JOIN dim_date d ON f.date_key = d.date_key").df().iloc[0]
        lines = con.execute("SELECT COUNT(*) AS n, "
                            "SUM(CASE WHEN price_per_base_unit IS NULL THEN 1 ELSE 0 END) AS no_unit_price, "
                            "SUM(CASE WHEN confidence < 1 THEN 1 ELSE 0 END) AS low_confidence "
                            "FROM fct_invoice_line").df().iloc[0]
        uncat = con.execute("SELECT COUNT(*) AS n FROM dim_product "
                            "WHERE category = 'Uncategorised'").df().iloc[0]["n"]
        carried = con.execute("SELECT AVG(any_carried_forward) AS share FROM fct_drink_cost "
                              "WHERE grain = 'quarter'").df().iloc[0]["share"]
        worst = con.execute("""
            SELECT s.supplier_name, COUNT(*) AS invoices,
                   SUM(CASE WHEN f.reconciles = 0 THEN 1 ELSE 0 END) AS not_reconciling
            FROM fct_invoice f JOIN dim_supplier s ON f.supplier_id = s.supplier_id
            GROUP BY 1 ORDER BY 3 DESC, 2 DESC""").df()
    finally:
        con.close()
    return {
        "invoices": int(inv["n"]),
        "reconciling": int(inv["ok"]),
        "first_day": str(inv["first_day"]),
        "last_day": str(inv["last_day"]),
        "lines": int(lines["n"]),
        "lines_without_unit_price": int(lines["no_unit_price"]),
        "low_confidence_lines": int(lines["low_confidence"]),
        "uncategorised_products": int(uncat),
        "carried_forward_share": float(carried or 0.0),
        "by_supplier": worst,
    }


def audit_trail(sl: SemanticLayer, limit: int = 200) -> pd.DataFrame:
    """Every number on every page can be traced back to one of these rows."""
    con = connect(DB_PATH)
    try:
        return con.execute(f"""
            SELECT f.invoice_no, d.business_date, s.supplier_name, f.source_file,
                   f.subtotal_ex_gst, f.lines_sum_ex_gst, f.reconciles,
                   COUNT(l.line_id) AS lines
            FROM fct_invoice f
            JOIN dim_supplier s ON f.supplier_id = s.supplier_id
            JOIN dim_date d ON f.date_key = d.date_key
            LEFT JOIN fct_invoice_line l ON l.invoice_no = f.invoice_no
            GROUP BY 1, 2, 3, 4, 5, 6, 7
            ORDER BY d.business_date DESC, f.invoice_no DESC
            LIMIT {int(limit)}""").df()
    finally:
        con.close()


# ---------------------------------------------------------------------- alerts
def _warehouse_lines() -> list[dict]:
    """The invoice lines in the shape creep.detect.analyse expects."""
    con = connect(DB_PATH)
    try:
        df = con.execute("""
            SELECT s.supplier_name AS supplier, d.business_date AS invoice_date,
                   p.code, p.product_name AS description, l.invoice_no,
                   l.qty, l.unit_price_ex_gst, l.base_unit, l.qty_base,
                   l.price_per_base_unit
            FROM fct_invoice_line l
            JOIN dim_supplier s ON l.supplier_id = s.supplier_id
            JOIN dim_product p ON l.product_id = p.product_id
            JOIN dim_date d ON l.date_key = d.date_key""").df()
    finally:
        con.close()
    df["base_qty"] = df["qty_base"] / df["qty"].replace(0, pd.NA)
    df["invoice_date"] = df["invoice_date"].astype(str)
    return df.dropna(subset=["price_per_base_unit", "base_unit"]).to_dict("records")


def products_to_drinks(recipes=None) -> dict[str, list[str]]:
    """Which drinks a product code ends up in, straight from the specs."""
    recipes = recipes or load_recipes()
    ingredients, drinks = recipes["ingredients"], recipes["drinks"]
    out: dict[str, list[str]] = {}
    for drink, spec in drinks.items():
        for ing in spec["spec"]:
            for code in ingredients.get(ing, {}).get("products", []):
                out.setdefault(str(code).upper(), []).append(drink)
    return {code: sorted(set(names)) for code, names in out.items()}


def alerts(limit: int | None = None, with_drinks: bool = True) -> pd.DataFrame:
    """What changed that someone should act on.

    The detector is the one from Price Creep: it ignores seasonal produce, one-off
    freight charges and moves too small to matter, so what's left is worth an
    email to a supplier. Each finding is then matched to the drinks it hits.
    """
    from creep.detect import analyse       # imported here to keep import cost off pages

    findings, _products = analyse(_warehouse_lines())
    by_code = products_to_drinks() if with_drinks else {}

    rows = []
    for f in findings[: limit or len(findings)]:
        # The detector writes the product code into its title, e.g.
        # "Cafe Noir Coffee Liqueur (HS-CFL-07): up 30% per L".
        m = re.search(r"\(([A-Za-z0-9][\w-]*)\)", f.title)
        code = (m.group(1).upper() if m else "")
        rows.append({
            "severity": f.severity,
            "kind": f.kind,
            "title": f.title,
            "detail": f.detail,
            "annual_impact": round(f.annual_impact, 2),
            "action": f.action,
            "drinks_affected": ", ".join(by_code.get(code, [])) or "-",
            "evidence": f.evidence,
        })
    return pd.DataFrame(rows)


def recipe_impact(sl: SemanticLayer, before: str, after: str,
                  grain: str = "quarter") -> pd.DataFrame:
    """Which drinks got dearer to make between two periods, and by how much."""
    f_before = sl.query({"metrics": ["cost_per_serve"], "dimensions": ["drink", "drink_type"],
                         "filters": [{"dimension": grain, "value": before}]})
    f_after = sl.query({"metrics": ["cost_per_serve"], "dimensions": ["drink", "drink_type"],
                        "filters": [{"dimension": grain, "value": after}]})
    df = f_before.merge(f_after, on=["drink", "drink_type"], suffixes=("_before", "_after"))
    df["change"] = df["cost_per_serve_after"] - df["cost_per_serve_before"]
    df["change_pct"] = df["change"] / df["cost_per_serve_before"]
    return df.sort_values("change_pct", ascending=False).reset_index(drop=True)


# ----------------------------------------------------------------- the inbox
def latest_prices() -> pd.DataFrame:
    """The last price paid for every product, with the date and invoice it came
    from. This is what a new invoice gets checked against."""
    con = connect(DB_PATH)
    try:
        return con.execute("""
            SELECT p.code, p.product_name, p.category, l.base_unit,
                   l.price_per_base_unit AS last_price, d.business_date AS last_seen,
                   l.invoice_no AS last_invoice
            FROM fct_invoice_line l
            JOIN dim_product p ON l.product_id = p.product_id
            JOIN dim_date d ON l.date_key = d.date_key
            JOIN (SELECT product_id, MAX(date_key) AS date_key
                  FROM fct_invoice_line GROUP BY product_id) m
              ON m.product_id = l.product_id AND m.date_key = l.date_key
            GROUP BY 1, 2, 3, 4, 5, 6, 7""").df()
    finally:
        con.close()


def check_invoice(invoice, history: pd.DataFrame | None = None) -> pd.DataFrame:
    """Read a freshly delivered invoice against everything bought before it.

    Each line comes back as one of: a price that held, a price that moved (with
    the percentage), or a product never bought before. Nothing is written to the
    warehouse - this is the check you do before you pay.
    """
    hist = latest_prices() if history is None else history
    by_code = {str(r["code"]).upper(): r for _, r in hist.iterrows()}

    rows = []
    for line in invoice.lines:
        code = (line.code or "").upper()
        prev = by_code.get(code)
        ppu = line.price_per_base_unit
        change = None
        if prev is not None and prev["last_price"] and ppu:
            change = ppu / float(prev["last_price"]) - 1
        if prev is None:
            verdict = "new product"
        elif change is None:
            verdict = "no unit price"
        elif abs(change) < 0.005:
            verdict = "unchanged"
        elif change > 0:
            verdict = "price up"
        else:
            verdict = "price down"
        rows.append({
            "line": line.line_no,
            "code": line.code,
            "description": line.description,
            "pack": line.pack_raw,
            "qty": line.qty,
            "unit_price_ex_gst": line.unit_price_ex_gst,
            "amount_ex_gst": line.amount_ex_gst,
            "per_base_unit": ppu,
            "unit": line.base_unit,
            "last_price": None if prev is None else float(prev["last_price"]),
            "change_pct": change,
            "verdict": verdict,
            "last_seen": None if prev is None else prev["last_seen"],
        })
    df = pd.DataFrame(rows)
    lines_total = round(float(df["amount_ex_gst"].fillna(0).sum()), 2)
    df.attrs["invoice_no"] = invoice.invoice_no
    df.attrs["supplier"] = invoice.supplier_name
    df.attrs["invoice_date"] = invoice.invoice_date
    df.attrs["subtotal_ex_gst"] = invoice.subtotal_ex_gst
    df.attrs["lines_sum_ex_gst"] = lines_total
    df.attrs["reconciles"] = bool(invoice.subtotal_ex_gst is not None
                                  and abs(lines_total - invoice.subtotal_ex_gst) < 0.02)
    return df


def drinks_hit_by(codes, recipes=None) -> dict[str, list[str]]:
    """Given product codes from an invoice, which drinks they feed into."""
    mapping = products_to_drinks(recipes)
    return {str(c).upper(): mapping.get(str(c).upper(), []) for c in codes}


def bridge_steps(b) -> pd.Series:
    """The four reasons a spend changed, as a chartable series. They add to the
    change exactly; that is what makes the chart worth showing."""
    return pd.Series({"Price": b.price, "Volume": b.volume,
                      "New products": b.new, "Stopped buying": b.stopped})


# --------------------------------------------------------------- the recipes
def recipe_library(recipes=None) -> pd.DataFrame:
    """Every drink's spec, as a table: the bar's spec book, readable at a glance."""
    recipes = recipes or load_recipes()
    rows = []
    for drink, d in recipes["drinks"].items():
        makes = float((d.get("batch") or {}).get("makes", 1) or 1)
        for ing, amount in d["spec"].items():
            rows.append({"drink": drink, "type": d["type"],
                         "menu_price_inc_gst": d["price"], "serves_per_batch": makes,
                         "ingredient": ing, "amount": str(amount)})
    return pd.DataFrame(rows)


def price_book(sl: SemanticLayer, period: str | None = None, grain: str = "quarter",
               recipes=None) -> dict:
    """Ingredient -> (price per unit paid, unit, carried forward?) for one period."""
    from askinv.menu import price_map, prices_at

    recipes = recipes or load_recipes()
    ing_price, ps = price_map(sl, grain, recipes["ingredients"])
    period = period or ps[-1]
    return prices_at(ing_price, period, recipes["ingredients"])


def cost_of_spec(spec: dict, book: dict, makes: float = 1.0, recipes=None,
                 label: str = "drink") -> tuple[float, pd.DataFrame]:
    """What would this spec cost to pour, at those prices?

    Used by the what-if editor: change 45ml of tequila to 40ml and the number
    moves, through exactly the same arithmetic the dashboard uses.
    """
    from askinv.menu import cost_spec

    recipes = recipes or load_recipes()
    total, rows, _carried = cost_spec(spec, recipes["ingredients"], book,
                                      makes=makes, label=label)
    return total, pd.DataFrame(rows)


def pour_cost(cost_per_serve: float, menu_price_inc_gst: float) -> float:
    """Cost ÷ menu price excluding GST. The menu price includes GST; ingredient
    costs don't, so the 1.1 has to come off before they're compared."""
    ex = menu_price_inc_gst / 1.1
    return cost_per_serve / ex if ex else float("nan")


def price_for_target_pour_cost(cost_per_serve: float, target: float = 0.20) -> float:
    """The menu price (including GST) this drink needs to hit a target pour cost."""
    return (cost_per_serve / target) * 1.1 if target else float("nan")


# ------------------------------------------------- did we get what we paid for
# Invoices alone cannot prove a delivery happened - that needs a docket someone
# signed at the door. What invoices CAN show is billing that doesn't look right:
# the same delivery billed twice, and a product that has stopped arriving.
def duplicate_invoices(tolerance_days: int = 3) -> pd.DataFrame:
    """Invoices that look like the same delivery billed twice.

    Two tests: the same invoice number twice, and the same supplier billing the
    same amount on the same day (or within a few days) under different numbers.
    A re-issued invoice reconciles perfectly on both pages, so nothing but a
    comparison catches it.
    """
    con = connect(DB_PATH)
    try:
        df = con.execute("""
            SELECT f.invoice_no, s.supplier_name, d.business_date, f.subtotal_ex_gst,
                   f.source_file
            FROM fct_invoice f
            JOIN dim_supplier s ON f.supplier_id = s.supplier_id
            JOIN dim_date d ON f.date_key = d.date_key
            ORDER BY s.supplier_name, d.business_date""").df()
    finally:
        con.close()

    df["business_date"] = pd.to_datetime(df["business_date"])
    rows = []

    repeated = df[df.duplicated("invoice_no", keep=False)]
    for no, grp in repeated.groupby("invoice_no"):
        rows.append({"kind": "same invoice number", "supplier": grp["supplier_name"].iloc[0],
                     "invoices": ", ".join(grp["invoice_no"]), "amount_ex_gst": float(grp["subtotal_ex_gst"].iloc[0]),
                     "dates": ", ".join(grp["business_date"].dt.strftime("%Y-%m-%d")),
                     "at_risk": float(grp["subtotal_ex_gst"].iloc[1:].sum())})

    for (supplier, amount), grp in df.groupby(["supplier_name", "subtotal_ex_gst"]):
        if len(grp) < 2:
            continue
        grp = grp.sort_values("business_date")
        gaps = grp["business_date"].diff().dt.days
        close = grp[(gaps <= tolerance_days) | (gaps.shift(-1) <= tolerance_days)]
        if len(close) >= 2 and close["invoice_no"].nunique() > 1:
            rows.append({"kind": "same amount, same days", "supplier": supplier,
                         "invoices": ", ".join(close["invoice_no"]),
                         "amount_ex_gst": float(amount),
                         "dates": ", ".join(close["business_date"].dt.strftime("%Y-%m-%d")),
                         "at_risk": float(amount) * (len(close) - 1)})

    out = pd.DataFrame(rows)
    return out.sort_values("at_risk", ascending=False).reset_index(drop=True) if len(out) else out


def delivery_rhythm(min_deliveries: int = 4, overdue_factor: float = 2.5,
                    min_days: int = 21) -> pd.DataFrame:
    """Products that used to arrive on a rhythm and have stopped.

    A bar doesn't stop ordering milk. If something that came every few days
    hasn't come for weeks, either it was dropped on purpose or an order was
    missed - and the person who would know is the one this table is for.
    """
    con = connect(DB_PATH)
    try:
        df = con.execute("""
            SELECT p.product_name, p.code, s.supplier_name, d.business_date
            FROM fct_invoice_line l
            JOIN dim_product p ON l.product_id = p.product_id
            JOIN dim_supplier s ON l.supplier_id = s.supplier_id
            JOIN dim_date d ON l.date_key = d.date_key
            GROUP BY 1, 2, 3, 4""").df()
    finally:
        con.close()

    df["business_date"] = pd.to_datetime(df["business_date"])
    as_at = df["business_date"].max()

    rows = []
    for (product, code, supplier), grp in df.groupby(["product_name", "code", "supplier_name"]):
        days = grp["business_date"].sort_values()
        if len(days) < min_deliveries:
            continue
        typical = float(days.diff().dt.days.median())
        last = days.iloc[-1]
        since = int((as_at - last).days)
        if typical > 0 and since > max(typical * overdue_factor, min_days):
            rows.append({"product": product, "code": code, "supplier": supplier,
                         "usual_gap_days": round(typical, 1), "last_delivery": last.date(),
                         "days_since": since, "deliveries": len(days),
                         "overdue_by": round(since / typical, 1)})
    out = pd.DataFrame(rows)
    return (out.sort_values("overdue_by", ascending=False).reset_index(drop=True)
            if len(out) else out)


def claim_value(lines: pd.DataFrame, received: dict) -> pd.DataFrame:
    """Cost what a short delivery is worth, line by line.

    `received` maps a line number to the quantity actually received. Anything
    less than the quantity billed becomes a credit to claim from the supplier.
    """
    rows = []
    for _, line in lines.iterrows():
        billed = float(line["qty"] or 0)
        got = float(received.get(line["line"], billed))
        short = max(billed - got, 0.0)
        unit_price = float(line["unit_price_ex_gst"] or 0)
        rows.append({"line": line["line"], "code": line["code"],
                     "description": line["description"], "qty_billed": billed,
                     "qty_received": got, "short": short,
                     "unit_price_ex_gst": unit_price,
                     "claim_ex_gst": round(short * unit_price, 2)})
    out = pd.DataFrame(rows)
    out.attrs["claim_total"] = round(float(out["claim_ex_gst"].sum()), 2)
    return out
