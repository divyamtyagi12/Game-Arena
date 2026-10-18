"""`rpc()` adapter over plain PostgreSQL (psql subprocess) so the real IplRepo/services run against a real database.

Each call opens its own connection → concurrent awaits are genuinely concurrent transactions (used by the race tests).
Configure with IPL_TEST_PG, e.g.  "-h /var/tmp/ipl_pg -p 54329 -U postgres -d ipltest"  (tests skip when unset/unreachable).
Statements travel via stdin (no argv size limit).
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
from typing import Any

DEFAULT = "-h /var/tmp/ipl_pg -p 54329 -U postgres -d ipltest"


class PgError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def conn_args() -> list[str]:
    return shlex.split(os.environ.get("IPL_TEST_PG", DEFAULT))


def run_sql(sql: str, *, timeout: float = 120) -> str:
    p = subprocess.run(["psql", *conn_args(), "-qAtX", "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=verbose", "-f", "-"],
                       input=sql, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        import re
        m = re.search(r"ERROR:\s+(\w{5}):\s*(.*)", p.stderr)
        raise PgError(m.group(1) if m else "XX000", (m.group(2) if m else p.stderr)[:400])
    return p.stdout.strip()


def available() -> bool:
    try:
        return run_sql("select 1 from ipl_tournaments limit 0; select 1", timeout=10) == "1"
    except Exception:  # noqa: BLE001
        return False


def _q(v: str) -> str:
    return "'" + v.replace("'", "''") + "'"


def literal(v: Any, typ: str) -> str:
    if v is None:
        return "null"
    if typ in ("jsonb", "json"):
        return f"{_q(json.dumps(v))}::jsonb"
    if typ.endswith("[]"):
        base = typ[:-2]
        return "array[" + ",".join(literal(x, base) for x in v) + f"]::{typ}" if v else f"'{{}}'::{typ}"
    if typ == "boolean":
        return "true" if v else "false"
    if typ in ("bigint", "integer", "smallint", "numeric", "double precision"):
        return f"{v}::{typ}"
    return f"{_q(str(v))}::{typ}"


class PgDatabase:
    """Quacks like database.client.Database for the `.rpc` surface IplRepo uses."""

    def __init__(self) -> None:
        self._sig: dict[str, tuple[list[str], list[str], str]] = {}
        self.calls = 0

    def _signature(self, fn: str):
        if fn not in self._sig:
            out = run_sql(f"""select coalesce(array_to_string(proargnames, ','), ''), array_to_string(proargtypes::regtype[], ','),
                              prorettype::regtype from pg_proc where proname = {_q(fn)} and pronamespace = 'public'::regnamespace""")
            if not out:
                raise PgError("42883", f"function {fn} does not exist")
            names, types, ret = out.split("|")
            self._sig[fn] = (names.split(","), types.split(","), ret)
        return self._sig[fn]

    async def rpc(self, fn: str, params: dict[str, Any]) -> Any:
        names, types, ret = self._signature(fn)
        tmap = dict(zip(names, types))
        args = ", ".join(f"{k} => {literal(v, tmap[k])}" for k, v in params.items())
        call = f"public.{fn}({args})"
        sql = f"select {call};" if ret == "void" else f"select coalesce(to_jsonb({call}), 'null'::jsonb);"
        self.calls += 1
        out = await asyncio.to_thread(run_sql, sql)
        if ret == "void" or out == "":
            return None
        return json.loads(out)

    async def exec(self, build, **_kw) -> Any:
        """Supabase-style table access (select/insert/update/delete/upsert + eq/neq/in_/limit/range) over psql."""
        q = build(_Client())
        if isinstance(q, _Query):
            sql = q.sql()
            out = await asyncio.to_thread(run_sql, sql)
            return json.loads(out) if out else []
        raise NotImplementedError("PgDatabase.exec only supports table() queries")


def _lit(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (dict, list)):
        return f"{_q(json.dumps(v))}::jsonb"
    return _q(str(v))


def _ident(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"bad identifier {name!r}")
    return '"' + name + '"'


class _Client:
    def table(self, name: str) -> "_Query":
        return _Query(name)


class _Query:
    def __init__(self, table: str) -> None:
        self.t, self.op, self.cols, self.payload = table, "select", "*", None
        self.where: list[str] = []
        self._limit: int | None = None
        self._offset = 0

    def select(self, cols: str = "*"):
        self.op, self.cols = "select", cols
        return self

    def insert(self, rows):
        self.op, self.payload = "insert", rows
        return self

    def upsert(self, rows, **_kw):
        self.op, self.payload = "upsert", rows
        return self

    def update(self, values: dict):
        self.op, self.payload = "update", values
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, col, v):
        self.where.append(f"{_ident(col)} = {_lit(v)}")
        return self

    def neq(self, col, v):
        self.where.append(f"{_ident(col)} <> {_lit(v)}")
        return self

    def in_(self, col, values):
        vals = list(values)
        self.where.append(f"{_ident(col)} in ({', '.join(_lit(v) for v in vals)})" if vals else "false")
        return self

    def limit(self, n: int):
        self._limit = int(n)
        return self

    def range(self, a: int, b: int):
        self._offset, self._limit = int(a), int(b) - int(a) + 1
        return self

    def sql(self) -> str:
        tbl = _ident(self.t)
        w = (" where " + " and ".join(self.where)) if self.where else ""
        if self.op == "select":
            cols = ", ".join(_ident(c.strip()) for c in self.cols.split(",")) if self.cols.strip() != "*" else "*"
            lim = f" limit {self._limit}" if self._limit is not None else ""
            off = f" offset {self._offset}" if self._offset else ""
            return f"select coalesce(jsonb_agg(to_jsonb(s)), '[]'::jsonb) from (select {cols} from {tbl}{w}{lim}{off}) s;"
        if self.op in ("insert", "upsert"):
            rows = self.payload if isinstance(self.payload, list) else [self.payload]
            if not rows:
                return "select '[]'::jsonb;"
            keys = sorted({k for r in rows for k in r})
            cols = ", ".join(_ident(k) for k in keys)
            vals = ", ".join("(" + ", ".join(_lit(r.get(k)) for k in keys) + ")" for r in rows)
            tail = " on conflict do nothing" if self.op == "upsert" else ""
            return f"insert into {tbl} ({cols}) values {vals}{tail}; select '[]'::jsonb;"
        if self.op == "update":
            sets = ", ".join(f"{_ident(k)} = {_lit(v)}" for k, v in self.payload.items())
            return f"update {tbl} set {sets}{w}; select '[]'::jsonb;"
        return f"delete from {tbl}{w}; select '[]'::jsonb;"
