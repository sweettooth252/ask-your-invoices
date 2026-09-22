"""My tiny planner: English in, a plan out. It never writes SQL."""
from askinv.layer import SemanticLayer, SemanticError

sl = SemanticLayer()
DRINKS = sl.values("drink", "drinks")          # the real drink names in the data
SUPPLIERS = sl.values("supplier")


def tiny_plan(question):
    q = question.lower()

    # 1. Things we know we can't answer
    if "sold" in q or "sales" in q:
        return {"refuse": "There is no sales data - only invoices and recipes."}

    # 2. Which measure?
    if "cost to make" in q or "cost per serve" in q:
        metric = "cost_per_serve"
    elif "pour cost" in q:
        metric = "pour_cost_pct"
    else:
        metric = "spend"

    # 3. Grouped by what?
    dims = []
    if "supplier" in q:
        dims.append("supplier")
    if "each drink" in q or "each cocktail" in q:
        dims.append("drink")

    # 4. Filtered to what? Only real values from the data are ever used.
    filters = []
    for d in DRINKS:
        if d.lower() in q:
            filters.append({"dimension": "drink", "value": d})
    return {"metrics": [metric], "dimensions": dims, "filters": filters}


def answer(question):
    plan = tiny_plan(question)
    print("Q:", question)
    print("Plan:", plan)
    if "refuse" in plan:
        print("Refused:", plan["refuse"])
    else:
        try:
            print(sl.query(plan).to_string(index=False))
        except SemanticError as e:
            print("Refused by the semantic layer:", e)
    print()


answer("How much did we spend with each supplier?")
answer("What does a Negroni cost to make?")
answer("What's the pour cost of each drink?")
answer("Which cocktail sold the most?")
