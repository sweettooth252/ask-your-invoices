"""
Tests for the promises the app makes.

Run after:  python make_invoices.py && python -m askinv.ingest
"""

import sqlite3
from pathlib import Path

import pytest

from askinv.answer import ask, verify_numbers
from askinv.bridge import explain_change
from askinv.db import DB_PATH, ENGINE, connect
from askinv.layer import SemanticError, SemanticLayer
from askinv.menu import explain_drink_change
from askinv.planner import Vocabulary, validate

pytestmark = pytest.mark.skipif(not Path(DB_PATH).exists(),
                                reason="run make_invoices.py and askinv.ingest first")


@pytest.fixture(scope="module")
def sl():
    return SemanticLayer()


@pytest.fixture(scope="module")
def vocab(sl):
    return Vocabulary(sl)


def raw(sql):
    con = connect(DB_PATH)
    try:
        return con.execute(sql).df()
    finally:
        con.close()


# ---------------------------------------------------------------- the data
def test_every_invoice_reconciles_to_its_printed_subtotal():
    """The invoice carries its own checksum. If the lines read don't add up to
    the subtotal printed on the page, something was misread."""
    df = raw("SELECT COUNT(*) AS n, SUM(reconciles) AS ok FROM fct_invoice")
    assert df["n"].iloc[0] > 0
    assert df["ok"].iloc[0] == df["n"].iloc[0]


def test_categories_match_whole_words():
    """'Single Origin' contains 'gin'. A substring match once filed coffee
    beans under Spirits, and every spirits total was wrong."""
    df = raw("SELECT category FROM dim_product WHERE product_name = 'Single Origin Filter Beans'")
    assert df["category"].iloc[0] == "Coffee"


def test_nothing_is_uncategorised():
    df = raw("SELECT COUNT(*) AS n FROM dim_product WHERE category = 'Uncategorised'")
    assert df["n"].iloc[0] == 0


# ---------------------------------------------------------------- the layer
def test_spend_matches_hand_written_sql(sl):
    got = sl.query({"metrics": ["spend"]})["spend"].iloc[0]
    want = raw("SELECT SUM(amount_ex_gst) AS v FROM fct_invoice_line")["v"].iloc[0]
    assert got == pytest.approx(want, abs=0.01)


def test_receipts_add_up_to_the_answer(sl):
    """Drill-through is only worth anything if the lines shown are exactly the
    lines counted. Every supplier's spend must equal the sum of its receipts."""
    q = {"metrics": ["spend"], "dimensions": ["supplier"]}
    for _, row in sl.query(q).iterrows():
        lines = sl.lineage(q, row={"supplier": row["supplier"]}, limit=100000)
        assert lines["amount_ex_gst"].sum() == pytest.approx(row["spend"], abs=0.01)


def test_price_per_unit_refuses_to_mix_units(sl):
    with pytest.raises(SemanticError, match="litres, kilos"):
        sl.query({"metrics": ["avg_price_per_unit"], "dimensions": ["category"]})


def test_price_per_unit_works_once_split_by_unit(sl):
    df = sl.query({"metrics": ["avg_price_per_unit"], "dimensions": ["category", "unit"]})
    assert len(df) > 0 and df["avg_price_per_unit"].notna().all()


def test_cost_per_serve_refuses_to_add_up_drinks(sl):
    with pytest.raises(SemanticError, match="different drinks"):
        sl.query({"metrics": ["cost_per_serve"], "dimensions": ["drink_type"]})


def test_pour_cost_across_drinks_is_allowed(sl):
    """Pour cost % across several drinks is defined (as if one of each were
    sold), so it must NOT be blocked by the drink rule."""
    df = sl.query({"metrics": ["pour_cost_pct"], "dimensions": ["drink_type"]})
    assert set(df["drink_type"]) == {"Cocktail", "Coffee"}


