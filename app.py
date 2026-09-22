"""
Ask Your Invoices - a cafe-bar's supplier invoices, as a chat.

    streamlit run app.py

Every answer shows: the sentence, the table behind it, the invoice lines behind
the table, and exactly what was run. Nothing is hidden, because an analyst's
answer you can't check is an opinion.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

from askinv.answer import ask
from askinv.db import DB_PATH, ENGINE, connect
from askinv.layer import SemanticLayer
from askinv.planner import Vocabulary

st.set_page_config(page_title="Ask Your Invoices", page_icon="🍸", layout="wide")


# On a fresh server (e.g. Streamlit Community Cloud) the data isn't in git, so
# build it once on first start. Takes about 10 seconds.
if not Path(DB_PATH).exists():
    with st.spinner("First start: generating the invoices and building the warehouse..."):
        here = Path(__file__).resolve().parent
        subprocess.run([sys.executable, "make_invoices.py"], cwd=here, check=True)
        subprocess.run([sys.executable, "-m", "askinv.ingest"], cwd=here, check=True)


@st.cache_resource
def load():
    sl = SemanticLayer()
    return sl, Vocabulary(sl)


sl, vocab = load()

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("Night & Day")
    st.caption("Specialty coffee by day, cocktails by night. Collingwood. (Fictional.)")
    con = connect()
    stats = con.execute("SELECT COUNT(*) AS n, SUM(reconciles) AS ok, "
                        "SUM(subtotal_ex_gst) AS spend FROM fct_invoice").df().iloc[0]
    con.close()
    st.metric("Invoices read", int(stats["n"]))
    st.caption(f"{int(stats['ok'])} of {int(stats['n'])} reconcile to their printed subtotal · "
               f"${stats['spend']:,.0f} ex GST · FY26 · {ENGINE}")

    has_key = bool(os.environ.get("GEMINI_API_KEY"))
    planner = st.radio("Planner", ["rules", "gemini"] if has_key else ["rules"],
                       help="Rules is free and offline. Gemini needs GEMINI_API_KEY.")
    rephrase = st.toggle("Let the model reword answers", value=False, disabled=not has_key,
                         help="Any rewrite containing a number not in the result is thrown away.")
    st.divider()
    st.caption("The assistant can only use measures defined in semantic/model.yml. "
               "It never writes SQL and never supplies a number.")

# ------------------------------------------------------------------ header
st.title("Ask your invoices")
st.write("291 supplier invoices from five suppliers, turned into a semantic layer. "
         "Ask about spend, prices, or what each drink costs to make.")

SUGGESTIONS = [
    "What does each drink cost to make?",
    "Why does the espresso martini cost more to make this quarter than 2025-Q3?",
    "Pour cost for cocktails vs coffee by quarter",
    "Why did we spend more at Ember this quarter than last quarter?",
    "What are we paying per kg for limes each month?",
    "What was the average price per unit by category?",
    "Which cocktail sold the most?",
]

if "history" not in st.session_state:
    st.session_state.history = []

cols = st.columns(4)
for i, s in enumerate(SUGGESTIONS[:4]):
    if cols[i].button(s, use_container_width=True):
        st.session_state.pending = s
cols = st.columns(3)
for i, s in enumerate(SUGGESTIONS[4:]):
    if cols[i].button(s, use_container_width=True):
        st.session_state.pending = s

question = st.chat_input("Ask about spend, prices or drink costs")
question = question or st.session_state.pop("pending", None)
if question:
    st.session_state.history.append(ask(question, sl, planner, vocab, rephrase=rephrase))


# ------------------------------------------------------------------ render
def render(a):
    with st.chat_message("user"):
        st.write(a.question)
    with st.chat_message("assistant", avatar="🍸"):
        if a.refused:
            st.warning(a.text)
            return
        st.write(a.text)
        if a.phrasing != "template":
            st.caption(f"Wording: {a.phrasing}")

        t = a.table
        intent = a.plan["intent"]
        if intent == "explain_spend":
            steps = t.set_index("step")["amount"]
            st.bar_chart(steps.iloc[1:-1])
            st.caption(f"Bridge reconciles to the cent: {t.attrs.get('reconciles')}")
            with st.expander("By product"):
                st.dataframe(t.attrs["by_product"].round(2), hide_index=True,
                             use_container_width=True)
        elif intent == "explain_drink":
            st.bar_chart(t.set_index("ingredient")["change"])
            st.dataframe(t.round(4), hide_index=True, use_container_width=True)
        else:
            q = a.plan["query"]
            dims = q.get("dimensions", [])
            metric = q["metrics"][0]
            if len(dims) == 1 and dims[0] in ("month", "quarter", "week") and len(t) > 1:
                st.line_chart(t.set_index(dims[0])[metric])
            elif len(dims) == 2 and dims[1] in ("month", "quarter") and len(t) > 1:
                st.line_chart(t.pivot(index=dims[1], columns=dims[0], values=metric))
            elif len(dims) == 1 and len(t) > 1:
                st.bar_chart(t.set_index(dims[0])[metric])
            st.dataframe(t, hide_index=True, use_container_width=True)

        c = a.citations
        if c is not None and len(c):
            n_inv = c["invoice_no"].nunique() if "invoice_no" in c else 0
            label = (f"Receipts - {len(c)} invoice lines from {n_inv} invoices" if n_inv
                     else f"Receipts - {len(c)} ingredient costs")
            with st.expander(label):
                st.dataframe(c, hide_index=True, use_container_width=True)

        with st.expander("How I worked it out"):
            st.code(json.dumps(a.plan, indent=2), language="json")
            if a.sql:
                st.code(a.sql, language="sql")


for a in st.session_state.history:
    render(a)
