from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from agentend.tools.base import ToolContext, ToolResult


class DbQueryTool:
    name = "db.query"
    description = "Run a read-only SQLite query and return rows."
    input_schema = {"type": "object", "required": ["sql"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        sql = str(input_data["sql"])
        if not sql.lstrip().lower().startswith("select"):
            raise ValueError("db.query only accepts SELECT statements")
        rows = _run_query(_database_path(context, input_data.get("database")), sql, input_data.get("params", []))
        data = {"rows": rows, "row_count": len(rows)}
        return ToolResult(content=json.dumps(rows, ensure_ascii=False, indent=2), data=data)


class DbExecuteTool:
    name = "db.execute"
    description = "Execute a SQLite statement that mutates a local database."
    input_schema = {"type": "object", "required": ["sql"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        path = _database_path(context, input_data.get("database"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            cursor = connection.execute(str(input_data["sql"]), input_data.get("params", []))
            connection.commit()
            data = {"database": str(path), "rows_affected": cursor.rowcount}
        return ToolResult(content=json.dumps(data, ensure_ascii=False, sort_keys=True), data=data)


class DbWriteRowsTool:
    name = "db.write_rows"
    description = "Insert JSON rows into a SQLite table."
    input_schema = {"type": "object", "required": ["table", "rows"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        path = _database_path(context, input_data.get("database"))
        table = _identifier(str(input_data["table"]))
        rows = input_data.get("rows", [])
        if not isinstance(rows, list) or not rows:
            raise ValueError("rows must be a non-empty list")
        columns = list(rows[0].keys())
        if not columns:
            raise ValueError("rows must contain at least one column")
        for row in rows:
            if set(row.keys()) != set(columns):
                raise ValueError("all rows must use the same columns")
        safe_columns = [_identifier(str(column)) for column in columns]
        placeholders = ", ".join("?" for _ in safe_columns)
        sql = f"insert into {table} ({', '.join(safe_columns)}) values ({placeholders})"
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.executemany(sql, [[row[column] for column in columns] for row in rows])
            connection.commit()
        data = {"database": str(path), "table": table, "inserted": len(rows)}
        return ToolResult(content=json.dumps(data, ensure_ascii=False, sort_keys=True), data=data)


def _run_query(path: Path, sql: str, params: Any) -> list[dict]:
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        cursor = connection.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]


def _database_path(context: ToolContext, value: object | None) -> Path:
    raw = Path(str(value or "data/agentend.sqlite"))
    return raw if raw.is_absolute() else (context.home / raw).resolve()


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Invalid SQLite identifier: {value}")
    return value


DB_TOOLS = [DbQueryTool(), DbExecuteTool(), DbWriteRowsTool()]
