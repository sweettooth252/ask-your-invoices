"""
Tests for the dashboard numbers.

A dashboard that disagrees with the chat is worse than no dashboard, so these
check the panels against the semantic layer itself, and check the alerts find
the problems that were deliberately planted in the invoices.

Run after:  python make_invoices.py && python -m askinv.ingest
"""

from pathlib import Path

import pytest

from askinv import insights as ins
from askinv.db import DB_PATH
from askinv.layer import SemanticLayer

pytestmark = pytest.mark.skipif(not Path(DB_PATH).exists(),
                                reason="run make_invoices.py and askinv.ingest first")


@pytest.fixture(scope="module")
def sl():
    return SemanticLayer()


# ------------------------------------------------------------- the dashboard
def test_headline_spend_matches_the_semantic_layer(sl):
    """The big number on the first screen must be the same 'spend' everything
    else uses, not a second definition written for the dashboard."""
    h = ins.headline(sl)
    want = sl.query({"metrics": ["spend"],
                     "filters": [{"dimension": "quarter", "value": h["after"]}]})
    assert h["spend"] == pytest.approx(float(want["spend"].iloc[0]), abs=0.01)


def test_the_four_reasons_add_up_to_the_change(sl):
    h = ins.headline(sl)
    assert ins.bridge_steps(h["bridge"]).sum() == pytest.approx(h["change"], abs=0.01)


def test_menu_board_covers_every_drink_and_ranks_by_pour_cost(sl):
    board = ins.menu_board(sl)
    drinks = sl.values("drink", "drinks")
    assert set(board["drink"]) == set(drinks)
    assert board["pour_cost_pct"].is_monotonic_decreasing
    # margin is what's left after the pour, on the ex-GST price
    row = board.iloc[0]
    assert row["menu_price"] - row["cost_per_serve"] == pytest.approx(row["margin_per_serve"],
                                                                     abs=0.01)


def test_movers_say_whether_price_or_volume_did_it(sl):
    before, after = ins.last_two(sl)
    m = ins.movers(sl, before, after, n=5)
    assert set(m["reason"]) <= {"price", "volume"}
    for _, r in m.iterrows():
        bigger = "price" if abs(r["price"]) >= abs(r["volume"]) else "volume"
        assert r["reason"] == bigger


# ------------------------------------------------------------------- alerts
def test_alerts_find_the_planted_problems():
    """Three problems were planted in the invoices on purpose. If the detector
    stops finding them, it has been broken."""
    a = ins.alerts()
    titles = " | ".join(a["title"])
    assert "HS-CFL-07" in titles          # the coffee liqueur creeping up monthly
    assert "HS-TEQ-75" in titles          # 750ml -> 700ml at the same case price
    assert any(a["kind"] == "pack_shrink")
    assert any(a["kind"] == "cross_supplier")   # oat milk cheaper elsewhere


def test_alerts_say_which_drinks_they_hit():
    a = ins.alerts()
    liqueur = a[a["title"].str.contains("HS-CFL-07")].iloc[0]
    assert "Espresso Martini" in liqueur["drinks_affected"]


def test_seasonal_produce_is_not_reported_as_a_supplier_increase():
    """Limes cost more in a Melbourne winter. That is a season, not a supplier
    decision, and calling it an increase is how a tool loses its reader."""
    a = ins.alerts()
    limes = a[a["title"].str.contains("1460")]
    assert all(limes["kind"] != "creep")


# -------------------------------------------------------------- the inbox
def test_a_known_invoice_reconciles_and_prices_are_compared():
    from creep.extract import read_invoice
    inv = read_invoice(Path(DB_PATH).parent / "invoices" / "HS-1046.pdf")
    df = ins.check_invoice(inv)
    assert df.attrs["reconciles"]
    assert df.attrs["lines_sum_ex_gst"] == pytest.approx(inv.subtotal_ex_gst, abs=0.01)
    assert set(df["verdict"]) <= {"unchanged", "price up", "price down", "new product",
                                  "no unit price"}
    assert df["last_price"].notna().all()          # every product has been bought before


