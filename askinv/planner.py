"""
English question -> a plan the semantic layer can run.

The planner never writes SQL and never sees the data. It picks one of four
intents and fills in names from the catalogue:

    {"intent": "query", "query": {"metrics": [...], "dimensions": [...], "filters": [...]}}
    {"intent": "explain_spend", "period": "quarter", "before": "2026-Q1", "after": "2026-Q2", "filters": [...]}
    {"intent": "explain_drink", "drink": "Margarita", "before": "2025-Q4", "after": "2026-Q3"}
    {"intent": "refuse", "reason": "..."}

Every plan then goes through validate(), which checks every name AND every
filter value against the real data. A model can invent a supplier; it cannot
get an invented supplier past validate().

Two planners with the same contract:
  rules  - keywords and the actual values in the data. Free, offline, the default.
  gemini - Google's free tier, used only if GEMINI_API_KEY is set.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

from askinv.layer import SemanticError, SemanticLayer

# --------------------------------------------------------------------------- vocab
METRIC_WORDS = [   # longest phrases first matter; order resolved by span
    ("pour_cost_pct", ["pour cost", "cost percentage", "cost %", "food cost %"]),
    ("margin_per_serve", ["margin", "gross profit per", "profit per drink", "make on"]),
    ("cost_per_serve", ["cost to make", "cost per serve", "cost per drink", "cost us to make",
                        "costs to make", "cost of making", "how much does", "cost"]),
    ("menu_price", ["menu price", "sell for", "selling price"]),
    ("avg_price_per_unit", ["price per", "per litre", "per liter", "per kg", "per kilo",
                            "unit price", "price of", "paying for", "paying per",
                            "per unit", "price"]),
    ("quantity", ["how much did we buy", "how many litres", "how many kilos", "quantity",
                  "volume", "litres", "kilos"]),
    ("invoices", ["how many invoices", "invoices", "deliveries"]),
    ("products_bought", ["how many products", "number of products"]),
    ("spend", ["spend", "spent", "spending", "outlay", "purchases", "bought", "paid"]),
]

DIMENSION_WORDS = [
    ("supplier", ["supplier", "suppliers", "vendor", "who we buy from"]),
    ("category", ["category", "categories", "type of product"]),
    ("product", ["product", "products", "item", "items", "ingredient", "ingredients"]),
    ("drink_type", ["cocktails vs coffee", "coffee vs cocktails", "drink type",
                    "cocktails and coffee", "coffee and cocktails"]),
    ("drink", ["each drink", "per drink", "by drink", "every drink", "which drink",
               "drinks", "menu", "each cocktail", "every cocktail", "which cocktail",
               "each coffee", "which coffee", "our cocktails", "cocktails"]),
    ("month", ["month", "monthly", "each month", "by month"]),
    ("quarter", ["quarter", "quarterly", "each quarter", "by quarter"]),
    ("week", ["week", "weekly"]),
    ("unit", ["by unit", "per unit type"]),
]

DRINK_ALIASES = {"margs": "Margarita", "g&t": "Gin & Tonic", "gin and tonic": "Gin & Tonic",
                 "gnt": "Gin & Tonic", "espresso martini": "Espresso Martini",
                 "oat latte": "Oat Latte (takeaway)", "filter": "Batch Filter",
                 "batch brew": "Batch Filter", "cold brew": "Cold Brew on Ice",
                 "aperol": "Spritz", "spritz": "Spritz"}

# Strong words ask "why" on their own. Weak ones ("vs", "compare") only mean a
# bridge when two concrete periods are named - "cocktails vs coffee by
# quarter" is a breakdown, not an explanation.
WHY_STRONG = ["why", "what changed", "what's changed", "explain", "what drove", "driver",
              "go up", "went up", "gone up", "increase", "more expensive", "dearer"]
WHY_WEAK = ["difference between", "compared to", "compare", " vs ", " versus ", " than "]

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
# Whole month words only. An earlier version read "martini" as March.
MONTH_RE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|november|"
    r"december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b(?:\s+(20\d\d))?")

OUT_OF_SCOPE = [
    # staff questions. "barista" is also in "Bar & Barista Supplies" and "Oat Milk
    # Barista", so skip it straight after "& " or "milk ".
    (r"(?<!& )(?<!milk )\b(baristas?|bartenders?|roster)\b",
     "There is no staff data here - only supplier invoices and menu specs."),
    (r"\b(sales|revenue|sold|turnover|takings|covers)\b",
     "There is no sales data here - only supplier invoices and menu specs. I can tell "
     "you what a drink costs to make, not how many were sold."),
    (r"\b(profit|net margin|ebitda|p&l|wage|staff|labour|labor|rent)\b(?!.*per)",
     "Only purchases and drink costs are in this data - nothing on wages, rent or overall profit."),
    (r"\b(reliab|late deliver|on time|quality|rating|review)",
     "Invoices don't record delivery times or quality, so I can't judge suppliers on that."),
    (r"\b(forecast|predict|next month|next quarter|will .* cost)\b",
     "I only report what was actually paid. I won't forecast prices."),
]


def _span_hits(text, table):
    """Longest phrase wins; a word already claimed can't be reused."""
    found = []
    for name, words in table:
        for w in words:
            for m in re.finditer(rf"(?<![a-z]){re.escape(w)}(?![a-z])", text):
                found.append((m.start(), m.end(), name))
    found.sort(key=lambda f: -(f[1] - f[0]))
    kept, spans = [], []
    for s, e, name in found:
        if any(s < b and a < e for a, b in spans):
            continue
        spans.append((s, e))
        if name not in kept:
            kept.append(name)
    return kept


