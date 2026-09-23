"""
Analytics - spend and prices, without having to ask a question.

Same definitions as the chat: every table here is a semantic-layer query, so a
number on this page and the same number in an answer come from one place.
"""

import streamlit as st

from askinv import insights as ins
from askinv.layer import SemanticError
from askinv.ui import download, money, page, pct

sl, vocab = page("Analytics", icon="📈")

st.title("Spend and prices")

# ------------------------------------------------------------------- filters
grain = st.radio("Time grain", ["month", "quarter", "week"], horizontal=True)
c1, c2 = st.columns(2)
suppliers = c1.multiselect("Suppliers", sl.values("supplier"), default=[])
categories = c2.multiselect("Categories", sl.values("category"), default=[])

filters = []
if suppliers:
    filters.append({"dimension": "supplier", "op": "in", "value": suppliers})
if categories:
    filters.append({"dimension": "category", "op": "in", "value": categories})

scope = " · ".join(filter(None, [", ".join(suppliers), ", ".join(categories)])) or "all suppliers"
st.caption(f"Showing: {scope}. Amounts exclude GST.")

tab_spend, tab_prices, tab_compare = st.tabs(["Spend", "Price trends", "Period on period"])

# --------------------------------------------------------------------- spend
with tab_spend:
    over_time = ins.spend_by(sl, [grain], filters, metrics=("spend", "invoices"))
    st.subheader(f"Spend by {grain}")
    st.line_chart(over_time.set_index(grain)["spend"])
    st.dataframe(over_time.round(2), hide_index=True, use_container_width=True)
    download(over_time, f"spend_by_{grain}.csv", key="dl_time")

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("By supplier")
        by_supplier = ins.spend_by(sl, ["supplier"], filters)
        st.bar_chart(by_supplier.set_index("supplier")["spend"])
        st.dataframe(by_supplier.round(2), hide_index=True, use_container_width=True)
        download(by_supplier, "spend_by_supplier.csv", key="dl_supplier")
    with c2:
        st.subheader("By category")
        by_category = ins.spend_by(sl, ["category"], filters)
        st.bar_chart(by_category.set_index("category")["spend"])
        st.dataframe(by_category.round(2), hide_index=True, use_container_width=True)
        download(by_category, "spend_by_category.csv", key="dl_category")

    st.subheader("Every product")
    by_product = ins.spend_by(sl, ["product", "supplier", "category"], filters,
                              metrics=("spend", "invoices"))
    by_product = by_product.sort_values("spend", ascending=False)
    st.dataframe(by_product.round(2), hide_index=True, use_container_width=True)
    download(by_product, "spend_by_product.csv", key="dl_product")

# -------------------------------------------------------------- price trends
with tab_prices:
    st.subheader("What we pay per litre or per kilo")
    st.caption("The invoice's own price column hides a pack-size change. This one "
               "doesn't: price per base unit is what actually changed.")
    products = sl.values("product")
    default = "Agave Azul Blanco Tequila" if "Agave Azul Blanco Tequila" in products else products[0]
    product = st.selectbox("Product", products, index=products.index(default))
    trend = ins.price_trend(sl, product, grain=grain)
    if trend.empty:
        st.info("Nothing bought in this period.")
    else:
        unit = trend["unit"].iloc[0]
        st.line_chart(trend.set_index(grain)["avg_price_per_unit"])
        first, last = trend["avg_price_per_unit"].iloc[0], trend["avg_price_per_unit"].iloc[-1]
        st.metric(f"Price per {unit}", money(last, cents=True),
                  f"{pct((last / first) - 1)} since {trend[grain].iloc[0]}",
                  delta_color="inverse")
        st.dataframe(trend.round(4), hide_index=True, use_container_width=True)
        download(trend, f"price_trend_{product}.csv".replace(" ", "_"), key="dl_trend")

    st.subheader("Price per unit by product")
    st.caption("Split by unit, because litres and kilos can't be averaged together. "
               "Asking for it across categories is refused, on purpose.")
    try:
        table = sl.query({"metrics": ["avg_price_per_unit", "quantity"],
                          "dimensions": ["product", "unit"], "filters": filters})
        st.dataframe(table.round(4), hide_index=True, use_container_width=True)
        download(table, "price_per_unit.csv", key="dl_ppu")
    except SemanticError as e:
        st.warning(str(e))

# ---------------------------------------------------------- period on period
with tab_compare:
    st.subheader("Period on period")
    ps = ins.periods(sl, grain)
    c1, c2 = st.columns(2)
    after = c1.selectbox("This period", ps[::-1], index=0, key="cmp_after")
    earlier = [p for p in ps if p < after]
    if not earlier:
        st.info("Nothing earlier to compare with.")
    else:
        before = c2.selectbox("Compared with", earlier[::-1], index=0, key="cmp_before")
        b = ins.explain_change(sl, grain, before, after, filters)
        k1, k2, k3 = st.columns(3)
        k1.metric(f"Spend {after}", money(b.spend_after), money(b.change),
                  delta_color="inverse")
        k2.metric("Price effect", money(b.price))
        k3.metric("Volume effect", money(b.volume))
        st.bar_chart(ins.bridge_steps(b))
        st.caption(f"The four bars add up to the whole change. Reconciles: {b.reconciles}.")
        drivers = b.by_product.sort_values("total", key=abs, ascending=False)
        st.dataframe(drivers.round(2), hide_index=True, use_container_width=True)
        download(drivers, f"change_{before}_to_{after}.csv", key="dl_bridge")
