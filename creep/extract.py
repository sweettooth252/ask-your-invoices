"""
Read line items out of a supplier invoice PDF.

No per-supplier templates. The reader finds the table by looking for the
vertical gaps that run down the page between columns, which every printed
invoice has and no two suppliers put in the same place. That is what lets one
piece of code read four different layouts, and it is why adding a fifth
supplier usually needs no code at all.

    from creep.extract import read_invoice
    inv = read_invoice("data/invoices/BV-1030.pdf")
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

import pdfplumber

from creep.units import parse_pack, find_pack_in_text

# Column headers, as the four suppliers spell them. Add to the right-hand
# lists when a new supplier turns up; that is normally the only change needed.
HEADER_SYNONYMS = {
    "code": ["code", "item", "sku", "item code", "product code", "item no", "ref"],
    "description": ["description", "product", "item description", "details",
                    "goods", "particulars"],
    "pack": ["pack", "size", "pack size", "unit", "uom"],
    "qty": ["qty", "quantity", "units", "ordered", "delivered"],
    "unit_price": ["unit $", "unit price", "price", "rate", "each", "unit",
                   "u/price", "unit cost"],
    "amount": ["amount $", "amount", "total", "value", "net", "ext", "extension",
               "line total"],
    "gst_flag": ["gst", "tax", "gst?"],
}

# "unit" is ambiguous - it means pack size on one invoice and unit price on
# another. Resolved by position: a column to the left of qty is a pack.
AMBIGUOUS = {"unit", "units"}

ROW_TOLERANCE = 2.5        # points; words within this vertical distance are one row
MIN_CORRIDOR = 5.0         # points; a gap narrower than this is not a column break
STOP_WORDS = ("subtotal", "sub total", "total", "gst", "amount due", "balance")

# A supplier code: has a digit, and is either hyphenated (OIL-2040, D-118),
# all digits (1201), or letters then digits (D101). "12oz" must NOT match.
CODE_RE = re.compile(
    r"^(?=[A-Za-z0-9/-]+$)(?=.*\d)"
    r"(?:[A-Za-z0-9]+[-/][A-Za-z0-9/-]*|\d{3,}|[A-Za-z]{1,4}\d{2,})$")
MONEY_RE = re.compile(r"^-?\$?\d{1,3}(?:,\d{3})*(?:\.\d+)?$|^-?\$?\d+(?:\.\d+)?$")
DATE_RES = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "ymd"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), "dmy"),
    (re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b"), "dmy"),
]


@dataclass
class Line:
    line_no: int
    code: str | None = None
    description: str | None = None
    pack_raw: str | None = None
    qty: float | None = None
    unit_price_ex_gst: float | None = None
    amount_ex_gst: float | None = None
    base_qty: float | None = None
    base_unit: str | None = None
    price_per_base_unit: float | None = None
    confidence: float = 1.0
    issues: list = field(default_factory=list)


@dataclass
class Invoice:
    source_file: str
    supplier_name: str | None = None
    supplier_abn: str | None = None
    invoice_no: str | None = None
    invoice_date: str | None = None
    lines: list = field(default_factory=list)
    subtotal_ex_gst: float | None = None
    total_inc_gst: float | None = None

    def to_dict(self):
        d = asdict(self)
        d["lines"] = [asdict(l) if not isinstance(l, dict) else l for l in self.lines]
        return d


# ---------------------------------------------------------------------------
# Words -> rows
# ---------------------------------------------------------------------------
def _rows(words):
    """Group words into printed lines by their vertical position."""
    rows, current, anchor = [], [], None
    for w in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
        if anchor is None or abs(w["top"] - anchor) <= ROW_TOLERANCE:
            current.append(w)
            anchor = w["top"] if anchor is None else anchor
        else:
            rows.append(sorted(current, key=lambda w: w["x0"]))
            current, anchor = [w], w["top"]
    if current:
        rows.append(sorted(current, key=lambda w: w["x0"]))
    return rows


def _numeric_words(row):
    return sum(1 for w in row if _to_number(w["text"]) is not None)


def _looks_like_item_row(row):
    """An item row carries at least two numbers: a quantity and a price.
    Banners, notes and wrapped descriptions do not, and must be kept out of
    the column detection or they fill in the gaps it depends on."""
    return _numeric_words(row) >= 2


def _row_text(row):
    return " ".join(w["text"] for w in row)


def _find_header_row(rows):
    """The header row is the one that matches the most known column names."""
    best, best_score = None, 0
    all_names = {s for names in HEADER_SYNONYMS.values() for s in names}
    for i, row in enumerate(rows):
        text = _row_text(row).lower()
        score = sum(1 for name in all_names if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", text))
        if score > best_score:
            best, best_score = i, score
    return best if best_score >= 3 else None


def _corridors(rows, page_width):
    """Find the vertical white gaps that run down the whole table."""
    bins = [0] * (int(page_width) + 2)
    for row in rows:
        for w in row:
            for x in range(int(w["x0"]), min(int(w["x1"]) + 1, len(bins))):
                bins[x] += 1

    gaps, start = [], None
    for x, count in enumerate(bins):
        if count == 0 and start is None:
            start = x
        elif count > 0 and start is not None:
            if x - start >= MIN_CORRIDOR:
                gaps.append((start, x))
            start = None
    if start is not None and len(bins) - start >= MIN_CORRIDOR:
        gaps.append((start, len(bins)))
    return [(a + b) / 2 for a, b in gaps]


def _columns_from_boundaries(boundaries, page_width):
    edges = [0.0] + sorted(boundaries) + [float(page_width)]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


def _cell_text(row, lo, hi):
    inside = [w["text"] for w in row if (w["x0"] + w["x1"]) / 2 >= lo
              and (w["x0"] + w["x1"]) / 2 < hi]
    return " ".join(inside).strip()


def _map_headers(header_row, columns):
    """Work out which column is which field, then fix the ambiguous ones."""
    mapping = {}
    for idx, (lo, hi) in enumerate(columns):
        raw = _cell_text(header_row, lo, hi).lower().strip()
        # "UNIT $", "AMOUNT $", "QTY:" all normalise to a bare word, otherwise
        # "unit $" matches the pack synonym "unit" and steals the price column.
        text = re.sub(r"[\$#:.]", "", raw).strip()
        if not text:
            continue
        if text in AMBIGUOUS:
            mapping.setdefault(idx, "_ambiguous_unit")
            continue
        for field_name, names in HEADER_SYNONYMS.items():
            if any(text == n for n in names):
                mapping.setdefault(idx, field_name)
                break

    qty_idx = next((i for i, f in mapping.items() if f == "qty"), None)
    for idx, f in list(mapping.items()):
        if f == "_ambiguous_unit":
            mapping[idx] = "pack" if (qty_idx is not None and idx < qty_idx) else "unit_price"
    return mapping


def _to_number(text):
    if not text:
        return None
    cleaned = text.replace("$", "").replace(",", "").strip()
    if not MONEY_RE.match(text.strip()) and not re.fullmatch(r"-?\d+(\.\d+)?", cleaned):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _split_merged(text):
    """Some invoices run code, description and pack together in one column.
    Pull them apart: a code-looking first token, a pack-looking tail."""
    if not text:
        return None, None, None
    tokens = text.split()
    code = None
    if tokens and CODE_RE.match(tokens[0]) and any(ch.isdigit() for ch in tokens[0]):
        code, tokens = tokens[0], tokens[1:]
    rest = " ".join(tokens)
    pack = find_pack_in_text(rest)
    if pack:
        rest = rest.replace(pack, " ")
    return code, re.sub(r"\s+", " ", rest).strip(" *"), pack


# ---------------------------------------------------------------------------
# Document level fields
# ---------------------------------------------------------------------------
def _find_meta(rows):
    meta = {}
    for row in rows[:14]:
        text = _row_text(row)
        m = re.search(r"(?:invoice|inv|tax invoice)\s*(?:no\.?|#|number)?\s*[:\-]?\s*([A-Z]{1,4}[-/]?\d{3,})",
                      text, re.IGNORECASE)
        if m and "invoice_no" not in meta:
            meta["invoice_no"] = m.group(1)
        m = re.search(r"abn\s*[:\-]?\s*((?:\d[\s\-]?){11})", text, re.IGNORECASE)
        if m and "supplier_abn" not in meta:
            meta["supplier_abn"] = re.sub(r"[\s\-]", " ", m.group(1)).strip()
        if "invoice_date" not in meta:
            for rx, order in DATE_RES:
                m = rx.search(text)
                if m:
                    a, b, c = m.groups()
                    meta["invoice_date"] = (f"{a}-{b}-{c}" if order == "ymd"
                                            else f"{c}-{int(b):02d}-{int(a):02d}")
                    break
    if rows:
        # The supplier name is top left. "TAX INVOICE" is top right on the same
        # line, so keep only the words on the left of the page.
        left = [w for w in rows[0] if w["x0"] < 300]
        meta["supplier_name"] = " ".join(w["text"] for w in left).strip()
    return meta


def _find_totals(rows):
    out = {}
    for row in rows:
        text = _row_text(row).lower()
        nums = [_to_number(w["text"]) for w in row]
        nums = [n for n in nums if n is not None]
        if not nums:
            continue
        if "subtotal" in text or "sub total" in text:
            out.setdefault("subtotal_ex_gst", nums[-1])
        elif re.search(r"\btotal\b", text) and "subtotal" not in text:
            out["total_inc_gst"] = nums[-1]
    return out


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------
def read_invoice(path: str | Path) -> Invoice:
    path = Path(path)
    inv = Invoice(source_file=path.name)

    pages = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            pages.append((page.extract_words(use_text_flow=False,
                                             keep_blank_chars=False), page.width))

    for page_no, (words, width) in enumerate(pages):
        rows = _rows(words)
        if page_no == 0:
            inv.__dict__.update(_find_meta(rows))
        inv.__dict__.update({k: v for k, v in _find_totals(rows).items()})
        _read_page(inv, rows, width)

    for n, line in enumerate(inv.lines, start=1):
        line.line_no = n
    return inv


def _read_page(inv: Invoice, rows, width) -> None:
    """Pull the line items off one page. A long order runs over several pages
    and each one repeats the column headers, so each page is read on its own."""
    header_idx = _find_header_row(rows)
    if header_idx is None:
        return

    # The table runs from below the header to the first totals row
    body = []
    for row in rows[header_idx + 1:]:
        text = _row_text(row).lower()
        if any(sw in text for sw in STOP_WORDS) and len(row) <= 4:
            break
        if all(w["x0"] > width * 0.55 for w in row):
            break
        body.append(row)

    item_rows = [r for r in body if _looks_like_item_row(r)]
    if not item_rows:
        return

    boundaries = _corridors(item_rows, width)
    columns = _columns_from_boundaries(boundaries, width)
    mapping = _map_headers(rows[header_idx], columns)

    for n, row in enumerate(item_rows, start=1):
        cells = {}
        for idx, (lo, hi) in enumerate(columns):
            fname = mapping.get(idx)
            if fname:
                cells[fname] = _cell_text(row, lo, hi)

        line = Line(line_no=n)
        line.code = (cells.get("code") or None)
        line.description = (cells.get("description") or None)
        line.pack_raw = (cells.get("pack") or None)
        line.qty = _to_number(cells.get("qty"))
        line.unit_price_ex_gst = _to_number(cells.get("unit_price"))
        line.amount_ex_gst = _to_number(cells.get("amount"))

        # Layouts that merge code + description + pack into one column
        if line.description and (not line.code or not line.pack_raw):
            code, desc, pack = _split_merged(line.description)
            line.code = line.code or code
            line.description = desc or line.description
            line.pack_raw = line.pack_raw or pack
        if line.description and not line.pack_raw:
            line.pack_raw = find_pack_in_text(line.description)
        if line.description:
            line.description = line.description.strip(" *").strip()

        if line.qty is None and line.unit_price_ex_gst is None and line.amount_ex_gst is None:
            continue   # a wrapped description line, not a real item

        base_qty, base_unit = parse_pack(line.pack_raw)
        if base_qty and line.unit_price_ex_gst is not None:
            line.base_qty = base_qty
            line.base_unit = base_unit
            line.price_per_base_unit = round(line.unit_price_ex_gst / base_qty, 4)
        else:
            line.issues.append("pack size not understood")
            line.confidence -= 0.4

        # Arithmetic check: qty x unit price should equal the line amount.
        # When it does not, something was read out of the wrong column.
        if None not in (line.qty, line.unit_price_ex_gst, line.amount_ex_gst):
            expected = line.qty * line.unit_price_ex_gst
            if abs(expected - line.amount_ex_gst) > max(0.02, abs(expected) * 0.01):
                line.issues.append("qty x unit price does not equal amount")
                line.confidence -= 0.5
        else:
            line.issues.append("missing a number")
            line.confidence -= 0.3

        line.confidence = round(max(0.0, line.confidence), 2)
        inv.lines.append(line)


def read_folder(folder: str | Path) -> list[Invoice]:
    return [read_invoice(p) for p in sorted(Path(folder).glob("*.pdf"))]