def test_a_product_never_bought_before_is_flagged_as_new():
    from creep.extract import Line, Invoice
    inv = Invoice(source_file="test.pdf", supplier_name="New Supplier Pty Ltd",
                  invoice_no="TEST-1", invoice_date="2026-07-01", subtotal_ex_gst=50.0,
                  lines=[Line(line_no=1, code="ZZ-NEW-01", description="Yuzu Juice",
                              pack_raw="6 x 500ML", qty=1, unit_price_ex_gst=50.0,
                              amount_ex_gst=50.0, base_qty=3.0, base_unit="L",
                              price_per_base_unit=16.6667)])
    df = ins.check_invoice(inv)
    assert df["verdict"].iloc[0] == "new product"
    assert df.attrs["reconciles"]


# ------------------------------------------------------------- data quality
def test_data_quality_reports_what_the_warehouse_holds(sl):
    dq = ins.data_quality(sl)
    assert dq["invoices"] == dq["reconciling"] > 0
    assert dq["uncategorised_products"] == 0
    assert dq["lines_without_unit_price"] == 0


def test_audit_trail_shows_the_checksum_for_every_invoice(sl):
    trail = ins.audit_trail(sl, limit=10)
    assert len(trail) == 10
    for _, row in trail.iterrows():
        assert row["subtotal_ex_gst"] == pytest.approx(row["lines_sum_ex_gst"], abs=0.02)


# --------------------------------------------------------------- the recipes
def test_costing_a_spec_by_hand_matches_the_stored_drink_cost(sl):
    """The what-if editor prices a spec with the same code the warehouse used.
    If these two ever disagree, the editor is lying to whoever is using it."""
    from askinv.menu import load_recipes
    recipes = load_recipes()
    book = ins.price_book(sl, period="2026-Q2", grain="quarter", recipes=recipes)
    spec = recipes["drinks"]["Margarita"]["spec"]
    cost, rows = ins.cost_of_spec(spec, book, recipes=recipes, label="Margarita")

    stored = sl.query({"metrics": ["cost_per_serve"],
                       "filters": [{"dimension": "drink", "value": "Margarita"},
                                   {"dimension": "quarter", "value": "2026-Q2"}]})
    assert cost == pytest.approx(float(stored["cost_per_serve"].iloc[0]), abs=1e-9)
    assert rows["cost"].sum() == pytest.approx(cost, abs=1e-9)


def test_a_batch_recipe_divides_by_what_it_makes(sl):
    """150g of filter beans brews ten cups. Cost per serve is a tenth of that,
    not the whole brew - a bar writes its specs per batch, not per cup."""
    from askinv.menu import load_recipes
    recipes = load_recipes()
    book = ins.price_book(sl, period="2026-Q2", grain="quarter", recipes=recipes)
    whole, _ = ins.cost_of_spec({"filter_beans": "150g"}, book, recipes=recipes)
    per_cup, _ = ins.cost_of_spec({"filter_beans": "150g"}, book, makes=10,
                                  recipes=recipes)
    assert per_cup == pytest.approx(whole / 10, abs=1e-12)

    stored = sl.query({"metrics": ["cost_per_serve"],
                       "filters": [{"dimension": "drink", "value": "Batch Filter"},
                                   {"dimension": "quarter", "value": "2026-Q2"}]})
    assert per_cup == pytest.approx(float(stored["cost_per_serve"].iloc[0]), abs=1e-9)


def test_pouring_less_costs_less(sl):
    from askinv.menu import load_recipes
    recipes = load_recipes()
    book = ins.price_book(sl, recipes=recipes)
    full, _ = ins.cost_of_spec({"tequila": "45ml"}, book, recipes=recipes)
    short, _ = ins.cost_of_spec({"tequila": "40ml"}, book, recipes=recipes)
    assert short < full
    assert short == pytest.approx(full * 40 / 45, abs=1e-9)


def test_the_price_a_drink_needs_hits_the_target_pour_cost():
    needed = ins.price_for_target_pour_cost(3.50, target=0.20)
    assert ins.pour_cost(3.50, needed) == pytest.approx(0.20, abs=1e-9)


def test_the_recipe_library_lists_every_drink():
    from askinv.menu import load_recipes
    recipes = load_recipes()
    lib = ins.recipe_library(recipes)
    assert set(lib["drink"]) == set(recipes["drinks"])
    assert (lib[lib["drink"] == "Batch Filter"]["serves_per_batch"] == 10).all()
