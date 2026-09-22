"""
Question in, answer out - with the receipts.

Every answer has three parts:
  text       - one or two plain sentences
  table      - the numbers behind the sentence
  citations  - the invoice lines those numbers came from

The sentence is written from a template, so every number in it comes straight
from the table. If an AI model is used to make the wording friendlier, its
output goes through verify_numbers(): any figure in the model's text that is
not in the table gets the whole rewrite thrown away. The model can choose words.
It cannot choose numbers.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass, field

import pandas as pd
import yaml

from askinv.bridge import explain_change
from askinv.db import ROOT
from askinv.layer import Query, SemanticError, SemanticLayer, fmt
from askinv.menu import explain_drink_change, load_recipes
from askinv.planner import Vocabulary, plan

# Period labels are matched whole, so the 3 in "2025-Q3" can't vouch for "3%".
PERIOD_RE = re.compile(r"\b20\d\d-(?:Q[1-4]|\d{2})\b")
# Money and percentages keep their $ and %, so "3%" never matches "$3" or "3.1%".
NUMBER_RE = re.compile(r"[-+]?\$?\d[\d,]*(?:\.\d+)?%?")


@dataclass
class Answer:
    question: str
    text: str
    plan: dict | None = None
    table: pd.DataFrame | None = None
    citations: pd.DataFrame | None = None
    sql: str | None = None
    refused: bool = False
    phrasing: str = "template"
    facts: list = field(default_factory=list)


# --------------------------------------------------------------------------- numbers
def _tokens(text):
    text = text or ""
    periods = PERIOD_RE.findall(text)
    rest = PERIOD_RE.sub(" ", text)
    numbers = [t.lstrip("+-").replace(",", "") for t in NUMBER_RE.findall(rest)]
    return periods + numbers


def verify_numbers(text: str, allowed: str) -> list[str]:
    """Every number in `text` must also appear in `allowed`, with the same $ or %.
    Returns the ones that do not. An empty list means the text is safe to show."""
    ok = set(_tokens(allowed))
    return [t for t in _tokens(text) if t not in ok]


def rephrase_gemini(question, draft, model="gemini-2.0-flash"):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    prompt = ("Rewrite this answer to a cafe-bar owner's question so it reads naturally, "
              "in at most two sentences. Use ONLY numbers that appear in the draft, written "
              "exactly as they appear. Do not add, round, convert or calculate any number.\n\n"
              f"Question: {question}\nDraft: {draft}\n\nRewrite:")
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3}}
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:                                           # noqa: BLE001
        return None


# --------------------------------------------------------------------------- helpers
def _describe_filters(filters):
    bits = []
    for f in filters or []:
        v = f["value"]
        v = ", ".join(v) if isinstance(v, list) else v
        bits.append(f"{v}" if f["dimension"] in ("supplier", "drink", "product", "category",
                                                   "drink_type") else f"in {v}")
    return " ".join(bits)


def _fmt_metric(sl, metric, value, unit=None):
    return fmt(value, sl.metrics[metric].get("format", "number"), unit)


def _invoice_citations(sl, filters, limit=400):
    lines = sl.lineage({"metrics": ["spend"], "filters": filters}, limit=limit)
    return lines


# --------------------------------------------------------------------------- handlers
def _answer_query(sl, question, p):
    q = Query.from_dict(p["query"])
    df = sl.query(q)
    note = df.attrs.get("note")
    metric = q.metrics[0]
    label = sl.metrics[metric].get("label", metric)
    unit = df["unit"].iloc[0] if "unit" in df.columns and len(df) else None
    where = _describe_filters(q.filters)

    if df.empty:
        text = f"Nothing matched that {('for ' + where) if where else ''}."
    elif not q.dimensions:
        val = _fmt_metric(sl, metric, df[metric].iloc[0], unit)
        text = f"{label}{' for ' + where if where else ''}: {val}"
        if len(q.metrics) > 1:
            text += " (" + ", ".join(
                f"{sl.metrics[m].get('label', m).lower()} {_fmt_metric(sl, m, df[m].iloc[0], unit)}"
                for m in q.metrics[1:]) + ")"
        text += "."
    elif q.dimensions[0] in ("month", "quarter", "week") and len(q.dimensions) == 1 and len(df) > 1:
        d = q.dimensions[0]
        a, b = df.iloc[0], df.iloc[-1]
        u = b.get("unit") if "unit" in df.columns else unit
        change = (b[metric] / a[metric] - 1) if a[metric] else None
        text = (f"{label}{' for ' + where if where else ''} went from "
                f"{_fmt_metric(sl, metric, a[metric], u)} in {a[d]} to "
                f"{_fmt_metric(sl, metric, b[metric], u)} in {b[d]}")
        text += f" ({change:+.1%})." if change is not None else "."
    elif len(q.dimensions) == 2 and q.dimensions[1] in ("month", "quarter", "week"):
        # A trend per group: "Coffee went from 19.5% to 21.9%; Cocktail from ..."
        g, t = q.dimensions
        parts = []
        for name, grp in df.groupby(g, sort=False):
            grp = grp.sort_values(t)
            a, b = grp.iloc[0], grp.iloc[-1]
            u = b.get("unit", unit)
            parts.append(f"{name} from {_fmt_metric(sl, metric, a[metric], u)} ({a[t]}) to "
                         f"{_fmt_metric(sl, metric, b[metric], u)} ({b[t]})")
        text = f"{label}{' for ' + where if where else ''}: " + "; ".join(parts) + "."
    else:
        ranked = df.sort_values(metric, ascending=not q.descending) if not q.order_by else df
        top = ranked.head(3)
        parts = [f"{' / '.join(str(r[d]) for d in q.dimensions)} at "
                 f"{_fmt_metric(sl, metric, r[metric], r.get('unit', unit))}"
                 for _, r in top.iterrows()]
        lead = "Highest" if q.descending else "Lowest"
        text = (f"{label}{' for ' + where if where else ''} by {' and '.join(q.dimensions)}: "
                f"{lead} is " + parts[0]
                + ("; then " + "; ".join(parts[1:]) if len(parts) > 1 else "") + ".")
    if note:
        text += f" (As at the {note}.)"

    # citations
    dataset = df.attrs["dataset"]
    if dataset == "purchases":
        cites = sl.lineage(q)
    else:
        cites = sl.lineage(q)             # ingredient costs behind each drink
    return Answer(question, text, p, df, cites, df.attrs["sql"])


def _answer_spend_change(sl, question, p):
    b = explain_change(sl, p["period"], p["before"], p["after"], p.get("filters"))
    where = _describe_filters(p.get("filters"))
    pct = (b.change / b.spend_before) if b.spend_before else None
    drivers = b.top_drivers(3)
    top = drivers.iloc[0]
    kind = max(("price", "volume", "new", "stopped"), key=lambda k: abs(top[k]))
    kind_word = {"price": "price", "volume": "volume", "new": "a new product",
                 "stopped": "a product you stopped buying"}[kind]

    text = (f"Spend{' with ' + where if where else ''} went from {fmt(b.spend_before, 'currency')} "
            f"in {b.before} to {fmt(b.spend_after, 'currency')} in {b.after} "
            f"({'+' if b.change >= 0 else '-'}{fmt(abs(b.change), 'currency')}"
            + (f", {pct:+.1%}" if pct is not None else "") + "). "
            f"Price changes account for {'+' if b.price >= 0 else '-'}{fmt(abs(b.price), 'currency')}, "
            f"buying more or less for {'+' if b.volume >= 0 else '-'}{fmt(abs(b.volume), 'currency')}")
    if abs(b.new) > 0.5:
        text += f", new products {'+' if b.new >= 0 else '-'}{fmt(abs(b.new), 'currency')}"
    if abs(b.stopped) > 0.5:
        text += f", products you stopped buying -{fmt(abs(b.stopped), 'currency')}"
    text += (f". The biggest single driver is {top['product']} "
             f"({'+' if top['total'] >= 0 else '-'}{fmt(abs(top['total']), 'currency')}, mostly {kind_word}).")

    table = pd.DataFrame([
        {"step": f"Spend {b.before}", "amount": b.spend_before},
        {"step": "Price changes", "amount": b.price},
        {"step": "Buying more / less", "amount": b.volume},
        {"step": "New products", "amount": b.new},
        {"step": "Stopped buying", "amount": b.stopped},
        {"step": f"Spend {b.after}", "amount": b.spend_after},
    ])
    table.attrs["by_product"] = b.by_product
    table.attrs["reconciles"] = b.reconciles

    cite_filters = list(p.get("filters") or []) + [
        {"dimension": "product", "op": "=", "value": top["product"]},
        {"dimension": p["period"], "op": "in", "value": [b.before, b.after]}]
    cites = _invoice_citations(sl, cite_filters)
    return Answer(question, text, p, table, cites)


def _answer_drink_change(sl, question, p):
    ch = explain_drink_change(sl, p["drink"], p["before"], p["after"])
    if ch is None:
        raise SemanticError(f"No cost history for {p['drink']} in those periods.")
    t0, t1 = ch.attrs["total_before"], ch.attrs["total_after"]
    pct = t1 / t0 - 1 if t0 else 0
    top = ch.iloc[0]
    second = ch.iloc[1] if len(ch) > 1 else None
    article = "An" if p["drink"][0].lower() in "aeiou" else "A"
    text = (f"{article} {p['drink']} cost {fmt(t0, 'currency_cents')} to make in {p['before']} and "
            f"{fmt(t1, 'currency_cents')} in {p['after']} ({pct:+.1%}). "
            f"The spec hasn't changed, so it's all ingredient prices: "
            f"{top['ingredient'].replace('_', ' ')} "
            f"{'+' if top['change'] >= 0 else '-'}{fmt(abs(top['change']), 'currency_cents')} a serve")
    if second is not None and abs(second["change"]) >= 0.01:
        text += (f", {second['ingredient'].replace('_', ' ')} "
                 f"{'+' if second['change'] >= 0 else '-'}{fmt(abs(second['change']), 'currency_cents')}")
    text += "."

    # Trace the biggest mover back to the invoices that set its price
    recipes = load_recipes()
    codes = [str(c) for c in recipes["ingredients"][top["ingredient"]]["products"]]
    grain = "quarter" if "-Q" in p["before"] else "month"
    products = sl.query({"metrics": ["spend"], "dimensions": ["product", "product_code"]})
    names = products[products["product_code"].isin(codes)]["product"].unique().tolist()
    cites = _invoice_citations(sl, [
        {"dimension": "product", "op": "in", "value": names},
        {"dimension": grain, "op": "in", "value": [p["before"], p["after"]]}])
    return Answer(question, text, p, ch, cites)


# --------------------------------------------------------------------------- entry
def ask(question: str, sl: SemanticLayer | None = None, planner: str = "auto",
        vocab: Vocabulary | None = None, rephrase: bool = False) -> Answer:
    sl = sl or SemanticLayer()
    try:
        p = plan(question, sl, planner, vocab)
        if p["intent"] == "query":
            ans = _answer_query(sl, question, p)
        elif p["intent"] == "explain_spend":
            ans = _answer_spend_change(sl, question, p)
        else:
            ans = _answer_drink_change(sl, question, p)
    except SemanticError as exc:
        return Answer(question, f"I can't answer that honestly: {exc}", refused=True)

    if rephrase:
        better = rephrase_gemini(question, ans.text)
        if better:
            bad = verify_numbers(better, ans.text)
            if bad:
                ans.phrasing = f"template (model rewrite rejected: unverified {', '.join(bad)})"
            else:
                ans.text, ans.phrasing = better, "model, numbers verified"
    return ans


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--planner", default="auto", choices=["auto", "rules", "gemini"])
    a = ap.parse_args()
    res = ask(a.question, planner=a.planner)
    print(res.text)
    if res.table is not None:
        print()
        print(res.table.to_string(index=False))
    if res.citations is not None and len(res.citations):
        inv = res.citations["invoice_no"].unique() if "invoice_no" in res.citations else []
        print(f"\nSources: {len(res.citations)} records" +
              (f" from {len(inv)} invoices: {', '.join(inv[:8])}{' ...' if len(inv) > 8 else ''}"
               if len(inv) else ""))
