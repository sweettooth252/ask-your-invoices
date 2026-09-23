"""
Alerts - what changed, what it costs, and what to do about it.

The detector only reports what a venue can act on. Seasonal produce swings, a
one-off freight charge and moves too small to matter are described as what they
are, not dressed up as supplier increases. A tool that cries wolf gets ignored
by week two.
"""

import pandas as pd
import streamlit as st

from askinv import insights as ins
from askinv.ui import download, money, page, pct

sl, vocab = page("Alerts", icon="🔔")

st.title("What changed")

with st.spinner("Reading every product's price history..."):
    alerts = ins.alerts()

SEVERITY = {"high": "🔴 Worth a call", "medium": "🟠 Worth a look", "info": "⚪ For information"}

# ------------------------------------------------------------------- briefing
actionable = alerts[alerts["annual_impact"] > 0]
total = float(actionable["annual_impact"].sum())
k1, k2, k3 = st.columns(3)
k1.metric("Findings", len(alerts))
k2.metric("Worth a call", int((alerts["severity"] == "high").sum()))
k3.metric("At stake, a year", money(total),
          help="Annualised effect of the rises found, at current volumes.")

st.caption("If every rise below were rolled back, that's what the venue would keep "
           "over a year at today's volumes. It's an order of magnitude, not a promise.")

st.divider()

# -------------------------------------------------------------------- filters
c1, c2 = st.columns([2, 1])
severities = c1.multiselect("Show", list(SEVERITY), default=["high", "medium"],
                            format_func=lambda s: SEVERITY[s])
only_menu = c2.toggle("Only ones that hit a drink", value=False)

shown = alerts[alerts["severity"].isin(severities)] if severities else alerts
if only_menu:
    shown = shown[shown["drinks_affected"] != "-"]

if shown.empty:
    st.success("Nothing to report with those filters.")

for _, row in shown.iterrows():
    head = f"{SEVERITY[row['severity']]} — {row['title']}"
    if row["annual_impact"]:
        head += f" · {money(row['annual_impact'])}/yr"
    with st.expander(head, expanded=(row["severity"] == "high")):
        st.write(row["detail"])
        if row["action"]:
            st.info(f"**What to do:** {row['action']}")
        if row["drinks_affected"] != "-":
            st.caption(f"**Drinks affected:** {row['drinks_affected']}")
        ev = row["evidence"]
        if ev and isinstance(ev[0], (list, tuple)) and len(ev[0]) >= 2:
            df = pd.DataFrame(ev, columns=(["date", "price_per_unit"] if len(ev[0]) == 2
                                           else ["date", "pack", "unit_price", "price_per_unit"]))
            if len(ev[0]) == 2 and str(df["price_per_unit"].iloc[0]).replace(".", "", 1).isdigit():
                st.line_chart(df.set_index("date")["price_per_unit"])
            st.dataframe(df, hide_index=True, use_container_width=True)

if not alerts.empty:
    download(alerts.drop(columns=["evidence"]), "alerts.csv", "Download every finding",
             key="dl_alerts")

st.divider()

# -------------------------------------------------------------- recipe impact
st.subheader("What it did to the menu")
ps = ins.periods(sl, "quarter")
c1, c2 = st.columns(2)
after = c1.selectbox("This quarter", ps[::-1], index=0, key="ri_after")
earlier = [p for p in ps if p < after]
if earlier:
    before = c2.selectbox("Compared with", earlier[::-1], index=len(earlier) - 1,
                          key="ri_before")
    impact = ins.recipe_impact(sl, before, after)
    pretty = impact.copy()
    for col in ("cost_per_serve_before", "cost_per_serve_after", "change"):
        pretty[col] = pretty[col].map(lambda v: money(v, cents=True))
    pretty["change_pct"] = impact["change_pct"].map(pct)
    st.dataframe(pretty, hide_index=True, use_container_width=True)
    worst = impact.iloc[0]
    st.caption(f"Biggest rise: {worst['drink']}, {pct(worst['change_pct'])} dearer to make "
               f"than in {before}. A drink's spec doesn't change, so every cent of this "
               "is ingredient prices.")
    download(impact, f"recipe_impact_{before}_to_{after}.csv", key="dl_impact")

st.divider()

# --------------------------------------------------------------- data quality
st.subheader("Can these numbers be trusted?")
dq = ins.data_quality(sl)
k1, k2, k3, k4 = st.columns(4)
k1.metric("Invoices reconciling", f"{dq['reconciling']}/{dq['invoices']}",
          help="The lines read must add up to the subtotal printed on the invoice.")
k2.metric("Lines read", f"{dq['lines']:,}",
          f"{dq['lines_without_unit_price']} without a unit price", delta_color="off")
k3.metric("Uncategorised products", dq["uncategorised_products"])
k4.metric("Drink costs using a carried-forward price", pct(dq["carried_forward_share"]),
          help="A month with no delivery uses the last price paid, and says so.")
st.caption(f"Covering {dq['first_day']} to {dq['last_day']}.")
st.dataframe(dq["by_supplier"], hide_index=True, use_container_width=True)

with st.expander("Audit trail - every invoice loaded"):
    trail = ins.audit_trail(sl, limit=500)
    st.dataframe(trail, hide_index=True, use_container_width=True)
    st.caption("`subtotal_ex_gst` is what the PDF printed; `lines_sum_ex_gst` is what was "
               "read. They must match, and that check is a test in the repo.")
    download(trail, "audit_trail.csv", key="dl_trail")
