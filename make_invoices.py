"""
Generate a financial year (FY26) of supplier invoices for a Melbourne cafe-bar, as PDFs,
plus the right answer for every line.

The venue is "Night & Day": specialty coffee from 7am, cocktails from 5pm.
Six fictional suppliers, five different invoice layouts.

Seven things are planted in the data. Nothing downstream is told about them:

  1. Coffee liqueur creeps up a little every month
  2. The coffee roaster lifts its whole range on one day (green bean prices)
  3. Tequila goes from 750ml to 700ml bottles at the same case price
  4. Oat milk is bought from two suppliers at two prices
  5. An opening price on single-origin beans ends
  6. Limes spike in winter - seasonal, NOT a supplier price rise
  7. A one-off fuel surcharge that is not a price at all

Usage:  python make_invoices.py
Output: data/invoices/*.pdf  and  data/ground_truth.json
"""

import json
import os
import random
from datetime import date, timedelta

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

SEED = 2718
random.seed(SEED)

ROOT = os.path.dirname(os.path.abspath(__file__))
PDF_DIR = os.path.join(ROOT, "data", "invoices")
os.makedirs(PDF_DIR, exist_ok=True)

# One Australian financial year: four complete quarters, so no quarter is
# silently compared against a partial one.
START = date(2025, 7, 1)
END = date(2026, 6, 30)

VENUE = {
    "name": "Night & Day",
    "address": "88 Smith Street",
    "suburb": "Collingwood VIC 3066",
    "account": "ND-2231",
}

SUPPLIERS = {
    "harbourline": {"name": "Harbourline Spirits Pty Ltd", "abn": "41 207 553 190",
                    "address": "5 Dock Lane, Port Melbourne VIC 3207",
                    "layout": "columns_with_pack", "cadence": 7, "prefix": "HS"},
    "ember": {"name": "Ember & Co Coffee Roasters", "abn": "72 614 008 325",
              "address": "31 Kiln Street, Brunswick VIC 3056",
              "layout": "gst_column", "cadence": 7, "prefix": "EC"},
    "northside": {"name": "Northside Dairy Direct", "abn": "19 447 902 336",
                  "address": "62 Sydney Road, Coburg VIC 3058",
                  "layout": "minimal", "cadence": 3, "prefix": "ND"},
    "yarrafresh": {"name": "Yarra Fresh Produce", "abn": "88 621 330 447",
                   "address": "Shed 4, Melbourne Markets, Epping VIC 3076",
                   "layout": "pack_in_description", "cadence": 7, "prefix": "YF"},
    "barsupply": {"name": "Bar & Barista Supplies", "abn": "30 559 148 002",
                  "address": "14 Trade Place, Coburg North VIC 3058",
                  "layout": "columns_with_pack", "cadence": 14, "prefix": "BB"},
    # A broad-line providore: mixers, syrups, teas, and a second spirits range.
    # Its invoice puts the description first and the code second, and calls the
    # columns PARTICULARS / REF / UOM / DELIVERED / U-PRICE / EXT - a layout the
    # reader has never seen, which is the point of adding it.
    "merri": {"name": "Merri Creek Providore", "abn": "63 118 740 526",
              "address": "9 Copeland Street, Preston VIC 3072",
              "layout": "description_first", "cadence": 10, "prefix": "MC"},
}

