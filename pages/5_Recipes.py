"""
Recipes - the spec book, what each drink costs, and what if you changed it.

The specs live in semantic/recipes.yml, in git, readable by anyone. This screen
shows them, costs them at the prices actually paid, and lets you try a change
before you commit to it. Editing here never writes to the file by itself: it
hands you the YAML to paste, because a menu change should be a decision someone
made, not a click.
"""

import pandas as pd
import streamlit as st
import yaml

from askinv import insights as ins
from askinv.menu import RecipeError, load_recipes
from askinv.ui import download, money, page, pct

sl, vocab = page("Recipes", icon="📖")

recipes = load_recipes()
ingredients = recipes["ingredients"]
drinks = recipes["drinks"]

st.title("The spec book")

grain = st.radio("Price the recipes at", ["quarter", "month"], horizontal=True,
                 help="Ingredient prices are the volume-weighted prices actually paid "
                      "in the period you pick.")
ps = ins.periods(sl, grain)
period = st.selectbox("Period", ps[::-1], index=0)
book = ins.price_book(sl, period=period, grain=grain, recipes=recipes)

tab_menu, tab_drink, tab_whatif, tab_new = st.tabs(
    ["Menu board", "One drink", "What if", "Add a drink"])

# ------------------------------------------------------------------ menu board
with tab_menu:
    board = ins.menu_board(sl, period=period, grain=grain)
    c1, c2, c3 = st.columns(3)
    c1.metric("Drinks costed", len(board))
    c2.metric("Dearest to pour", board.iloc[0]["drink"],
              pct(board.iloc[0]["pour_cost_pct"]), delta_color="off")
    c3.metric("Average pour cost", pct(board["pour_cost_pct"].mean()))

    pretty = board.copy()
    for col in ("cost_per_serve", "menu_price", "margin_per_serve"):
        pretty[col] = pretty[col].map(lambda v: money(v, cents=True))
    pretty["pour_cost_pct"] = board["pour_cost_pct"].map(pct)
    st.dataframe(pretty, hide_index=True, use_container_width=True)
    st.caption("`menu_price` excludes GST, because ingredient costs do. Pour cost "
               "assumes perfect pours: no spillage, wastage or comps.")
    download(board, f"menu_board_{period}.csv", key="dl_board")

    st.subheader("Every spec")
    library = ins.recipe_library(recipes)
    st.dataframe(library, hide_index=True, use_container_width=True)
    st.caption("`serves_per_batch` above 1 means the amounts are written for a batch: "
               "150g of filter beans brews ten cups, so cost per serve is the batch "
               "cost divided by ten.")
    download(library, "recipe_library.csv", key="dl_library")

# ------------------------------------------------------------------- one drink
with tab_drink:
    drink = st.selectbox("Drink", sorted(drinks), key="one_drink")
    d = drinks[drink]
    makes = float((d.get("batch") or {}).get("makes", 1) or 1)

    cost, rows = ins.cost_of_spec(d["spec"], book, makes=makes, recipes=recipes,
                                  label=drink)
    price_ex = d["price"] / 1.1

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Costs to make", money(cost, cents=True))
    k2.metric("Menu price ex GST", money(price_ex, cents=True),
              f"{money(d['price'], cents=True)} inc GST", delta_color="off")
    k3.metric("Margin a serve", money(price_ex - cost, cents=True))
    k4.metric("Pour cost", pct(ins.pour_cost(cost, d["price"])))

    c1, c2 = st.columns([1, 1])
    with c1:
        st.bar_chart(rows.set_index("ingredient")["cost"])
    with c2:
        show = rows.copy()
        show["cost"] = rows["cost"].map(lambda v: f"${v:,.4f}")
        show["price_per_unit"] = rows["price_per_unit"].map(lambda v: f"${v:,.4f}")
        show["used_qty"] = rows["used_qty"].map(lambda v: f"{v:,.4f}")
        st.dataframe(show, hide_index=True, use_container_width=True)

    if rows["carried_forward"].any():
        st.info("Some ingredients weren't delivered in this period, so the last price "
                "paid was carried forward. The rows are marked.")

    st.subheader("What it has cost over time")
    history = ins.drink_cost_history(sl, drink, grain="month")
    st.line_chart(history.set_index("month")["cost_per_serve"])
    first, last = history["cost_per_serve"].iloc[0], history["cost_per_serve"].iloc[-1]
    st.caption(f"{money(first, cents=True)} in {history['month'].iloc[0]} → "
               f"{money(last, cents=True)} in {history['month'].iloc[-1]} "
               f"({pct(last / first - 1)}). The spec hasn't changed, so all of that "
               "is ingredient prices.")
    download(history, f"cost_history_{drink}.csv".replace(" ", "_"), key="dl_history")

    st.subheader("What would it need to sell for?")
    target = st.slider("Target pour cost", 0.10, 0.35, 0.20, 0.01, format="%.0f%%",
                       key="target_one")
    needed = ins.price_for_target_pour_cost(cost, target)
    st.metric(f"Menu price for a {pct(target, 0)} pour cost", money(needed, cents=True),
              f"{money(needed - d['price'], cents=True)} against today's price",
              delta_color="off")
    st.caption("Including GST, so it's comparable with the price on the menu.")

