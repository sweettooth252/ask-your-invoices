"""
Shared furniture for the Streamlit pages: loading, formatting, downloads.

Layout only. Every number shown on a page is computed in askinv/insights.py or
in the semantic layer, so the dashboard and the chat can never disagree.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import streamlit as st

from askinv.db import DB_PATH, ENGINE, connect
from askinv.layer import SemanticLayer
from askinv.planner import Vocabulary

ROOT = Path(__file__).resolve().parent.parent
VENUE = "Night & Day"
VENUE_NOTE = "Specialty coffee by day, cocktails by night. Collingwood. (Fictional.)"


def ensure_data():
    """On a fresh server the invoices aren't in git, so build them once."""
    if Path(DB_PATH).exists():
        return
    with st.spinner("First start: generating the invoices and building the warehouse..."):
        subprocess.run([sys.executable, "make_invoices.py"], cwd=ROOT, check=True)
        subprocess.run([sys.executable, "-m", "askinv.ingest"], cwd=ROOT, check=True)


@st.cache_resource
def load():
    sl = SemanticLayer()
    return sl, Vocabulary(sl)


@st.cache_data(show_spinner=False)
def invoice_stats():
    con = connect()
    try:
        return con.execute("SELECT COUNT(*) AS n, SUM(reconciles) AS ok, "
                           "SUM(subtotal_ex_gst) AS spend FROM fct_invoice").df().iloc[0].to_dict()
    finally:
        con.close()


def page(title: str, icon: str = "🍸", layout: str = "wide"):
    """Every page starts the same way: config, data, sidebar."""
    st.set_page_config(page_title=f"{title} · Ask Your Invoices", page_icon=icon,
                       layout=layout)
    ensure_data()
    sl, vocab = load()
    sidebar()
    return sl, vocab


def sidebar():
    with st.sidebar:
        st.header(VENUE)
        st.caption(VENUE_NOTE)
        s = invoice_stats()
        st.metric("Invoices read", int(s["n"]))
        st.caption(f"{int(s['ok'])} of {int(s['n'])} reconcile to their printed subtotal · "
                   f"${s['spend']:,.0f} ex GST · FY26 · {ENGINE}")
        st.divider()
        st.caption("Every figure comes from measures defined in semantic/model.yml. "
                   "Nothing on these pages is written by a model.")


# ------------------------------------------------------------------ formatting
def money(x, cents: bool = False) -> str:
    if x is None:
        return "-"
    return f"${x:,.2f}" if cents else f"${x:,.0f}"


def signed_money(x, cents: bool = False) -> str:
    return ("+" if x >= 0 else "-") + money(abs(x), cents)


def pct(x, places: int = 1) -> str:
    return "-" if x is None else f"{x * 100:.{places}f}%"


def download(df, name: str, label: str = "Download CSV", key: str | None = None):
    """Any table on any page can leave as a CSV. An analyst's first question
    about a dashboard is always 'can I get this in a spreadsheet?'"""
    st.download_button(label, df.to_csv(index=False).encode("utf-8"),
                       file_name=name, mime="text/csv", key=key)