# supplier, code, description, pack, base_qty, base_unit, price per pack ex GST
# (Dec 2025), GST applies, order quantity range, chance it is on an order
CATALOGUE = [
    ("harbourline", "HS-VOD-07", "Harbour Vodka", "6 x 700ML", 4.2, "L", 168.00, True, (1, 2), 0.85),
    ("harbourline", "HS-GIN-07", "Coastline Dry Gin", "6 x 700ML", 4.2, "L", 222.00, True, (1, 1), 0.70),
    ("harbourline", "HS-TEQ-75", "Agave Azul Blanco Tequila", "6 x 750ML", 4.5, "L", 246.00, True, (1, 1), 0.60),
    ("harbourline", "HS-CFL-07", "Cafe Noir Coffee Liqueur", "6 x 700ML", 4.2, "L", 150.00, True, (1, 2), 0.85),
    ("harbourline", "HS-BIT-07", "Rosso Bitter Aperitivo", "6 x 700ML", 4.2, "L", 174.00, True, (1, 1), 0.55),
    ("harbourline", "HS-VER-10", "Sweet Vermouth Rosso", "6 x 1L", 6, "L", 96.00, True, (1, 1), 0.45),
    ("harbourline", "HS-SPZ-07", "Arancia Spritz Aperitivo", "6 x 700ML", 4.2, "L", 132.00, True, (1, 2), 0.75),
    ("harbourline", "HS-PRO-75", "Prosecco DOC", "12 x 750ML", 9, "L", 168.00, True, (1, 2), 0.75),
    ("harbourline", "HS-TSC-07", "Triple Sec", "6 x 700ML", 4.2, "L", 120.00, True, (1, 1), 0.40),

    ("ember", "EC-HB-1K", "House Blend Espresso Beans", "6 x 1KG", 6, "kg", 198.00, True, (2, 3), 1.00),
    ("ember", "EC-SO-1K", "Single Origin Filter Beans", "1KG", 1, "kg", 48.00, True, (2, 4), 0.80),
    ("ember", "EC-DC-1K", "Swiss Water Decaf Beans", "1KG", 1, "kg", 44.00, True, (1, 2), 0.70),
    ("ember", "EC-CB-5L", "Cold Brew Concentrate", "5L", 5, "L", 62.00, True, (1, 2), 0.70),

    ("northside", "D-101", "Full Cream Milk", "9 x 2L", 18, "L", 26.10, True, (3, 6), 1.00),
    ("northside", "D-140", "Oat Milk Barista", "12 x 1L", 12, "L", 39.60, True, (1, 3), 0.90),
    ("northside", "D-118", "Thickened Cream", "6 x 1L", 6, "L", 28.20, True, (1, 1), 0.35),

    ("yarrafresh", "1455", "Lemon Class 1 18kg", "18KG", 18, "kg", 54.00, False, (1, 1), 0.60),
    ("yarrafresh", "1460", "Lime Tahitian 10kg", "10KG", 10, "kg", 48.00, False, (1, 2), 0.95),
    ("yarrafresh", "1470", "Orange Navel 15kg", "15KG", 15, "kg", 36.00, False, (1, 1), 0.80),
    ("yarrafresh", "1510", "Mint Bunches 1 doz", "1DOZ", 12, "ea", 18.00, False, (1, 2), 0.70),

    ("barsupply", "BS-SYR-1L", "Cane Sugar Syrup", "6 x 1L", 6, "L", 42.00, True, (1, 2), 0.90),
    ("barsupply", "BS-TON-20", "Premium Tonic Water", "24 x 200ML", 4.8, "L", 38.40, True, (1, 2), 0.90),
    ("barsupply", "BS-SOD-20", "Soda Water", "24 x 200ML", 4.8, "L", 26.40, True, (1, 1), 0.85),
    ("barsupply", "BS-OAT-1L", "Oat Milk Barista", "12 x 1L", 12, "L", 34.80, True, (1, 2), 0.90),
    ("barsupply", "BS-CUP-08", "Takeaway Cup 8oz Double Wall", "CTN 500", 500, "ea", 82.00, True, (1, 1), 0.90),
    ("barsupply", "BS-LID-08", "Sip Lid 8oz", "CTN 1000", 1000, "ea", 46.00, True, (1, 1), 0.60),

    # Merri Creek Providore - mixers and garnish
    ("merri", "MC-GRF-25", "Pink Grapefruit Soda", "24 x 250ML", 6, "L", 44.40, True, (1, 2), 0.80),
    ("merri", "MC-GNB-25", "Ginger Beer", "24 x 250ML", 6, "L", 46.80, True, (1, 2), 0.75),
    ("merri", "MC-AGV-1L", "Agave Syrup", "6 x 1L", 6, "L", 96.00, True, (1, 1), 0.55),
    ("merri", "MC-CRN-1L", "Cranberry Juice", "12 x 1L", 12, "L", 54.00, True, (1, 1), 0.50),
    ("merri", "MC-BIT-200", "Aromatic Bitters", "6 x 200ML", 1.2, "L", 96.00, True, (1, 1), 0.30),
    ("merri", "MC-FOA-500", "Vegan Cocktail Foamer", "6 x 500ML", 3, "L", 108.00, True, (1, 1), 0.45),
    ("merri", "MC-CUC-10", "Cucumber Continental", "10KG", 10, "kg", 32.00, False, (1, 1), 0.45),

    # Merri Creek Providore - coffee and tea extras
    ("merri", "MC-CHA-1L", "Chai Concentrate", "6 x 1L", 6, "L", 78.00, True, (1, 2), 0.75),
    ("merri", "MC-MAT-500", "Matcha Powder Ceremonial", "500G", 0.5, "kg", 42.00, True, (1, 1), 0.50),
    ("merri", "MC-CHO-1K", "Drinking Chocolate", "10 x 1KG", 10, "kg", 110.00, True, (1, 1), 0.65),
    ("merri", "MC-VAN-1L", "Vanilla Syrup", "6 x 1L", 6, "L", 54.00, True, (1, 1), 0.60),
    ("merri", "MC-CAR-1L", "Caramel Syrup", "6 x 1L", 6, "L", 54.00, True, (1, 1), 0.55),
    ("merri", "MC-HON-3K", "Honey Yellow Box", "3KG", 3, "kg", 36.00, False, (1, 1), 0.40),
    ("merri", "MC-ALM-1L", "Almond Milk Barista", "12 x 1L", 12, "L", 42.00, True, (1, 2), 0.70),

    # Merri Creek Providore - second spirits and wine range
    ("merri", "MC-WHI-70", "Ironbark Blended Whisky", "6 x 700ML", 4.2, "L", 210.00, True, (1, 1), 0.55),
    ("merri", "MC-RUM-70", "Harbourside Spiced Rum", "6 x 700ML", 4.2, "L", 186.00, True, (1, 1), 0.45),
    ("merri", "MC-WWH-75", "Pinot Grigio White Wine", "12 x 750ML", 9, "L", 132.00, True, (1, 2), 0.70),
    ("merri", "MC-WRD-75", "Shiraz Red Wine", "12 x 750ML", 9, "L", 144.00, True, (1, 1), 0.65),
    ("merri", "MC-MIN-75", "Sparkling Mineral Water", "12 x 750ML", 9, "L", 42.00, True, (1, 2), 0.80),
    ("merri", "MC-RUW-70", "Caribbean White Rum", "6 x 700ML", 4.2, "L", 162.00, True, (1, 1), 0.60),
    ("merri", "MC-VDR-10", "Dry Vermouth", "6 x 1L", 6, "L", 90.00, True, (1, 1), 0.40),
    ("merri", "MC-AMR-70", "Amaretto Liqueur", "6 x 700ML", 4.2, "L", 168.00, True, (1, 1), 0.35),
    ("merri", "MC-TOM-1L", "Tomato Juice", "12 x 1L", 12, "L", 48.00, True, (1, 1), 0.45),
    # 100% fruit juice is GST-free in Australia, unlike the tomato juice above,
    # which is a flavoured beverage. Details like this are what make a demo
    # believable to anyone who has reconciled a real invoice.
    ("merri", "MC-OJU-2L", "Orange Juice Chilled", "6 x 2L", 12, "L", 39.60, False, (1, 2), 0.75),
    ("merri", "MC-OLI-2K", "Queen Green Olives", "2KG", 2, "kg", 26.00, True, (1, 1), 0.45),
    ("merri", "MC-CHR-1K", "Maraschino Cherries", "1KG", 1, "kg", 21.00, True, (1, 1), 0.40),
    ("merri", "MC-WOR-1L", "Worcestershire Sauce", "6 x 1L", 6, "L", 42.00, True, (1, 1), 0.30),
    ("merri", "MC-TAB-60", "Tabasco Pepper Sauce", "12 x 60ML", 0.72, "L", 63.00, True, (1, 1), 0.30),
]