# --------------------------------------------------------------------- what if
with tab_whatif:
    st.write("Change a spec and see the cost move, at this period's prices. Nothing "
             "is saved until you copy the YAML out.")
    drink = st.selectbox("Drink", sorted(drinks), key="whatif_drink")
    d = drinks[drink]
    base_makes = float((d.get("batch") or {}).get("makes", 1) or 1)
    base_cost, _rows = ins.cost_of_spec(d["spec"], book, makes=base_makes,
                                        recipes=recipes, label=drink)

    c1, c2 = st.columns(2)
    new_price = c1.number_input("Menu price inc GST", value=float(d["price"]),
                                step=0.50, min_value=0.0)
    new_makes = c2.number_input("Serves per batch", value=float(base_makes), step=1.0,
                                min_value=1.0,
                                help="1 for a drink made one at a time.")

    editable = pd.DataFrame({"ingredient": list(d["spec"]),
                             "amount": [str(v) for v in d["spec"].values()]})
    edited = st.data_editor(
        editable, num_rows="dynamic", use_container_width=True, hide_index=True,
        key=f"editor_{drink}",
        column_config={"ingredient": st.column_config.SelectboxColumn(
            "ingredient", options=sorted(ingredients), required=True)})

    spec = {r["ingredient"]: r["amount"] for _, r in edited.iterrows()
            if r["ingredient"] and r["amount"]}

    try:
        new_cost, new_rows = ins.cost_of_spec(spec, book, makes=new_makes,
                                              recipes=recipes, label=drink)
        costed = True
    except (RecipeError, KeyError) as e:
        st.error(f"Can't cost that yet: {e}")
        costed = False

    if costed:
        k1, k2, k3 = st.columns(3)
        k1.metric("Costs to make", money(new_cost, cents=True),
                  f"{money(new_cost - base_cost, cents=True)} vs the saved spec",
                  delta_color="inverse")
        k2.metric("Pour cost", pct(ins.pour_cost(new_cost, new_price)),
                  f"{(ins.pour_cost(new_cost, new_price) - ins.pour_cost(base_cost, d['price'])) * 100:+.1f} pts",
                  delta_color="inverse")
        k3.metric("Margin a serve", money(new_price / 1.1 - new_cost, cents=True))

        st.dataframe(new_rows.round(4), hide_index=True, use_container_width=True)

        snippet = {drink: {"type": d["type"], "price": round(float(new_price), 2),
                           **({"batch": {"makes": int(new_makes)}} if new_makes > 1 else {}),
                           "spec": spec}}
        st.subheader("To keep this change")
        st.code(yaml.safe_dump(snippet, sort_keys=False, default_flow_style=False),
                language="yaml")
        st.caption("Paste that over the drink in `semantic/recipes.yml`, then run "
                   "`python -m askinv.ingest`. Going through the file means every menu "
                   "change is a commit someone can review, not a click nobody saw.")

# ----------------------------------------------------------------- add a drink
with tab_new:
    st.write("Cost a drink that isn't on the menu yet. It can only use ingredients "
             "the venue actually buys - that's what makes the number real.")

    with st.form("new_drink"):
        c1, c2, c3 = st.columns(3)
        name = c1.text_input("Name", value="Espresso Tonic")
        kind = c2.selectbox("Type", ["Cocktail", "Coffee"])
        price = c3.number_input("Menu price inc GST", value=7.50, step=0.50, min_value=0.0)
        makes_new = st.number_input("Serves per batch", value=1.0, step=1.0, min_value=1.0)
        start = pd.DataFrame({"ingredient": ["espresso_beans", "tonic"],
                              "amount": ["18g", "150ml"]})
        new_spec = st.data_editor(
            start, num_rows="dynamic", use_container_width=True, hide_index=True,
            key="new_editor",
            column_config={"ingredient": st.column_config.SelectboxColumn(
                "ingredient", options=sorted(ingredients), required=True)})
        submitted = st.form_submit_button("Cost it", type="primary")

    if submitted:
        spec = {r["ingredient"]: r["amount"] for _, r in new_spec.iterrows()
                if r["ingredient"] and r["amount"]}
        try:
            cost, rows = ins.cost_of_spec(spec, book, makes=makes_new, recipes=recipes,
                                          label=name or "drink")
        except (RecipeError, KeyError) as e:
            st.error(f"Can't cost that: {e}")
        else:
            k1, k2, k3 = st.columns(3)
            k1.metric("Costs to make", money(cost, cents=True))
            k2.metric("Pour cost", pct(ins.pour_cost(cost, price)))
            k3.metric("Margin a serve", money(price / 1.1 - cost, cents=True))
            st.dataframe(rows.round(4), hide_index=True, use_container_width=True)

            snippet = {name: {"type": kind, "price": round(float(price), 2),
                              **({"batch": {"makes": int(makes_new)}} if makes_new > 1
                                 else {}),
                              "spec": spec}}
            st.code(yaml.safe_dump(snippet, sort_keys=False, default_flow_style=False),
                    language="yaml")
            st.caption("Add that under `drinks:` in `semantic/recipes.yml`, indented two "
                       "spaces, then run `python -m askinv.ingest`. The new drink then "
                       "appears everywhere: the dashboard, the chat, the alerts. "
                       "Nothing else needs changing, which is the point of a semantic "
                       "layer.")

    st.divider()
    st.subheader("Ingredients you can use")
    rows = [{"ingredient": name,
             "products": ", ".join(str(c) for c in spec["products"]),
             "yield": spec.get("yield", ""),
             "waste allowance": spec.get("waste", "")}
            for name, spec in ingredients.items()]
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    st.caption("An ingredient can come from several products: oat milk is bought from "
               "two suppliers, and the price used is what was actually paid across "
               "both. `yield` converts a bought unit into a poured one - a kilo of "
               "limes gives about 450ml of juice.")
