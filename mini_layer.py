"""My mini semantic layer. Names in, SQL out."""
import yaml
from askinv.db import connect

model = yaml.safe_load(open("semantic/mini_model.yml"))
ds = model["datasets"]["purchases"]


def compile_sql(metrics, dimensions=(), filters=None):
    # 1. Refuse anything not defined in the model
    for m in metrics:
        if m not in model["metrics"]:
            raise ValueError(f"'{m}' is not a defined measure")
    for d in dimensions:
        if d not in model["dimensions"]:
            raise ValueError(f"'{d}' is not a defined dimension")

    # 2. SELECT the dimensions, then the measures
    cols = [f"{model['dimensions'][d]} AS {d}" for d in dimensions]
    cols += [f"{model['metrics'][m]['sql']} AS {m}" for m in metrics]

    # 3. The guard: count units in every row, check after running
    if any(model["metrics"][m].get("one_per") == "unit" for m in metrics):
        cols.append("COUNT(DISTINCT fct_invoice_line.base_unit) AS units_in_row")

    sql = "SELECT " + ", ".join(cols) + f" FROM {ds['table']} " + " ".join(ds["joins"])
    if filters:
        sql += " WHERE " + " AND ".join(
            f"{model['dimensions'][d]} = '{v}'" for d, v in filters.items())
    if dimensions:
        sql += " GROUP BY " + ", ".join(str(i + 1) for i in range(len(dimensions)))
        sql += " ORDER BY 1"
    return sql


def query(metrics, dimensions=(), filters=None):
    sql = compile_sql(metrics, dimensions, filters)
    con = connect()
    df = con.execute(sql).df()
    con.close()
    if "units_in_row" in df.columns:
        if (df["units_in_row"] > 1).any():
            raise ValueError("That would add litres to kilos. Split by 'unit' or 'product'.")
        df = df.drop(columns="units_in_row")
    return df


if __name__ == "__main__":
    print(compile_sql(["spend"], ["supplier"]))
    print()
    print(query(["spend"], ["supplier"]))
    print()
    print(query(["price_per_unit"], ["product"], {"category": "Spirits"}))
    print()
    try:
        query(["price_per_unit"], ["category"])
    except ValueError as e:
        print("Refused:", e)
