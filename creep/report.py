"""
Turn findings into something a venue owner will actually read.

The report is written for someone standing in a kitchen at 3pm with ten
minutes. Dollars first, the action second, the evidence last and only if they
want it.

    python -m creep.report data/invoices --out out/report.md
"""

from __future__ import annotations

import argparse
from pathlib import Path

from creep.detect import analyse, total_impact, _money
from creep.extract import read_folder

SEVERITY_LABEL = {"high": "Worth a call", "medium": "Worth a look",
                  "info": "For information"}


def lines_from_invoices(invoices):
    """Flatten read invoices into the rows the detector expects."""
    rows = []
    for inv in invoices:
        for l in inv.lines:
            row = dict(l.__dict__)
            row.update(supplier=inv.supplier_name,
                       invoice_date=inv.invoice_date,
                       invoice_no=inv.invoice_no)
            rows.append(row)
    return rows


def build_report(invoices) -> str:
    rows = lines_from_invoices(invoices)
    findings, products = analyse(rows)

    dated = [r["invoice_date"] for r in rows if r.get("invoice_date")]
    period = f"{min(dated)} to {max(dated)}" if dated else "unknown period"
    spend = sum(r.get("amount_ex_gst") or 0 for r in rows)
    suppliers = sorted({r["supplier"] for r in rows if r.get("supplier")})
    actionable = [f for f in findings if f.annual_impact > 0]
    low_confidence = [r for r in rows if (r.get("confidence") or 1) < 0.8]

    out = [
        "# Supplier price check",
        "",
        f"{len(invoices)} invoices from {len(suppliers)} suppliers, {period}. "
        f"{_money(spend)} of purchases excluding GST.",
        "",
        f"**Increases worth about {_money(total_impact(findings))} a year.** "
        f"{len(actionable)} things to act on, {len(findings) - len(actionable)} "
        f"for information.",
        "",
    ]

    if actionable:
        out += ["## What it is costing you", "",
                "| | Finding | A year |", "| --- | --- | ---: |"]
        for f in actionable:
            out.append(f"| {SEVERITY_LABEL[f.severity]} | {f.title} | "
                       f"{_money(f.annual_impact)} |")
        out.append("")

    out += ["## The detail", ""]
    for f in findings:
        out += [f"### {f.title}", ""]
        if f.annual_impact:
            out.append(f"**About {_money(f.annual_impact)} a year.**")
            out.append("")
        out += [f.detail, ""]
        if f.action:
            out += [f"*What to do:* {f.action}", ""]

    out += ["## Suppliers in this check", ""]
    for s in suppliers:
        n = sum(1 for i in invoices if i.supplier_name == s)
        amt = sum(r.get("amount_ex_gst") or 0 for r in rows if r.get("supplier") == s)
        out.append(f"- {s} — {n} invoices, {_money(amt)}")
    out.append("")

    if low_confidence:
        out += [
            "## Lines to check", "",
            f"{len(low_confidence)} lines could not be read with confidence and were "
            "left out of the figures above. They are usually an unusual pack size or "
            "a handwritten amendment.", "",
        ]
        for r in low_confidence[:10]:
            out.append(f"- {r.get('invoice_no')}: {r.get('description')} "
                       f"({'; '.join(r.get('issues') or [])})")
        out.append("")

    out += [
        "---", "",
        "Prices are compared per litre, per kilo or per unit, so a change in pack "
        "size counts as a price change. Figures exclude GST. Annual estimates use "
        "your buying pattern over the period above.",
        "",
    ]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", nargs="?", default="data/invoices")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    invoices = read_folder(args.folder)
    if not invoices:
        raise SystemExit(f"No PDFs found in {args.folder}")
    md = build_report(invoices)

    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(md, encoding="utf-8")
        print(f"written to {path}")
    else:
        print(md)


if __name__ == "__main__":
    main()