class Vocabulary:
    """The real values in the data - suppliers, drinks, products, periods."""

    def __init__(self, sl: SemanticLayer):
        self.suppliers = sl.values("supplier")
        self.categories = sl.values("category")
        self.products = sl.values("product")
        self.drinks = sl.values("drink", "drinks")
        self.quarters = sl.values("quarter", "purchases")
        self.months = sl.values("month", "purchases")

    def find_supplier(self, q):
        for s in self.suppliers:
            first = re.split(r"[\s&]+", s.lower())[0]
            if first in q or s.lower() in q:
                return s
        return None

    def find_drink(self, q):
        for alias, name in sorted(DRINK_ALIASES.items(), key=lambda x: -len(x[0])):
            if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", q) and name in self.drinks:
                return name
        for d in sorted(self.drinks, key=len, reverse=True):
            if d.lower().split(" (")[0] in q:
                return d
        return None

    def find_category(self, q):
        for c in self.categories:
            words = [w for w in re.split(r"[\s&,]+", c.lower()) if len(w) > 3]
            if c.lower() in q or any(re.search(rf"\b{re.escape(w)}", q) for w in words):
                return c
        return None

    def find_products(self, q):
        hits = []
        for p in self.products:
            head = p.lower().split()[0]
            if len(head) >= 3 and re.search(rf"\b{re.escape(head)}s?\b", q):
                hits.append(p)
        return hits

    def find_periods(self, q):
        """Quarters and months, in the order they appear in the question."""
        out = []
        for m in re.finditer(r"(20\d\d)\s*-?\s*q([1-4])|q([1-4])\s*(20\d\d)", q):
            y = m.group(1) or m.group(4)
            n = m.group(2) or m.group(3)
            out.append((m.start(), "quarter", f"{y}-Q{n}"))
        for m in re.finditer(r"\b(20\d\d)-(0[1-9]|1[0-2])\b", q):
            out.append((m.start(), "month", f"{m.group(1)}-{m.group(2)}"))
        for m in MONTH_RE.finditer(q):
            mm = MONTHS[m.group(1)[:3]]
            if m.group(2):
                label = f"{m.group(2)}-{mm:02d}"
            else:
                label = next((x for x in reversed(self.months) if x.endswith(f"-{mm:02d}")), None)
            if label:
                out.append((m.start(), "month", label))
        if "this quarter" in q or "latest quarter" in q:
            out.append((q.find("this quarter") if "this quarter" in q else q.find("latest quarter"),
                        "quarter", self.quarters[-1]))
        if "last quarter" in q or "previous quarter" in q:
            pos = q.find("last quarter") if "last quarter" in q else q.find("previous quarter")
            out.append((pos, "quarter", self.quarters[-2]))
        if "this month" in q:
            out.append((q.find("this month"), "month", self.months[-1]))
        if "last month" in q:
            out.append((q.find("last month"), "month", self.months[-2]))
        out.sort()
        seen, res = set(), []
        for _pos, kind, label in out:
            if (kind, label) not in seen:
                seen.add((kind, label))
                res.append((kind, label))
        return res


