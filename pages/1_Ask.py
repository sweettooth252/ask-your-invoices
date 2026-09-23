"""
Ask - the chat.

Every answer shows: the sentence, the table behind it, the invoice lines behind
the table, and exactly what was run. Nothing is hidden, because an analyst's
answer you can't check is an opinion.
"""

import json
import os

import streamlit as st

from askinv.answer import ask
from askinv.ui import download, page

sl, vocab = page("Ask", icon="💬")

st.title("Ask your invoices")
st.write("291 supplier invoices from five suppliers, turned into a semantic layer. "
         "Ask about spend, prices, or what each drink costs to make.")

has_key = bool(os.environ.get("GEMINI_API_KEY"))
c1, c2 = st.columns([1, 2])
planner = c1.radio("Planner", ["rules", "gemini"] if has_key else ["rules"],
                   horizontal=True,
                   help="Rules is free and offline. Gemini needs GEMINI_API_KEY.")
rephrase = c2.toggle("Let the model reword answers", value=False, disabled=not has_key,
                     help="Any rewrite containing a number not in the result is thrown away.")

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


def render(a, n):
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

        download(t, "answer.csv", "Download this table", key=f"dl_answer_{n}")

        c = a.citations
        if c is not None and len(c):
            n_inv = c["invoice_no"].nunique() if "invoice_no" in c else 0
            label = (f"Receipts - {len(c)} invoice lines from {n_inv} invoices" if n_inv
                     else f"Receipts - {len(c)} ingredient costs")
            with st.expander(label):
                st.dataframe(c, hide_index=True, use_container_width=True)
                download(c, "receipts.csv", "Download the receipts", key=f"dl_cite_{n}")

        with st.expander("How I worked it out"):
            st.code(json.dumps(a.plan, indent=2), language="json")
            if a.sql:
                st.code(a.sql, language="sql")


for n, a in enumerate(st.session_state.history):
    render(a, n)
