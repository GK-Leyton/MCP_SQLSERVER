"""
tools.py
========

Implementación de los tools MCP. Cada tool:

1. Valida sus argumentos.
2. Construye una query parametrizada *cuando sea posible* (los nombres
   de objetos ─schema, tabla, base de datos─ no pueden ir parametrizados,
   por lo que se sanean con una whitelist estricta de identificadores).
3. Pasa la query por ``security.validate_select`` antes de ejecutarla.
4. Aplica el límite ``MCP_MAX_ROWS``.
5. Devuelve un payload JSON-serializable.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .config import Settings
from .database import Database, DatabaseError
from .security import UnsafeQueryError, validate_select


log = logging.getLogger("mcp_sqlserver.tools")


# ---------------------------------------------------------------------------
# Sanitización de identificadores
# ---------------------------------------------------------------------------

# Identificadores SQL Server "razonables": letras, dígitos, _, $ y #.
# No se permiten corchetes, espacios ni puntos (los manejamos por separado).
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]{0,127}$")


def _sanitize_identifier(value: str, kind: str) -> str:
    """
    Devuelve `value` cuotado con corchetes para usarlo como identificador.
    Lanza ValueError si `value` no parece un identificador legítimo.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"{kind} vacío o inválido.")
    if not _IDENT_RE.match(value):
        raise ValueError(
            f"{kind} inválido: '{value}'. Solo letras, dígitos, '_', '$', '#'."
        )
    # Quoting con corchetes es la forma estándar en T-SQL.
    return f"[{value}]"


def _qualified(*parts: str) -> str:
    """Une partes ya cuotadas con puntos: [db].[schema].[table]"""
    return ".".join(parts)


def _clamp_rows(requested: int | None, settings: Settings) -> int:
    if requested is None:
        return settings.max_rows
    if not isinstance(requested, int):
        raise ValueError("max_rows debe ser entero.")
    if requested <= 0:
        raise ValueError("max_rows debe ser > 0.")
    return min(requested, settings.max_rows)