# --------------------------------------------------------------------------- rules
def plan_rules(question: str, sl: SemanticLayer, vocab: Vocabulary | None = None) -> dict:
    vocab = vocab or Vocabulary(sl)
    q = " " + question.lower().replace("’", "'") + " "

    for pattern, reason in OUT_OF_SCOPE:
        if re.search(pattern, q):
            return {"intent": "refuse", "reason": reason}

    drink = vocab.find_drink(q)
    supplier = vocab.find_supplier(q)
    periods = vocab.find_periods(q)
    is_why = any(w in q for w in WHY_STRONG) or (
        any(w in q for w in WHY_WEAK) and len(periods) >= 2)

    # ---- "why" questions -> a bridge (defaults to the last two quarters)
    if is_why:
        kind = periods[0][0] if periods else ("month" if "month" in q else "quarter")
        labels = [p for k, p in periods if k == kind]
        series = vocab.quarters if kind == "quarter" else vocab.months
        if len(labels) >= 2:
            before, after = labels[0], labels[1]
        elif len(labels) == 1:
            i = series.index(labels[0]) if labels[0] in series else len(series) - 1
            before, after = (series[i - 1], labels[0]) if i > 0 else (labels[0], series[-1])
        else:
            before, after = series[-2], series[-1]
        if before > after:
            before, after = after, before
        if drink:
            return {"intent": "explain_drink", "drink": drink, "before": before, "after": after}
        filters = []
        if supplier:
            filters.append({"dimension": "supplier", "op": "=", "value": supplier})
        cat = vocab.find_category(q)
        if cat and not supplier:
            filters.append({"dimension": "category", "op": "=", "value": cat})
        return {"intent": "explain_spend", "period": kind, "before": before,
                "after": after, "filters": filters}

    # ---- ordinary questions
    metrics = _span_hits(q, METRIC_WORDS)
    dims = _span_hits(q, DIMENSION_WORDS)
    drinkish = bool(drink) or "drink" in dims or "drink_type" in dims or \
        any(w in q for w in ["cocktail", "coffee menu", "to make", "pour cost", "margin", "serve"])

    if drinkish:
        metrics = [m for m in metrics if m in ("cost_per_serve", "pour_cost_pct",
                                               "margin_per_serve", "menu_price")] or ["cost_per_serve"]
        dims = [d for d in dims if d in ("drink", "drink_type", "month", "quarter")]
    else:
        metrics = [m for m in metrics if m in ("spend", "invoices", "products_bought",
                                               "quantity", "avg_price_per_unit")] or ["spend"]
        dims = [d for d in dims if d not in ("drink", "drink_type")]

    filters = []
    if drinkish:
        if drink:
            filters.append({"dimension": "drink", "op": "=", "value": drink})
        elif "cocktail" in q and "coffee" not in q:
            filters.append({"dimension": "drink_type", "op": "=", "value": "Cocktail"})
        elif "coffee" in q and "cocktail" not in q:
            filters.append({"dimension": "drink_type", "op": "=", "value": "Coffee"})
        if not drink and not dims and any(m in metrics for m in
                                          ("cost_per_serve", "margin_per_serve", "menu_price")):
            dims.insert(0, "drink")     # only when no grouping was asked for
    else:
        if supplier:
            filters.append({"dimension": "supplier", "op": "=", "value": supplier})
            dims = [d for d in dims if d != "supplier"]
        prods = vocab.find_products(q)
        cat = vocab.find_category(q)
        if prods and len(prods) <= 3:
            filters.append({"dimension": "product", "op": "in", "value": prods})
            if len(prods) > 1 and "product" not in dims:
                dims.append("product")
        elif cat and "category" not in dims:
            filters.append({"dimension": "category", "op": "=", "value": cat})
        asked_for_total = re.search(r"\b(total|altogether|overall|in all|combined)\b", q)
        if any(m in metrics for m in ("quantity", "avg_price_per_unit")) and \
                not prods and not dims and not asked_for_total:
            # "What are we paying per unit?" - per product is the only honest
            # reading. But if a grouping WAS asked for (by category, by
            # supplier) it is left alone, so the layer can refuse and explain.
            dims.append("product")

    for kind, label in periods[:2]:
        if kind not in dims:
            op_vals = [p for k, p in periods if k == kind]
            filters.append({"dimension": kind, "op": "in" if len(op_vals) > 1 else "=",
                            "value": op_vals if len(op_vals) > 1 else op_vals[0]})
            break

    query = {"metrics": metrics[:3], "dimensions": dims[:2], "filters": filters}
    if re.search(r"\b(top|most|biggest|highest|largest|which)\b", q) and dims:
        query["order_by"] = metrics[0]
        query["limit"] = 5
    elif re.search(r"\b(lowest|least|cheapest|smallest)\b", q) and dims:
        query["order_by"] = metrics[0]
        query["descending"] = False
        query["limit"] = 5
    return {"intent": "query", "query": query}