BY_SUPPLIER = {}
for row in CATALOGUE:
    BY_SUPPLIER.setdefault(row[0], []).append(row)

PRODUCE = {"1455", "1460", "1470", "1510"}
EMBER = {"EC-HB-1K", "EC-SO-1K", "EC-DC-1K", "EC-CB-5L"}


def month_index(d):
    return (d.year - START.year) * 12 + (d.month - START.month)


def price_multiplier(code, d):
    """How each pack price moves over time. The planted problems live here."""
    m = month_index(d)
    mult = 1.0 + 0.003 * m                       # gentle background inflation

    if code == "HS-CFL-07":                      # 1. coffee liqueur creeps
        mult *= 1.0 + 0.028 * m

    if code in EMBER and d >= date(2026, 3, 2):  # 2. roaster lifts the range
        mult *= 1.09

    if code == "EC-SO-1K" and d < date(2026, 2, 16):   # 5. opening price
        mult *= 0.82

    if code == "MC-CHA-1L":                      # 8. chai concentrate creeps too
        mult *= 1.0 + 0.021 * m

    if code in PRODUCE:                          # 6. produce is a market
        mult *= 1.0 + 0.16 * random.uniform(-1, 1)
        if code == "1460" and d.month in (6, 7, 8):
            mult *= 1.55                         # limes in a Melbourne winter
    return mult