def test_purchases_and_drinks_are_never_combined(sl):
    with pytest.raises(SemanticError, match="separately"):
        sl.query({"metrics": ["spend", "cost_per_serve"]})


def test_supplier_does_not_apply_to_drinks(sl):
    with pytest.raises(SemanticError, match="doesn't apply"):
        sl.query({"metrics": ["cost_per_serve"], "dimensions": ["supplier"]})


# ---------------------------------------------------------------- the menu
def test_flat_white_cost_matches_a_hand_calculation(sl):
    """18g of house blend + 180ml of milk, at the prices actually paid in the
    quarter, calculated directly from the invoice lines."""
    q = "2026-Q2"
    prices = raw(f"""
        SELECT p.code, SUM(l.amount_ex_gst) / SUM(l.qty_base) AS ppu
        FROM fct_invoice_line l
        JOIN dim_product p ON l.product_id = p.product_id
        JOIN dim_date d ON l.date_key = d.date_key
        WHERE d.quarter = '{q}' AND p.code IN ('EC-HB-1K', 'D-101')
        GROUP BY p.code""").set_index("code")["ppu"]
    want = 0.018 * prices["EC-HB-1K"] + 0.180 * prices["D-101"]
    got = sl.query({"metrics": ["cost_per_serve"],
                    "filters": [{"dimension": "drink", "value": "Flat White"},
                                {"dimension": "quarter", "value": q}]})["cost_per_serve"].iloc[0]
    assert got == pytest.approx(want, abs=0.0001)


def test_drink_cost_change_is_fully_explained_by_ingredients(sl):
    ch = explain_drink_change(sl, "Margarita", "2025-Q3", "2026-Q2")
    assert ch["change"].sum() == pytest.approx(
        ch.attrs["total_after"] - ch.attrs["total_before"], abs=1e-9)


# ---------------------------------------------------------------- the bridge
@pytest.mark.parametrize("before,after", [("2025-Q3", "2026-Q2"), ("2026-Q1", "2026-Q2")])
def test_spend_bridge_reconciles_to_the_cent(sl, before, after):
    b = explain_change(sl, "quarter", before, after)
    assert b.reconciles
    assert b.price + b.volume + b.new + b.stopped == pytest.approx(b.change, abs=0.01)


# ---------------------------------------------------------------- the AI
def test_invented_filter_values_are_rejected(sl, vocab):
    plan = {"intent": "query", "query": {"metrics": ["spend"], "filters": [
        {"dimension": "supplier", "op": "=", "value": "Imaginary Wines Pty Ltd"}]}}
    with pytest.raises(SemanticError, match="no supplier"):
        validate(plan, sl, vocab)


def test_invented_measures_are_rejected(sl, vocab):
    with pytest.raises(SemanticError):
        validate({"intent": "query", "query": {"metrics": ["profit"]}}, sl, vocab)


def test_number_checker_catches_a_rounded_figure():
    draft = "A Margarita cost $3.40 to make in 2025-Q3 and $3.51 in 2026-Q2 (+3.1%)."
    assert verify_numbers("It now costs about $3.50.", draft) == ["$3.50"]
    assert verify_numbers("It now costs $3.51, up 3.1% since 2025-Q3.", draft) == []


def test_number_checker_is_not_fooled_by_period_labels():
    """The 3 in '2025-Q3' must not vouch for an invented '3%'."""
    draft = "A Margarita cost $3.40 to make in 2025-Q3 and $3.51 in 2026-Q2 (+3.1%)."
    assert verify_numbers("Up 3% since 2025-Q3.", draft) == ["3%"]


def test_every_answer_comes_with_receipts(sl, vocab):
    for q in ["How much did we spend with each supplier?",
              "Why did we spend more at Ember this quarter than last quarter?",
              "Why does the margarita cost more to make this quarter than 2025-Q3?"]:
        a = ask(q, sl, "rules", vocab)
        assert not a.refused, a.text
        assert a.citations is not None and len(a.citations) > 0
