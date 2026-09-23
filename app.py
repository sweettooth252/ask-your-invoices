"""
Ask Your Invoices - the first screen.

It answers one question before anyone types anything: how is this venue doing?
Spend, what moved it, what it costs to pour a drink, and what needs a phone call
this week. Every panel links to the page where you can check it.

    streamlit run app.py
"""

import streamlit as st

from askinv import insights as ins
from askinv.ui import download, money, page, pct, signed_money

sl, vocab = page("Overview", icon="📊")

st.title("How is Night & Day doing?")

grain = st.radio("Compare by", ["quarter", "month"], horizontal=True,
                 help="Quarters smooth out delivery timing; months show a change sooner.")
ps = ins.periods(sl, grain)
c1, c2 = st.columns(2)
after = c1.selectbox("This period", ps[::-1], index=0)
earlier = [p for p in ps if p < after]
before = c2.selectbox("Compared with", earlier[::-1],
                      index=0 if earlier else None)

if not earlier:
    st.info("Pick a later period to compare against; this is the first one on record.")
    st.stop()

h = ins.headline(sl, before=before, after=after, grain=grain)

# ------------------------------------------------------------------ the numbers
k1, k2, k3, k4 = st.columns(4)
k1.metric("Spend ex GST", money(h["spend"]),
          f"{signed_money(h['change'])} ({pct(h['change_pct'])})", delta_color="inverse")
k2.metric("From price changes", signed_money(h["price_effect"]),
          help="What the same basket would have cost at the old prices.")
k3.metric("From buying more or less", signed_money(h["volume_effect"]),
          help="Volume, at last period's prices. Price + volume = the whole change.")
k4.metric("Invoices", f"{h['invoices']}", f"{h['products']} products bought",
          delta_color="off")

pour = h["pour_cost"]
cols = st.columns(max(len(pour), 1))
for col, (_, row) in zip(cols, pour.iterrows()):
    col.metric(f"Pour cost · {row['drink_type']}", pct(row["pour_cost_pct"]),
               f"{row['change_pts']:+.1f} pts", delta_color="inverse")

st.caption(f"{grain.title()} {after} against {before}. Pour cost is ingredient cost ÷ menu "
           "price ex GST, at the prices actually paid. It assumes perfect pours: no "
           "spillage, wastage or comps.")

st.divider()

# ------------------------------------------------------------- what moved money
left, right = st.columns([1, 1])

with left:
    st.subheader("What moved the money")
    b = h["bridge"]
    st.bar_chart(ins.bridge_steps(b))
    st.caption(f"Price + volume + new + stopped = {signed_money(b.change)}, the whole "
               f"change. Reconciles: {b.reconciles}.")

with right:
    st.subheader("Biggest movers")
    m = ins.movers(sl, before, after, grain=grain, n=6)
    show = m[["product", "supplier", "price", "volume", "total", "reason"]].round(2)
    st.dataframe(show, hide_index=True, use_container_width=True)
    st.caption("'price' is the supplier charging differently; 'volume' is the venue "
               "buying differently. Only the first is worth a phone call.")

st.divider()

# ------------------------------------------------------------------- the menu
st.subheader("The menu, by what it costs to pour")
board = ins.menu_board(sl, period=after, grain=grain)
c1, c2 = st.columns([1, 1])
with c1:
    st.bar_chart(board.set_index("drink")["pour_cost_pct"])
with c2:
    pretty = board.copy()
    pretty["cost_per_serve"] = pretty["cost_per_serve"].map(lambda v: money(v, cents=True))
    pretty["menu_price"] = pretty["menu_price"].map(lambda v: money(v, cents=True))
    pretty["margin_per_serve"] = pretty["margin_per_serve"].map(lambda v: money(v, cents=True))
    pretty["pour_cost_pct"] = pretty["pour_cost_pct"].map(pct)
    st.dataframe(pretty, hide_index=True, use_container_width=True)
    download(board, f"menu_{after}.csv", "Download the menu board", key="dl_menu")

worst = board.iloc[0]
st.caption(f"Highest pour cost this {grain}: {worst['drink']} at "
           f"{pct(worst['pour_cost_pct'])}. Menu prices exclude GST here, because "
           "ingredient costs do too.")

st.divider()

# --------------------------------------------------------------- needs a call
st.subheader("Needs attention")
with st.spinner("Checking every product's price history..."):
    a = ins.alerts()

if a.empty:
    st.success("Nothing worth a phone call: no sustained price rises, pack shrinks or "
               "new charges.")
else:
    top = a[a["severity"] == "high"].head(3)
    if top.empty:
        top = a.head(3)
    for _, row in top.iterrows():
        st.markdown(f"**{row['title']}** — {money(row['annual_impact'])} a year"
                    if row["annual_impact"] else f"**{row['title']}**")
        st.caption(row["detail"])
        if row["drinks_affected"] != "-":
            st.caption(f"Hits: {row['drinks_affected']}")
    st.page_link("pages/3_Alerts.py", label="See every alert and its evidence →")

st.divider()
st.caption("Ask a question of your own on the **Ask** page · charts and CSV on "
           "**Analytics** · check a delivery before you pay it on **Invoice inbox**.")