def pack_for(code, d):
    """3. The tequila bottle shrinks from 750ml to 700ml in May. Same case price."""
    if code == "HS-TEQ-75" and d >= date(2026, 5, 1):
        return "6 x 700ML", 4.2
    return None, None


def build_invoices():
    invoices = []
    counters = {k: 1000 for k in SUPPLIERS}
    for key, sup in SUPPLIERS.items():
        d = START
        while d <= END:
            if d.weekday() >= 5:
                d += timedelta(days=1)
                continue
            items = [i for i in BY_SUPPLIER[key] if random.random() < i[9]]
            if not items:
                items = [BY_SUPPLIER[key][0]]
            counters[key] += 1
            inv = {"invoice_no": f"{sup['prefix']}-{counters[key]}",
                   "supplier_key": key, "supplier_name": sup["name"],
                   "supplier_abn": sup["abn"], "invoice_date": d.isoformat(),
                   "lines": []}
            for (_s, code, desc, pack_raw, base_qty, base_unit, start_price,
                 gst, qty_range, _p) in items:
                new_pack, new_base = pack_for(code, d)
                if new_pack:
                    pack_raw, base_qty = new_pack, new_base
                unit_price = round(start_price * price_multiplier(code, d)
                                   * random.uniform(0.997, 1.003), 2)
                qty = random.randint(*qty_range)
                inv["lines"].append({
                    "line_no": len(inv["lines"]) + 1, "code": code,
                    "description": desc, "pack_raw": pack_raw, "qty": qty,
                    "unit_price_ex_gst": unit_price,
                    "amount_ex_gst": round(unit_price * qty, 2),
                    "gst_applies": gst,
                    "truth_base_qty": base_qty, "truth_base_unit": base_unit,
                    "truth_price_per_base_unit": round(unit_price / base_qty, 4),
                })
            # 7. a one-off surcharge on a Harbourline delivery day
            if key == "harbourline" and d == date(2026, 6, 16):
                inv["lines"].append({
                    "line_no": len(inv["lines"]) + 1, "code": "FRT-001",
                    "description": "Fuel Surcharge - June", "pack_raw": "1EA",
                    "qty": 1, "unit_price_ex_gst": 45.00, "amount_ex_gst": 45.00,
                    "gst_applies": True, "truth_base_qty": 1,
                    "truth_base_unit": "ea", "truth_price_per_base_unit": 45.0})
            inv["subtotal_ex_gst"] = round(sum(l["amount_ex_gst"] for l in inv["lines"]), 2)
            inv["gst"] = round(sum(l["amount_ex_gst"] * 0.1 for l in inv["lines"]
                                   if l["gst_applies"]), 2)
            inv["total_inc_gst"] = round(inv["subtotal_ex_gst"] + inv["gst"], 2)
            invoices.append(inv)
            d += timedelta(days=sup["cadence"])
    return invoices


# ---------------------------------------------------------------------------
def _header(c, inv, sup, y):
    c.setFont("Helvetica-Bold", 15)
    c.drawString(20 * mm, y, sup["name"])
    c.setFont("Helvetica", 8)
    c.drawString(20 * mm, y - 5 * mm, sup["address"])
    c.drawString(20 * mm, y - 9 * mm, f"ABN {sup['abn']}")

    c.setFont("Helvetica-Bold", 13)
    c.drawRightString(190 * mm, y, "TAX INVOICE")
    c.setFont("Helvetica", 9)
    c.drawRightString(190 * mm, y - 6 * mm, f"Invoice  {inv['invoice_no']}")
    c.drawRightString(190 * mm, y - 10 * mm, f"Date  {inv['invoice_date']}")
    c.drawRightString(190 * mm, y - 14 * mm, f"Account  {VENUE['account']}")

    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, y - 20 * mm, "Deliver to:")
    c.setFont("Helvetica-Bold", 9)
    c.drawString(20 * mm, y - 25 * mm, VENUE["name"])
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, y - 29 * mm, VENUE["address"])
    c.drawString(20 * mm, y - 33 * mm, VENUE["suburb"])
    return y - 42 * mm