# ---------------------------------------------------------------------------
# Resultado uniforme
# ---------------------------------------------------------------------------


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _err(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


# ---------------------------------------------------------------------------
# Tool: execute_select_query
# ---------------------------------------------------------------------------


def tool_execute_select_query(
    db: Database,
    settings: Settings,
    *,
    query: str,
    max_rows: int | None = None,
) -> dict[str, Any]:
    """Ejecuta una SELECT validada y devuelve filas + columnas."""
    try:
        limit = _clamp_rows(max_rows, settings)
    except ValueError as exc:
        return _err(str(exc))

    try:
        result = validate_select(query)
        result.raise_if_invalid()
        safe_sql = result.normalized_sql
    except UnsafeQueryError as exc:
        log.warning("Query rechazada por validador: %s", exc)
        return _err(f"Query rechazada: {exc}")

    # Garantiza el límite de filas sin romper ORDER BY ni CTEs. El tope
    # definitivo lo aplica fetchmany(limit) en la capa de base de datos.
    sql_to_exec = _inject_row_limit(safe_sql, limit)

    try:
        payload = db.execute_select(sql_to_exec, max_rows=limit)
    except DatabaseError as exc:
        return _err(str(exc))

    payload["row_limit_applied"] = limit
    return _ok(payload)


# Detecta un limitador de filas ya presente: TOP (...) o paginación
# OFFSET ... ROWS [FETCH ...].
_HAS_TOP_RE = re.compile(r"^\s*SELECT\s+(?:ALL\s+|DISTINCT\s+)?TOP\b", re.IGNORECASE)
_HAS_OFFSET_RE = re.compile(r"\bOFFSET\b\s+.+?\bROWS?\b", re.IGNORECASE)

# Cabecera del SELECT donde inyectar el TOP, respetando ALL/DISTINCT.
_SELECT_HEAD_RE = re.compile(r"^\s*SELECT\s+(?:ALL\s+|DISTINCT\s+)?", re.IGNORECASE)


def _inject_row_limit(sql: str, limit: int) -> str:
    """
    Garantiza un límite de filas a nivel de motor SIN envolver la consulta
    en una subconsulta.

    El enfoque anterior (``SELECT TOP (N) * FROM (<query>) AS _mcp_sub``)
    rompía dos formas de SELECT perfectamente válidas:

    * ``SELECT ... ORDER BY ...`` sin TOP → SQL Server prohíbe ``ORDER BY``
      dentro de una *derived table* salvo que lleve TOP/OFFSET (error 1033).
    * CTEs ``WITH ... SELECT ...`` → ``WITH`` no puede aparecer dentro de una
      subconsulta entre paréntesis.

    Estrategia actual:

    * Si ya hay ``TOP`` o paginación ``OFFSET/FETCH``, se respeta tal cual.
    * Si es un ``SELECT`` plano, se inyecta ``TOP (N)`` tras
      ``SELECT``/``ALL``/``DISTINCT``. Junto a un ``ORDER BY`` produce la
      forma canónica y eficiente (Top-N sort).
    * Si es un CTE u otra forma no reconocida, se deja intacto; el tope
      efectivo lo garantiza ``fetchmany(limit)`` en la capa de base de datos
      (ver ``Database.execute_select``).
    """
    stripped = sql.rstrip(";").strip()

    if _HAS_TOP_RE.match(stripped) or _HAS_OFFSET_RE.search(stripped):
        return stripped

    head = _SELECT_HEAD_RE.match(stripped)
    if head:
        return f"{stripped[:head.end()]}TOP ({int(limit)}) {stripped[head.end():]}"

    return stripped


# ---------------------------------------------------------------------------
# Tool: list_databases
# ---------------------------------------------------------------------------


def tool_list_databases(db: Database, settings: Settings) -> dict[str, Any]:
    """Lista las bases de datos visibles para el usuario."""
    sql = (
        "SELECT TOP (?) name, database_id, "
        "       CONVERT(varchar(33), create_date, 126) AS create_date, "
        "       state_desc, recovery_model_desc "
        "FROM sys.databases "
        "WHERE state_desc = 'ONLINE' "
        "ORDER BY name"
    )
    # validate_select no acepta '?' bien como token, así que lo construimos ya
    # final con el TOP literal y validamos.
    final_sql = sql.replace("?", str(settings.max_rows), 1)
    try:
        validate_select(final_sql).raise_if_invalid()
    except UnsafeQueryError as exc:
        return _err(f"Error interno de validación: {exc}")

    try:
        return _ok(db.execute_select(final_sql))
    except DatabaseError as exc:
        return _err(str(exc))


# ---------------------------------------------------------------------------
# Tool: list_tables
# ---------------------------------------------------------------------------


def tool_list_tables(
    db: Database,
    settings: Settings,
    *,
    database: str | None = None,
    schema: str | None = None,
) -> dict[str, Any]:
    """Lista tablas y vistas de un esquema (por defecto, todas)."""
    try:
        db_clause = ""
        if database:
            db_clause = _sanitize_identifier(database, "database") + "."
        schema_filter = ""
        if schema:
            _sanitize_identifier(schema, "schema")  # solo valida formato
            schema_filter = f"AND s.name = '{schema}'"

        sql = (
            f"SELECT TOP ({settings.max_rows}) "
            f"s.name AS [schema], o.name AS [name], "
            f"o.type_desc AS [type], "
            f"CONVERT(varchar(33), o.create_date, 126) AS create_date, "
            f"CONVERT(varchar(33), o.modify_date, 126) AS modify_date "
            f"FROM {db_clause}sys.objects o "
            f"JOIN {db_clause}sys.schemas s ON s.schema_id = o.schema_id "
            f"WHERE o.type IN ('U', 'V') "
            f"{schema_filter} "
            f"ORDER BY s.name, o.name"
        )
    except ValueError as exc:
        return _err(str(exc))

    try:
        validate_select(sql).raise_if_invalid()
    except UnsafeQueryError as exc:
        return _err(f"Error interno de validación: {exc}")

    try:
        return _ok(db.execute_select(sql))
    except DatabaseError as exc:
        return _err(str(exc))


# ---------------------------------------------------------------------------
# Tool: describe_table
# ---------------------------------------------------------------------------


def tool_describe_table(
    db: Database,
    settings: Settings,
    *,
    table: str,
    schema: str | None = None,
    database: str | None = None,
) -> dict[str, Any]:
    """Devuelve columnas, tipos, nullability e info de PK de una tabla."""
    try:
        _sanitize_identifier(table, "table")
        if schema:
            _sanitize_identifier(schema, "schema")
        if database:
            _sanitize_identifier(database, "database")
    except ValueError as exc:
        return _err(str(exc))

    schema_filter = f"AND s.name = '{schema}'" if schema else ""
    db_prefix = f"[{database}]." if database else ""

    sql = f"""
        SELECT TOP ({settings.max_rows})
            s.name AS [schema],
            t.name AS [table],
            c.name AS column_name,
            ty.name AS data_type,
            c.max_length,
            c.precision,
            c.scale,
            c.is_nullable,
            c.is_identity,
            CASE WHEN ic.column_id IS NOT NULL THEN 1 ELSE 0 END AS is_primary_key
        FROM {db_prefix}sys.columns c
        JOIN {db_prefix}sys.tables t      ON t.object_id = c.object_id
        JOIN {db_prefix}sys.schemas s     ON s.schema_id = t.schema_id
        JOIN {db_prefix}sys.types ty      ON ty.user_type_id = c.user_type_id
        LEFT JOIN {db_prefix}sys.indexes i
               ON i.object_id = t.object_id AND i.is_primary_key = 1
        LEFT JOIN {db_prefix}sys.index_columns ic
               ON ic.object_id = t.object_id
              AND ic.index_id = i.index_id
              AND ic.column_id = c.column_id
        WHERE t.name = '{table}'
          {schema_filter}
        ORDER BY c.column_id
    """

    try:
        validate_select(sql).raise_if_invalid()
    except UnsafeQueryError as exc:
        return _err(f"Error interno de validación: {exc}")

    try:
        return _ok(db.execute_select(sql))
    except DatabaseError as exc:
        return _err(str(exc))


# ---------------------------------------------------------------------------
# Tool: preview_table_data
# ---------------------------------------------------------------------------


def tool_preview_table_data(
    db: Database,
    settings: Settings,
    *,
    table: str,
    rows: int | None = None,
    schema: str | None = None,
    database: str | None = None,
) -> dict[str, Any]:
    """Devuelve las primeras N filas de la tabla (TOP N)."""
    try:
        limit = _clamp_rows(rows, settings)
        table_q = _sanitize_identifier(table, "table")
        schema_q = _sanitize_identifier(schema, "schema") if schema else None
        db_q = _sanitize_identifier(database, "database") if database else None
    except ValueError as exc:
        return _err(str(exc))

    parts = [p for p in (db_q, schema_q, table_q) if p]
    full_name = _qualified(*parts)

    sql = f"SELECT TOP ({int(limit)}) * FROM {full_name}"

    try:
        validate_select(sql).raise_if_invalid()
    except UnsafeQueryError as exc:
        return _err(f"Error interno de validación: {exc}")

    try:
        payload = db.execute_select(sql, max_rows=limit)
        payload["row_limit_applied"] = limit
        return _ok(payload)
    except DatabaseError as exc:
        return _err(str(exc))
