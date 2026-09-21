"""
The semantic layer: metric and dimension names in, SQL and a table out.

Four promises, each enforced in code rather than hoped for:

  1. Only names defined in semantic/model.yml can be asked for.
  2. Metrics from different datasets are never silently combined.
  3. A metric marked one_per: [unit] or [drink] is never computed across
     mixed units or mixed drinks - the result is checked row by row.
  4. Any result row can be traced back to the records behind it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd
import yaml

from askinv.db import DB_PATH, ROOT, connect

MODEL_PATH = ROOT / "semantic" / "model.yml"
OPS = {"=", "!=", ">", ">=", "<", "<=", "in"}


class SemanticError(Exception):
    """Raised when a question cannot be answered honestly."""


@dataclass
class Query:
    metrics: list
    dimensions: list = field(default_factory=list)
    filters: list = field(default_factory=list)      # [{"dimension","op","value"}]
    order_by: str | None = None
    descending: bool = True
    limit: int | None = None

    @classmethod
    def from_dict(cls, d):
        if isinstance(d, Query):
            return d
        return cls(metrics=list(d.get("metrics") or []),
                   dimensions=list(d.get("dimensions") or []),
                   filters=list(d.get("filters") or []),
                   order_by=d.get("order_by"),
                   descending=d.get("descending", True),
                   limit=d.get("limit"))

    def to_dict(self):
        out = {"metrics": self.metrics}
        if self.dimensions:
            out["dimensions"] = self.dimensions
        if self.filters:
            out["filters"] = self.filters
        if self.order_by:
            out["order_by"] = self.order_by
        if self.limit:
            out["limit"] = self.limit
        return out


def _literal(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


class SemanticLayer:
    def __init__(self, model_path=MODEL_PATH, db_path=DB_PATH):
        with open(model_path, encoding="utf-8") as fh:
            self.model = yaml.safe_load(fh)
        self.datasets = self.model["datasets"]
        self.metrics = self.model["metrics"]
        self.dimensions = self.model["dimensions"]
        self.db_path = db_path

    # ------------------------------------------------------------ catalogue
    def catalogue(self) -> dict:
        """What can be asked. This, and only this, is what the AI sees."""
        return {
            "metrics": {k: {"label": v.get("label", k),
                            "dataset": v["dataset"],
                            "description": " ".join(str(v.get("description", "")).split()),
                            "only_meaningful_per": v.get("one_per", [])}
                        for k, v in self.metrics.items()},
            "dimensions": {k: {"description": v.get("description", ""),
                               "datasets": sorted(v["sql"])}
                           for k, v in self.dimensions.items()},
        }

    def dataset_of(self, metrics):
        found = {self.metrics[m]["dataset"] for m in metrics}
        if len(found) > 1:
            groups = {}
            for m in metrics:
                groups.setdefault(self.metrics[m]["dataset"], []).append(m)
            detail = "; ".join(f"{', '.join(v)} come from {k}" for k, v in groups.items())
            raise SemanticError(
                f"Those can't be answered in one table: {detail}. Ask for them separately.")
        return found.pop()

    def values(self, dimension, dataset=None):
        """Distinct values of a dimension - used to check filters are real."""
        self._check_dimension(dimension)
        sqls = self.dimensions[dimension]["sql"]
        dataset = dataset or next(iter(sqls))
        ds = self.datasets[dataset]
        sql = (f"SELECT DISTINCT {sqls[dataset]} AS v FROM {ds['table']} "
               + " ".join(ds.get("joins", [])))
        if ds.get("grain_column") and self.dimensions[dimension].get("grain"):
            sql += f" WHERE {ds['grain_column']} = {_literal(self.dimensions[dimension]['grain'])}"
        sql += " ORDER BY 1"
        return [v for v in self._run(sql)["v"].tolist() if v is not None]

    # ------------------------------------------------------------ checks
    def _check_dimension(self, d):
        if d not in self.dimensions:
            raise SemanticError(f"'{d}' is not something I can group by. Available: "
                                f"{', '.join(sorted(self.dimensions))}.")

    def _check_metric(self, m):
        if m not in self.metrics:
            raise SemanticError(f"'{m}' is not a defined measure. Available: "
                                f"{', '.join(sorted(self.metrics))}.")

    def _dim_sql(self, d, dataset):
        self._check_dimension(d)
        sqls = self.dimensions[d]["sql"]
        if dataset not in sqls:
            where = ", ".join(sorted(sqls))
            raise SemanticError(
                f"'{d}' doesn't apply to {dataset} - it only exists for {where}.")
        return sqls[dataset]

    def _expand(self, name, seen=()):
        if name in seen:
            raise SemanticError(f"'{name}' is defined in terms of itself.")
        self._check_metric(name)
        spec = self.metrics[name]
        if spec["type"] == "simple":
            return f"({spec['sql']})"
        out = spec["sql"]
        for ref in sorted(self.metrics, key=len, reverse=True):
            if ref != name and re.search(rf"\b{ref}\b", out):
                out = re.sub(rf"\b{ref}\b", self._expand(ref, seen + (name,)), out)
        return f"({out})"

    def _where(self, filters, dataset):
        out = []
        for f in filters:
            col = self._dim_sql(f["dimension"], dataset)
            op = str(f.get("op", "=")).lower()
            if op not in OPS:
                raise SemanticError(f"Operator '{op}' is not supported.")
            if op == "in":
                vals = f["value"] if isinstance(f["value"], (list, tuple)) else [f["value"]]
                out.append(f"{col} IN ({', '.join(_literal(v) for v in vals)})")
            else:
                out.append(f"{col} {op} {_literal(f['value'])}")
        return out

    def _grain(self, q, dataset):
        """Drink costs are stored per month and per quarter. Pick the grain the
        question implies, and default to the latest quarter otherwise."""
        ds = self.datasets[dataset]
        if not ds.get("grain_column"):
            return [], None
        used = q.dimensions + [f["dimension"] for f in q.filters]
        grains = {self.dimensions[d].get("grain") for d in used} - {None}
        if len(grains) > 1:
            raise SemanticError("Pick either months or quarters for drink costs, not both.")
        if grains:
            return [f"{ds['grain_column']} = {_literal(grains.pop())}"], None
        latest = self.values("quarter", dataset)[-1]
        return ([f"{ds['grain_column']} = 'quarter'",
                 f"{self.dimensions['quarter']['sql'][dataset]} = {_literal(latest)}"],
                f"latest quarter, {latest}")

    # ------------------------------------------------------------ compile
    def compile(self, q) -> tuple[str, str, str | None]:
        q = Query.from_dict(q)
        if not q.metrics:
            raise SemanticError("Ask for at least one measure.")
        for m in q.metrics:
            self._check_metric(m)
        dataset = self.dataset_of(q.metrics)
        ds = self.datasets[dataset]

        select = [f"{self._dim_sql(d, dataset)} AS {d}" for d in q.dimensions]
        select += [f"{self._expand(m)} AS {m}" for m in q.metrics]

        guards = sorted({g for m in q.metrics for g in self.metrics[m].get("one_per", [])})
        for g in guards:
            col = ds["guards"][g]
            select += [f"COUNT(DISTINCT {col}) AS _n_{g}", f"MIN({col}) AS _v_{g}"]

        where = self._where(q.filters, dataset)
        grain_where, note = self._grain(q, dataset)
        where += grain_where

        sql = "SELECT\n  " + ",\n  ".join(select) + f"\nFROM {ds['table']}"
        if ds.get("joins"):
            sql += "\n" + "\n".join(ds["joins"])
        if where:
            sql += "\nWHERE " + "\n  AND ".join(where)
        if q.dimensions:
            sql += "\nGROUP BY " + ", ".join(str(i + 1) for i in range(len(q.dimensions)))
        if q.order_by:
            if q.order_by not in q.metrics + q.dimensions:
                raise SemanticError(f"Can't sort by '{q.order_by}': it isn't in the answer.")
            sql += f"\nORDER BY {q.order_by} {'DESC' if q.descending else 'ASC'}"
        elif q.dimensions:
            sql += "\nORDER BY " + ", ".join(str(i + 1) for i in range(len(q.dimensions)))
        if q.limit:
            sql += f"\nLIMIT {int(q.limit)}"
        return sql, dataset, note

    # ------------------------------------------------------------ run
    def _run(self, sql):
        con = connect(self.db_path)
        try:
            return con.execute(sql).df()
        finally:
            con.close()

    def query(self, q) -> pd.DataFrame:
        q = Query.from_dict(q)
        sql, dataset, note = self.compile(q)
        df = self._run(sql)

        for col in [c for c in df.columns if c.startswith("_n_")]:
            g = col[3:]
            bad = df[df[col] > 1]
            if len(bad):
                where = "; ".join(" / ".join(str(r[d]) for d in q.dimensions) or "everything"
                                  for _, r in bad.head(3).iterrows())
                ms = [m for m in q.metrics if g in self.metrics[m].get("one_per", [])]
                what = {"unit": "litres, kilos and units", "drink": "several different drinks"}[g]
                fix = {"unit": "Split it by 'unit' or 'product', or filter to one unit.",
                       "drink": "Split it by 'drink', or ask about one drink."}[g]
                raise SemanticError(f"{', '.join(ms)} would add up {what} in {len(bad)} "
                                    f"row(s) ({where}). {fix}")
            df = df.drop(columns=[col])
            if g in q.dimensions or (g == "drink" and "drink" in df.columns):
                df = df.drop(columns=[f"_v_{g}"])
            else:
                df = df.rename(columns={f"_v_{g}": g})

        df.attrs.update(sql=sql, query=q, dataset=dataset, note=note)
        return df

    # ------------------------------------------------------------ lineage
    def lineage(self, q, row: dict | None = None, limit: int = 1000) -> pd.DataFrame:
        """The records behind an answer - invoice lines for purchases,
        ingredient costs for drinks. Pass a result row to narrow to that row."""
        q = Query.from_dict(q)
        dataset = self.dataset_of(q.metrics)
        ds = self.datasets[dataset]
        table = ds.get("lineage_table", ds["table"])

        filters = list(q.filters)
        for d in q.dimensions:
            if row is not None and d in row:
                filters.append({"dimension": d, "op": "=", "value": row[d]})
        where = self._where(filters, dataset)
        _, note = self._grain(q, dataset)
        if ds.get("grain_column"):
            where += self._grain(q, dataset)[0]
        cols = [f"{c} AS {c.split('.')[-1]}" for c in ds["lineage"]]
        sql = "SELECT\n  " + ",\n  ".join(cols) + f"\nFROM {table}"
        if ds.get("joins"):
            sql += "\n" + "\n".join(ds["joins"])
        if table != ds["table"]:
            # Drink filters are written against fct_drink_cost; the ingredient
            # table has the same drink / grain / period columns.
            where = [w.replace(ds["table"] + ".", table + ".") for w in where]
        if where:
            sql += "\nWHERE " + "\n  AND ".join(where)
        sql += f"\nLIMIT {int(limit)}"
        out = self._run(sql)
        out.attrs["note"] = note
        return out


def fmt(value, kind, unit=None):
    if value is None or (isinstance(value, float) and value != value):
        return "-"
    if kind == "currency":
        return f"${value:,.0f}" if abs(value) >= 100 else f"${value:,.2f}"
    if kind == "currency_cents":
        return f"${value:,.2f}"
    if kind == "unit_price":
        u = f"/{unit}" if unit else ""
        return f"${value:,.4f}{u}" if abs(value) < 1 else f"${value:,.2f}{u}"
    if kind == "quantity":
        return f"{value:,.1f} {unit or ''}".strip()
    if kind == "percent":
        return f"{value:.1%}"
    return f"{value:,.0f}" if isinstance(value, float) else f"{value:,}"
