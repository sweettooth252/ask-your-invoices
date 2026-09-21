"""
Read a folder of invoice PDFs into a small star schema.

Every row in the fact table keeps the file, invoice number and line number it
came from. That is what makes drill-through possible later: any figure the
assistant quotes can be traced back to the exact printed line behind it.

    python -m askinv.ingest data/invoices
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd
import yaml

from askinv.db import DB_PATH, ENGINE, ROOT, connect
from creep.extract import read_folder

CATEGORY_FILE = ROOT / "semantic" / "categories.yml"


def load_category_rules(path=CATEGORY_FILE):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)["categories"]


def categorise(description: str, rules) -> str:
    """First rule whose keyword appears as a whole word wins. Whole words
    matter: a plain substring check filed "Single Origin Filter Beans" under
    Spirits, because "Origin" contains "gin"."""
    text = (description or "").lower()
    for rule in rules:
        if any(re.search(rf"\b{re.escape(k)}s?\b", text) for k in rule["keywords"]):
            return rule["name"]
    return "Uncategorised"


def build(folder, db_path=DB_PATH, verbose=True) -> dict:
    invoices = read_folder(folder)
    rules = load_category_rules()

    suppliers, products, lines, headers = {}, {}, [], []
    for inv in invoices:
        sname = (inv.supplier_name or "Unknown supplier").strip()
        sid = suppliers.setdefault(sname, {"supplier_id": len(suppliers) + 1,
                                           "supplier_name": sname,
                                           "abn": inv.supplier_abn})["supplier_id"]
        line_total = 0.0
        for l in inv.lines:
            code = (l.code or "").strip().upper()
            key = (sid, code or (l.description or "").strip().lower())
            if key not in products:
                products[key] = {
                    "product_id": len(products) + 1,
                    "supplier_id": sid,
                    "code": code or None,
                    "product_name": (l.description or "").strip(),
                    "category": categorise(l.description, rules),
                    "base_unit": l.base_unit,
                }
            pid = products[key]["product_id"]
            amount = l.amount_ex_gst or 0.0
            line_total += amount
            lines.append({
                "line_id": len(lines) + 1,
                "invoice_no": inv.invoice_no,
                "source_file": inv.source_file,
                "line_no": l.line_no,
                "date_key": int(inv.invoice_date.replace("-", "")) if inv.invoice_date else None,
                "supplier_id": sid,
                "product_id": pid,
                "pack_raw": l.pack_raw,
                "qty": l.qty,
                "unit_price_ex_gst": l.unit_price_ex_gst,
                "amount_ex_gst": amount,
                "base_unit": l.base_unit,
                "qty_base": (l.qty * l.base_qty) if (l.qty is not None and l.base_qty) else None,
                "price_per_base_unit": l.price_per_base_unit,
                "confidence": l.confidence,
            })
        headers.append({
            "invoice_no": inv.invoice_no,
            "source_file": inv.source_file,
            "supplier_id": sid,
            "date_key": int(inv.invoice_date.replace("-", "")) if inv.invoice_date else None,
            "subtotal_ex_gst": inv.subtotal_ex_gst,
            "total_inc_gst": inv.total_inc_gst,
            "lines_sum_ex_gst": round(line_total, 2),
            # The invoice carries its own checksum. If the lines we read do not
            # add up to the subtotal printed on the page, something was misread.
            "reconciles": int(inv.subtotal_ex_gst is not None
                              and abs(line_total - inv.subtotal_ex_gst) < 0.02),
        })

    fact = pd.DataFrame(lines)
    days = pd.to_datetime(fact["date_key"].dropna().astype(int).astype(str), format="%Y%m%d")
    all_days = pd.date_range(days.min(), days.max(), freq="D")
    dim_date = pd.DataFrame({
        "date_key": all_days.strftime("%Y%m%d").astype(int),
        "business_date": all_days.strftime("%Y-%m-%d"),
        "month": all_days.strftime("%Y-%m"),
        "quarter": all_days.year.astype(str) + "-Q" + all_days.quarter.astype(str),
        "week_start": (all_days - pd.to_timedelta(all_days.dayofweek, unit="D")).strftime("%Y-%m-%d"),
    })

    con = connect(db_path, read_only=False)
    con.write("dim_supplier", pd.DataFrame(suppliers.values()))
    con.write("dim_product", pd.DataFrame(products.values()))
    con.write("dim_date", dim_date)
    con.write("fct_invoice_line", fact)
    con.write("fct_invoice", pd.DataFrame(headers))
    con.close()

    # Invoices -> ingredient prices -> cost of every drink on the menu.
    # Read back through the semantic layer so the menu uses the same
    # definition of "price paid" as every other answer. Skipped until the
    # semantic layer and the recipes exist, so this file works from day one.
    drinks = None
    if (ROOT / "semantic" / "model.yml").exists() and (ROOT / "semantic" / "recipes.yml").exists():
        from askinv.layer import SemanticLayer
        from askinv.menu import build_costs
        drinks, ingredients = build_costs(SemanticLayer(db_path=db_path))
        con = connect(db_path, read_only=False)
        con.write("fct_drink_cost", drinks)
        con.write("fct_drink_ingredient_cost", ingredients)
        con.close()

    summary = {
        "engine": ENGINE,
        "invoices": len(headers),
        "lines": len(fact),
        "suppliers": len(suppliers),
        "products": len(products),
        "reconciled": sum(h["reconciles"] for h in headers),
        "drinks": drinks["drink"].nunique() if drinks is not None else 0,
        "uncategorised": sorted(p["product_name"] for p in products.values()
                                if p["category"] == "Uncategorised"),
    }
    if verbose:
        print(f"engine        {summary['engine']}")
        print(f"invoices      {summary['invoices']}  ({summary['reconciled']} reconcile to their printed subtotal)")
        print(f"lines         {summary['lines']}")
        print(f"suppliers     {summary['suppliers']}")
        print(f"products      {summary['products']}")
        if drinks is not None:
            print(f"drinks costed {summary['drinks']}  (every month and quarter)")
        else:
            print("drinks        not yet - add semantic/model.yml and recipes.yml")
        if summary["uncategorised"]:
            print("uncategorised - add a keyword to semantic/categories.yml:")
            for name in summary["uncategorised"]:
                print(f"  {name}")
        print(f"written to    {db_path}")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", nargs="?", default=str(ROOT / "data" / "invoices"))
    build(ap.parse_args().folder)
