import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ident(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value or ""):
        raise ValueError(f"Invalid SQL identifier: {value!r}")
    return f'"{value}"'


@dataclass
class AdapterResponse:
    data: Any = None
    count: Optional[int] = None


class SupabaseDatabaseAdapter:
    def __init__(self, client: Any):
        self.client = client

    def table(self, name: str) -> Any:
        return self.client.table(name)


class PostgresQuery:
    def __init__(self, adapter: "PostgresDatabaseAdapter", table: str):
        self.adapter = adapter
        self.table_name = table
        self.operation = "select"
        self.columns = "*"
        self.payload: Any = None
        self.filters: List[Tuple[str, str, Any]] = []
        self.or_filter_groups: List[List[Tuple[str, str, Any]]] = []
        self.ordering: List[Tuple[str, bool]] = []
        self.limit_value: Optional[int] = None
        self.single_value = False
        self.maybe_single_value = False
        self.count_mode: Optional[str] = None
        self.on_conflict: Optional[str] = None

    def select(self, columns: str = "*", count: Optional[str] = None) -> "PostgresQuery":
        self.operation = "select"
        self.columns = columns or "*"
        self.count_mode = count
        return self

    def insert(self, payload: Any) -> "PostgresQuery":
        self.operation = "insert"
        self.payload = payload
        return self

    def update(self, payload: Dict[str, Any]) -> "PostgresQuery":
        self.operation = "update"
        self.payload = payload
        return self

    def upsert(self, payload: Any, on_conflict: Optional[str] = None) -> "PostgresQuery":
        self.operation = "upsert"
        self.payload = payload
        self.on_conflict = on_conflict
        return self

    def delete(self) -> "PostgresQuery":
        self.operation = "delete"
        return self

    def eq(self, column: str, value: Any) -> "PostgresQuery":
        self.filters.append((column, "=", value))
        return self

    def neq(self, column: str, value: Any) -> "PostgresQuery":
        self.filters.append((column, "!=", value))
        return self

    def gt(self, column: str, value: Any) -> "PostgresQuery":
        self.filters.append((column, ">", value))
        return self

    def gte(self, column: str, value: Any) -> "PostgresQuery":
        self.filters.append((column, ">=", value))
        return self

    def lt(self, column: str, value: Any) -> "PostgresQuery":
        self.filters.append((column, "<", value))
        return self

    def lte(self, column: str, value: Any) -> "PostgresQuery":
        self.filters.append((column, "<=", value))
        return self

    def in_(self, column: str, values: Sequence[Any]) -> "PostgresQuery":
        self.filters.append((column, "in", list(values)))
        return self

    def is_(self, column: str, value: Any) -> "PostgresQuery":
        self.filters.append((column, "is", value))
        return self

    def order(self, column: str, desc: bool = False) -> "PostgresQuery":
        self.ordering.append((column, bool(desc)))
        return self

    def limit(self, value: int) -> "PostgresQuery":
        self.limit_value = int(value)
        return self

    def single(self) -> "PostgresQuery":
        self.single_value = True
        self.limit_value = 1
        return self

    def maybe_single(self) -> "PostgresQuery":
        self.maybe_single_value = True
        self.limit_value = 1
        return self

    def or_(self, expression: str) -> "PostgresQuery":
        group: List[Tuple[str, str, Any]] = []
        for clause in _split_postgrest_or(expression):
            parts = clause.split(".", 2)
            if len(parts) != 3:
                raise ValueError(f"Unsupported PostgREST or_ clause: {clause}")
            column, operator, raw_value = parts
            if operator == "eq":
                value: Any = _coerce_postgrest_value(raw_value)
                group.append((column, "=", value))
            elif operator == "is":
                value = _coerce_postgrest_value(raw_value)
                group.append((column, "is", value))
            elif operator == "in" and raw_value.startswith("(") and raw_value.endswith(")"):
                values = [_coerce_postgrest_value(item) for item in raw_value[1:-1].split(",") if item]
                group.append((column, "in", values))
            else:
                raise ValueError(f"Unsupported PostgREST or_ operator: {operator}")
        if group:
            self.or_filter_groups.append(group)
        return self

    def _where(self) -> Tuple[str, List[Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        for column, op, value in self.filters:
            name = _ident(column)
            if op == "in":
                if not value:
                    clauses.append("FALSE")
                else:
                    clauses.append(f"{name} = ANY(%s)")
                    params.append(value)
            elif op == "is":
                if value is None or str(value).lower() == "null":
                    clauses.append(f"{name} IS NULL")
                elif str(value).lower() == "true":
                    clauses.append(f"{name} IS TRUE")
                elif str(value).lower() == "false":
                    clauses.append(f"{name} IS FALSE")
                else:
                    clauses.append(f"{name} IS %s")
                    params.append(value)
            else:
                clauses.append(f"{name} {op} %s")
                params.append(value)
        for group in self.or_filter_groups:
            or_clauses: List[str] = []
            for column, op, value in group:
                name = _ident(column)
                if op == "in":
                    or_clauses.append(f"{name} = ANY(%s)")
                    params.append(value)
                elif op == "is" and value is None:
                    or_clauses.append(f"{name} IS NULL")
                elif op == "is" and value is True:
                    or_clauses.append(f"{name} IS TRUE")
                elif op == "is" and value is False:
                    or_clauses.append(f"{name} IS FALSE")
                else:
                    or_clauses.append(f"{name} {op} %s")
                    params.append(value)
            clauses.append("(" + " OR ".join(or_clauses) + ")")
        return (" WHERE " + " AND ".join(clauses), params) if clauses else ("", params)

    def _returning(self) -> str:
        return " RETURNING *"

    def execute(self) -> AdapterResponse:
        if self.operation == "select":
            return self._execute_select()
        if self.operation in {"insert", "upsert"}:
            return self._execute_insert()
        if self.operation == "update":
            return self._execute_update()
        if self.operation == "delete":
            return self._execute_delete()
        raise ValueError(f"Unsupported operation: {self.operation}")

    def _execute_select(self) -> AdapterResponse:
        columns = "*" if self.columns == "*" else ", ".join(_ident(item.strip()) for item in self.columns.split(","))
        where, params = self._where()
        order = ""
        if self.ordering:
            order = " ORDER BY " + ", ".join(f"{_ident(column)} {'DESC' if desc else 'ASC'}" for column, desc in self.ordering)
        limit = f" LIMIT {self.limit_value}" if self.limit_value is not None else ""
        rows = self.adapter.execute(f"SELECT {columns} FROM {_ident(self.table_name)}{where}{order}{limit}", params, fetch=True)
        data: Any = rows
        if self.single_value:
            if not rows:
                raise LookupError(f"No row found in {self.table_name}")
            data = rows[0]
        elif self.maybe_single_value:
            data = rows[0] if rows else None
        return AdapterResponse(data=data, count=len(rows) if self.count_mode else None)

    def _execute_insert(self) -> AdapterResponse:
        rows = self.payload if isinstance(self.payload, list) else [self.payload]
        if not rows:
            return AdapterResponse(data=[])
        columns = list(rows[0].keys())
        values = [[row.get(column) for column in columns] for row in rows]
        placeholders = ", ".join(["%s"] * len(columns))
        conflict = ""
        if self.operation == "upsert":
            conflict_columns = [part.strip() for part in (self.on_conflict or "").split(",") if part.strip()]
            if not conflict_columns:
                raise ValueError("PostgreSQL upsert requires on_conflict")
            updates = ", ".join(f"{_ident(column)} = EXCLUDED.{_ident(column)}" for column in columns if column not in conflict_columns)
            conflict = f" ON CONFLICT ({', '.join(_ident(column) for column in conflict_columns)}) DO UPDATE SET {updates}"
        sql = f"INSERT INTO {_ident(self.table_name)} ({', '.join(_ident(column) for column in columns)}) VALUES ({placeholders}){conflict}{self._returning()}"
        result: List[Dict[str, Any]] = []
        for value_row in values:
            result.extend(self.adapter.execute(sql, value_row, fetch=True))
        return AdapterResponse(data=result)

    def _execute_update(self) -> AdapterResponse:
        columns = list((self.payload or {}).keys())
        assignments = ", ".join(f"{_ident(column)} = %s" for column in columns)
        where, params = self._where()
        values = [self.payload[column] for column in columns] + params
        rows = self.adapter.execute(f"UPDATE {_ident(self.table_name)} SET {assignments}{where}{self._returning()}", values, fetch=True)
        return AdapterResponse(data=rows)

    def _execute_delete(self) -> AdapterResponse:
        where, params = self._where()
        rows = self.adapter.execute(f"DELETE FROM {_ident(self.table_name)}{where}{self._returning()}", params, fetch=True)
        return AdapterResponse(data=rows)


class PostgresDatabaseAdapter:
    def __init__(self, database_url: Optional[str] = None):
        self.database_url = database_url or os.getenv("DATABASE_URL", "")
        if not self.database_url:
            raise RuntimeError("PostgreSQL adapter requires DATABASE_URL")

    def table(self, name: str) -> PostgresQuery:
        _ident(name)
        return PostgresQuery(self, name)

    def execute(self, sql: str, params: Optional[Iterable[Any]] = None, *, fetch: bool = False) -> List[Dict[str, Any]]:
        try:
            import psycopg2
            from psycopg2.extras import Json, RealDictCursor
        except ImportError as exc:
            raise RuntimeError("psycopg2-binary is required for PostgreSQL platform adapter") from exc
        adapted = [Json(value) if isinstance(value, dict) else value for value in (params or [])]
        with psycopg2.connect(self.database_url) as connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(sql, adapted)
                if not fetch:
                    return []
                return [dict(row) for row in cursor.fetchall()]


def _split_postgrest_or(expression: str) -> List[str]:
    clauses: List[str] = []
    current: List[str] = []
    depth = 0
    for char in expression:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            clauses.append("".join(current))
            current = []
        else:
            current.append(char)
    if current:
        clauses.append("".join(current))
    return [clause.strip() for clause in clauses if clause.strip()]


def _coerce_postgrest_value(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    return value
