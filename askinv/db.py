"""
One place that knows which database we are talking to.

DuckDB is the target: one file, fast, no server. If DuckDB is not installed the
same code runs on SQLite, which ships with Python. Every SQL statement in this
project is written to work on both, which is why dates are pre-computed in a
date table instead of using date functions that differ between databases.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "invoices.db"

try:
    import duckdb  # noqa: F401
    ENGINE = "duckdb"
except ImportError:          # pragma: no cover - depends on the machine
    ENGINE = "sqlite"


class _Result:
    def __init__(self, cursor):
        self._cursor = cursor

    def df(self) -> pd.DataFrame:
        rows = self._cursor.fetchall()
        cols = [c[0] for c in self._cursor.description]
        return pd.DataFrame(rows, columns=cols)


class _SqliteConnection:
    """Gives SQLite the same .execute(sql).df() shape that DuckDB has."""

    def __init__(self, path):
        self._con = sqlite3.connect(str(path))

    def execute(self, sql, params=None):
        return _Result(self._con.execute(sql, params or []))

    def write(self, name, df):
        df.to_sql(name, self._con, if_exists="replace", index=False)
        self._con.commit()

    def close(self):
        self._con.close()


class _DuckConnection:
    def __init__(self, path, read_only):
        import duckdb
        self._con = duckdb.connect(str(path), read_only=read_only)

    def execute(self, sql, params=None):
        return self._con.execute(sql, params or [])

    def write(self, name, df):
        self._con.register("_incoming", df)
        self._con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM _incoming")
        self._con.unregister("_incoming")

    def close(self):
        self._con.close()


def connect(path: Path | str = DB_PATH, read_only: bool = True):
    if ENGINE == "duckdb":
        return _DuckConnection(path, read_only=read_only)
    return _SqliteConnection(path)