def _totals(c, inv, y):
    c.setLineWidth(0.4)
    c.line(120 * mm, y + 4 * mm, 190 * mm, y + 4 * mm)
    c.setFont("Helvetica", 9)
    c.drawRightString(165 * mm, y - 1 * mm, "Subtotal (ex GST)")
    c.drawRightString(190 * mm, y - 1 * mm, f"{inv['subtotal_ex_gst']:,.2f}")
    c.drawRightString(165 * mm, y - 6 * mm, "GST")
    c.drawRightString(190 * mm, y - 6 * mm, f"{inv['gst']:,.2f}")
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(165 * mm, y - 12 * mm, "TOTAL")
    c.drawRightString(190 * mm, y - 12 * mm, f"${inv['total_inc_gst']:,.2f}")
    c.setFont("Helvetica", 7)
    c.setFillColor(colors.grey)
    c.drawString(20 * mm, 15 * mm,
                 "Items marked * are GST-free. Payment terms 14 days.")
    c.setFillColor(colors.black)


def render_columns_with_pack(c, inv, sup):
    """Bidvale: code, description, pack, qty, unit price, amount."""
    y = _header(c, inv, sup, 275 * mm)
    c.setFont("Helvetica-Bold", 8)
    for label, x, align in [("CODE", 20, "l"), ("DESCRIPTION", 45, "l"),
                            ("PACK", 112, "l"), ("QTY", 140, "r"),
                            ("UNIT $", 165, "r"), ("AMOUNT $", 190, "r")]:
        (c.drawString if align == "l" else c.drawRightString)(x * mm, y, label)
    c.setLineWidth(0.6)
    c.line(20 * mm, y - 2 * mm, 190 * mm, y - 2 * mm)
    y -= 7 * mm

    c.setFont("Helvetica", 8)
    for l in inv["lines"]:
        star = "" if l["gst_applies"] else " *"
        c.drawString(20 * mm, y, l["code"])
        c.drawString(45 * mm, y, (l["description"] + star)[:44])
        c.drawString(112 * mm, y, l["pack_raw"])
        c.drawRightString(140 * mm, y, str(l["qty"]))
        c.drawRightString(165 * mm, y, f"{l['unit_price_ex_gst']:,.2f}")
        c.drawRightString(190 * mm, y, f"{l['amount_ex_gst']:,.2f}")
        y -= 5 * mm
    _totals(c, inv, y - 3 * mm)


def render_pack_in_description(c, inv, sup):
    """Yarra Fresh: no pack column, the size is inside the description."""
    y = _header(c, inv, sup, 275 * mm)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(20 * mm, y, "ITEM")
    c.drawString(38 * mm, y, "PRODUCT")
    c.drawRightString(145 * mm, y, "QTY")
    c.drawRightString(168 * mm, y, "PRICE")
    c.drawRightString(190 * mm, y, "TOTAL")
    c.setLineWidth(0.6)
    c.line(20 * mm, y - 2 * mm, 190 * mm, y - 2 * mm)
    y -= 7 * mm

    c.setFont("Helvetica", 8)
    for l in inv["lines"]:
        star = "" if l["gst_applies"] else " *"
        c.drawString(20 * mm, y, l["code"])
        c.drawString(38 * mm, y, (l["description"] + star)[:60])
        c.drawRightString(145 * mm, y, f"{l['qty']}")
        c.drawRightString(168 * mm, y, f"{l['unit_price_ex_gst']:,.2f}")
        c.drawRightString(190 * mm, y, f"{l['amount_ex_gst']:,.2f}")
        y -= 5 * mm
    _totals(c, inv, y - 3 * mm)


def render_gst_column(c, inv, sup):
    """Bayside: pack first, and a GST column per line."""
    y = _header(c, inv, sup, 275 * mm)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(20 * mm, y, "PACK")
    c.drawString(45 * mm, y, "DESCRIPTION")
    c.drawString(112 * mm, y, "SKU")
    c.drawRightString(136 * mm, y, "QTY")
    c.drawRightString(158 * mm, y, "RATE")
    c.drawRightString(172 * mm, y, "GST")
    c.drawRightString(190 * mm, y, "NET")
    c.setLineWidth(0.6)
    c.line(20 * mm, y - 2 * mm, 190 * mm, y - 2 * mm)
    y -= 7 * mm

    c.setFont("Helvetica", 8)
    for l in inv["lines"]:
        c.drawString(20 * mm, y, l["pack_raw"])
        c.drawString(45 * mm, y, l["description"][:42])
        c.drawString(112 * mm, y, l["code"])
        c.drawRightString(136 * mm, y, str(l["qty"]))
        c.drawRightString(158 * mm, y, f"{l['unit_price_ex_gst']:,.2f}")
        c.drawRightString(172 * mm, y, "Y" if l["gst_applies"] else "N")
        c.drawRightString(190 * mm, y, f"{l['amount_ex_gst']:,.2f}")
        y -= 5 * mm
    _totals(c, inv, y - 3 * mm)


