"""
Invoice inbox - check a delivery before you pay it.

Drop today's invoice PDFs in. Each one is read, its lines are added up against
the subtotal printed on the page, and every price is compared with the last
price paid for that product. Nothing is written to the warehouse unless you ask
for it, so this is safe to run on an invoice you're still arguing about.
"""

import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

from askinv import insights as ins
from askinv.ui import ROOT, download, money, page, pct

sl, vocab = page("Invoice inbox", icon="📥")

st.title("Check a delivery")
st.write("Drop in supplier invoice PDFs. Each line is checked against the last price "
         "paid for that product, and the invoice has to add up to its own printed "
         "subtotal before any of it is believed.")

with st.expander("No invoices handy? Use one from the sample folder"):
    st.write("The repo generates a year of invoices in `data/invoices/`. Upload any of "
             "them here to see the check run: `HS-1046.pdf` is the one where the "
             "tequila bottle quietly shrank from 750ml to 700ml.")

uploads = st.file_uploader("Invoice PDFs", type=["pdf"], accept_multiple_files=True)

if not uploads:
    st.stop()

history = ins.latest_prices()
saved = []

for up in uploads:
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(up.getbuffer())
        tmp_path = Path(tmp.name)

    try:
        from creep.extract import read_invoice
        invoice = read_invoice(tmp_path)
        invoice.source_file = up.name
        df = ins.check_invoice(invoice, history)
    except Exception as e:                                          # noqa: BLE001
        st.error(f"{up.name}: couldn't read this one - {e}")
        continue

    a = df.attrs
    st.subheader(f"{a['supplier'] or 'Unknown supplier'} · {a['invoice_no'] or up.name}")

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Invoice date", a["invoice_date"] or "not found")
    k2.metric("Lines read", len(df))
    k3.metric("Lines add up to", money(a["lines_sum_ex_gst"], cents=True))
    k4.metric("Printed subtotal", money(a["subtotal_ex_gst"], cents=True)
              if a["subtotal_ex_gst"] is not None else "not found")

    if a["reconciles"]:
        st.success("Reconciles: every line was read, and they add up to the subtotal "
                   "printed on the page.")
    else:
        st.warning("Does not reconcile. Either a line was misread or the invoice itself "
                   "doesn't add up. Check the lines below against the PDF before paying.")

    moved = df[df["verdict"].isin(["price up", "price down"])]
    fresh = df[df["verdict"] == "new product"]
    c1, c2, c3 = st.columns(3)
    c1.metric("Prices moved", len(moved))
    c2.metric("New products", len(fresh))
    c3.metric("Unchanged", int((df["verdict"] == "unchanged").sum()))

    show = df.copy()
    show["change_pct"] = show["change_pct"].map(lambda v: "-" if pd.isna(v) else pct(v))
    show["per_base_unit"] = show["per_base_unit"].map(
        lambda v: "-" if pd.isna(v) else f"${v:,.4f}")
    show["last_price"] = show["last_price"].map(
        lambda v: "-" if v is None or pd.isna(v) else f"${v:,.4f}")
    st.dataframe(show[["line", "code", "description", "pack", "qty", "unit_price_ex_gst",
                       "per_base_unit", "unit", "last_price", "change_pct", "verdict",
                       "last_seen"]],
                 hide_index=True, use_container_width=True)
    st.caption("`per_base_unit` is the price per litre or per kilo. It is the column a "
               "pack-size change shows up in, while `unit_price_ex_gst` stays still.")

    if len(moved):
        hits = ins.drinks_hit_by(moved["code"].dropna())
        rows = [{"product": r["description"],
                 "change": pct(r["change_pct"]),
                 "drinks affected": ", ".join(hits.get(str(r["code"]).upper(), [])) or "-"}
                for _, r in moved.iterrows()]
        st.markdown("**What these price moves hit on the menu**")
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    download(df, f"check_{a['invoice_no'] or up.name}.csv", "Download this check",
             key=f"dl_{up.name}")

    saved.append((up, invoice))

st.divider()

# ---------------------------------------------------------------- keeping it
st.subheader("Keep these invoices?")
st.caption("Checking is read-only. If you want these included in the dashboard and the "
           "chat, save them into `data/invoices/` and rebuild the warehouse. On the "
           "hosted demo the server's disk is wiped when the app restarts, so this is "
           "really for running it on your own machine.")

if st.button("Save and rebuild the warehouse", type="primary"):
    folder = ROOT / "data" / "invoices"
    folder.mkdir(parents=True, exist_ok=True)
    for up, _invoice in saved:
        (folder / up.name).write_bytes(up.getbuffer())
    with st.spinner("Rebuilding..."):
        import subprocess
        import sys
        result = subprocess.run([sys.executable, "-m", "askinv.ingest"], cwd=ROOT,
                                capture_output=True, text=True)
    if result.returncode == 0:
        st.cache_resource.clear()
        st.cache_data.clear()
        st.success(f"Saved {len(saved)} invoice(s) and rebuilt the warehouse.")
        st.code(result.stdout or "", language="text")
    else:
        st.error("The rebuild failed. Nothing was changed in the database.")
        st.code(result.stderr[-2000:], language="text")