# --------------------------------------------------------------------------- gemini
PROMPT = """You turn questions about a cafe-bar's supplier invoices and drink costs
into a JSON plan. You never write SQL and you never state numbers.

Allowed measures and groupings (use these exact names, nothing else):
{catalogue}

Real values in the data (filters must use one of these exactly):
suppliers: {suppliers}
drinks: {drinks}
categories: {categories}
quarters: {quarters}
months: {months}

Return ONE JSON object, one of:
{{"intent":"query","query":{{"metrics":[...],"dimensions":[...],"filters":[{{"dimension":"...","op":"=","value":"..."}}],"order_by":null,"limit":null}}}}
{{"intent":"explain_spend","period":"quarter","before":"...","after":"...","filters":[]}}
{{"intent":"explain_drink","drink":"...","before":"...","after":"..."}}
{{"intent":"refuse","reason":"..."}}

Rules:
- Measures from "purchases" and "drinks" cannot be combined in one query.
- quantity and avg_price_per_unit only make sense per product or per unit.
- cost_per_serve, menu_price, margin_per_serve only make sense per drink.
- "This quarter" means {latest_q}. "Last quarter" means {prev_q}.
- If the question needs sales, wages, forecasts, quality or anything not listed, refuse.
Output JSON only."""


def plan_gemini(question: str, sl: SemanticLayer, vocab: Vocabulary | None = None,
                model: str = "gemini-2.0-flash") -> dict:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise SemanticError("GEMINI_API_KEY is not set.")
    vocab = vocab or Vocabulary(sl)
    prompt = PROMPT.format(
        catalogue=json.dumps(sl.catalogue(), indent=1),
        suppliers=vocab.suppliers, drinks=vocab.drinks, categories=vocab.categories,
        quarters=vocab.quarters, months=vocab.months,
        latest_q=vocab.quarters[-1], prev_q=vocab.quarters[-2])
    body = {"contents": [{"parts": [{"text": prompt + "\n\nQuestion: " + question}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    return json.loads(text)


# --------------------------------------------------------------------------- guardrail
def validate(plan: dict, sl: SemanticLayer, vocab: Vocabulary | None = None) -> dict:
    """Nothing reaches the database without passing this. Names must exist in
    the model; filter values must exist in the data; periods must be real."""
    vocab = vocab or Vocabulary(sl)
    intent = plan.get("intent")
    if intent == "refuse":
        raise SemanticError(plan.get("reason") or "That isn't something this data can answer.")

    periods = {"quarter": vocab.quarters, "month": vocab.months}
    known = {"supplier": vocab.suppliers, "drink": vocab.drinks,
             "category": vocab.categories, "product": vocab.products,
             "quarter": vocab.quarters, "month": vocab.months,
             "drink_type": ["Cocktail", "Coffee"]}

    def check_filters(filters):
        for f in filters or []:
            d = f.get("dimension")
            if d not in sl.dimensions:
                raise SemanticError(f"'{d}' isn't something I can filter on.")
            vals = f.get("value")
            vals = vals if isinstance(vals, list) else [vals]
            if d in known:
                for v in vals:
                    if v not in known[d]:
                        raise SemanticError(f"There is no {d} called '{v}' in the invoices.")

    if intent == "query":
        q = plan.get("query") or {}
        for m in q.get("metrics") or []:
            if m not in sl.metrics:
                raise SemanticError(f"'{m}' isn't a defined measure, so I won't guess at it.")
        for d in q.get("dimensions") or []:
            if d not in sl.dimensions:
                raise SemanticError(f"'{d}' isn't something I can group by.")
        check_filters(q.get("filters"))
        sl.compile(q)                          # dataset and grain rules
        return plan

    if intent in ("explain_spend", "explain_drink"):
        kind = plan.get("period") or ("quarter" if "-Q" in str(plan.get("before")) else "month")
        for p in (plan.get("before"), plan.get("after")):
            if p not in periods.get(kind, []):
                raise SemanticError(f"There is no {kind} '{p}' in the data.")
        if intent == "explain_drink" and plan.get("drink") not in vocab.drinks:
            raise SemanticError(f"'{plan.get('drink')}' isn't on the menu.")
        check_filters(plan.get("filters"))
        return plan

    raise SemanticError("I couldn't work out what was being asked.")


def plan(question: str, sl: SemanticLayer, planner: str = "auto",
         vocab: Vocabulary | None = None) -> dict:
    vocab = vocab or Vocabulary(sl)
    if planner == "auto":
        planner = "gemini" if os.environ.get("GEMINI_API_KEY") else "rules"
    raw = plan_gemini(question, sl, vocab) if planner == "gemini" else plan_rules(question, sl, vocab)
    return validate(raw, sl, vocab)