def render_minimal(c, inv, sup):
    """Northside: description and pack run together, few columns."""
    y = _header(c, inv, sup, 275 * mm)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(20 * mm, y, "DESCRIPTION")
    c.drawRightString(150 * mm, y, "QTY")
    c.drawRightString(170 * mm, y, "EACH")
    c.drawRightString(190 * mm, y, "VALUE")
    c.setLineWidth(0.6)
    c.line(20 * mm, y - 2 * mm, 190 * mm, y - 2 * mm)
    y -= 7 * mm

    c.setFont("Helvetica", 8)
    for l in inv["lines"]:
        star = "" if l["gst_applies"] else " *"
        c.drawString(20 * mm, y,
                     f"{l['code']} {l['description']} {l['pack_raw']}{star}"[:78])
        c.drawRightString(150 * mm, y, str(l["qty"]))
        c.drawRightString(170 * mm, y, f"{l['unit_price_ex_gst']:,.2f}")
        c.drawRightString(190 * mm, y, f"{l['amount_ex_gst']:,.2f}")
        y -= 5 * mm
    _totals(c, inv, y - 3 * mm)


def render_description_first(c, inv, sup):
    """Merri Creek: description first, code second, and column names nobody
    else uses. Reading this layout is the test of a header-driven reader: it
    never learns a supplier, it reads whatever the header row says."""
    y = _header(c, inv, sup, 275 * mm)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(20 * mm, y, "PARTICULARS")
    c.drawString(94 * mm, y, "REF")
    c.drawString(116 * mm, y, "UOM")
    c.drawRightString(143 * mm, y, "QTY")
    c.drawRightString(168 * mm, y, "U/PRICE")
    c.drawRightString(190 * mm, y, "EXT")
    c.setLineWidth(0.6)
    c.line(20 * mm, y - 2 * mm, 190 * mm, y - 2 * mm)
    y -= 7 * mm

    c.setFont("Helvetica", 8)
    for l in inv["lines"]:
        star = "" if l["gst_applies"] else " *"
        c.drawString(20 * mm, y, (l["description"] + star)[:40])
        c.drawString(94 * mm, y, l["code"])
        c.drawString(116 * mm, y, l["pack_raw"])
        c.drawRightString(143 * mm, y, str(l["qty"]))
        c.drawRightString(168 * mm, y, f"{l['unit_price_ex_gst']:,.2f}")
        c.drawRightString(190 * mm, y, f"{l['amount_ex_gst']:,.2f}")
        y -= 5 * mm
    _totals(c, inv, y - 3 * mm)


RENDERERS = {
    "columns_with_pack": render_columns_with_pack,
    "pack_in_description": render_pack_in_description,
    "gst_column": render_gst_column,
    "minimal": render_minimal,
    "description_first": render_description_first,
}


def main():
    invoices = build_invoices()
    for inv in invoices:
        sup = SUPPLIERS[inv["supplier_key"]]
        path = os.path.join(PDF_DIR, f"{inv['invoice_no']}.pdf")
        c = canvas.Canvas(path, pagesize=A4)
        RENDERERS[sup["layout"]](c, inv, sup)
        c.save()

    truth_path = os.path.join(ROOT, "data", "ground_truth.json")
    with open(truth_path, "w", encoding="utf-8") as fh:
        json.dump(invoices, fh, indent=1)

    lines = sum(len(i["lines"]) for i in invoices)
    spend = sum(i["subtotal_ex_gst"] for i in invoices)
    print(f"invoices   {len(invoices):,}")
    print(f"lines      {lines:,}")
    print(f"period     {START} to {END}")
    print(f"spend      ${spend:,.0f} ex GST")
    print(f"pdfs       {PDF_DIR}")
    print(f"truth      {truth_path}")


if __name__ == "__main__":
    main()
