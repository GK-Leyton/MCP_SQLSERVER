"""
server.py
=========

Servidor MCP de solo lectura para SQL Server 2019.

Ejecución:
    python -m src.server               # Inicia el servidor MCP por stdio.
    python -m src.server --selftest    # Verifica configuración y conexión
                                       # sin levantar el servidor.

Cliente recomendado: Claude Desktop (ver README).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .config import ConfigError, Settings, load_settings
from .database import Database, DatabaseError
from .tools import (
    tool_describe_table,
    tool_execute_select_query,
    tool_list_databases,
    tool_list_tables,
    tool_preview_table_data,
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _setup_logging(settings: Settings) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if settings.log_file:
        handlers.append(logging.FileHandler(settings.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
        handlers=handlers,
    )


# ---------------------------------------------------------------------------
# Definición de tools (schemas para el cliente MCP)
# ---------------------------------------------------------------------------


def _tool_definitions() -> list[Tool]:
    return [
        Tool(
            name="execute_select_query",
            description=(
                "Ejecuta una consulta SQL SELECT contra SQL Server 2019. "
                "Solo se permiten sentencias SELECT (incluidos CTEs `WITH ... SELECT`). "
                "Cualquier otra cosa (INSERT/UPDATE/DELETE/MERGE/DDL/EXEC/varias "
                "sentencias/comentarios SQL) es rechazada. El resultado está limitado "
                "por `max_rows` (tope absoluto definido por la configuración del MCP)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Sentencia SELECT a ejecutar.",
                    },
                    "max_rows": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Máximo de filas a devolver. Se aplica TOP automáticamente. "
                            "Si excede el tope del MCP, se recorta."
                        ),
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="list_databases",
            description="Lista las bases de datos online visibles para el usuario MCP.",
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        Tool(
            name="list_tables",
            description=(
                "Lista tablas y vistas. Filtra opcionalmente por base de datos y/o schema. "
                "Si no se especifica `database`, usa la BD actual."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "database": {"type": "string"},
                    "schema": {"type": "string"},
                },
                "additionalProperties": False,
            },
        ),
        Tool(
            name="describe_table",
            description=(
                "Devuelve columnas, tipos, nullability, identity y PK de una tabla."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "table": {"type": "string"},
                    "schema": {"type": "string"},
                    "database": {"type": "string"},
                },
                "required": ["table"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="preview_table_data",
            description=(
                "Devuelve las primeras N filas de una tabla (TOP N), respetando el tope "
                "máximo del MCP."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "table": {"type": "string"},
                    "rows": {"type": "integer", "minimum": 1},
                    "schema": {"type": "string"},
                    "database": {"type": "string"},
                },
                "required": ["table"],
                "additionalProperties": False,
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Construcción del servidor
# ---------------------------------------------------------------------------


def _build_server(settings: Settings, db: Database) -> Server:
    server: Server = Server("mcp-sqlserver-readonly")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return _tool_definitions()

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        args = arguments or {}
        try:
            if name == "execute_select_query":
                payload = tool_execute_select_query(
                    db,
                    settings,
                    query=args.get("query", ""),
                    max_rows=args.get("max_rows"),
                )
            elif name == "list_databases":
                payload = tool_list_databases(db, settings)
            elif name == "list_tables":
                payload = tool_list_tables(
                    db,
                    settings,
                    database=args.get("database"),
                    schema=args.get("schema"),
                )
            elif name == "describe_table":
                payload = tool_describe_table(
                    db,
                    settings,
                    table=args.get("table", ""),
                    schema=args.get("schema"),
                    database=args.get("database"),
                )
            elif name == "preview_table_data":
                payload = tool_preview_table_data(
                    db,
                    settings,
                    table=args.get("table", ""),
                    rows=args.get("rows"),
                    schema=args.get("schema"),
                    database=args.get("database"),
                )
            else:
                payload = {"ok": False, "error": f"Tool desconocido: {name}"}
        except Exception as exc:  # noqa: BLE001 — last line of defense
            # No exponemos el stack al cliente MCP.
            logging.getLogger("mcp_sqlserver.server").exception(
                "Excepción no controlada en tool=%s", name
            )
            payload = {
                "ok": False,
                "error": "Error interno del servidor MCP. Revisa los logs.",
            }

        return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2))]

    return server


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def _run_stdio(settings: Settings, db: Database) -> None:
    server = _build_server(settings, db)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def _run_selftest(settings: Settings, db: Database) -> int:
    """Comprueba conexión + un SELECT trivial. Útil para diagnóstico."""
    log = logging.getLogger("mcp_sqlserver.selftest")
    log.info("Configuración: %s", settings.safe_repr())
    try:
        result = db.execute_select("SELECT TOP (1) 1 AS one")
    except DatabaseError as exc:
        log.error("Selftest FALLÓ: %s", exc)
        return 1
    log.info("Selftest OK: %s", result)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp-sqlserver-readonly")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="Verifica config y conexión, luego sale (no levanta el MCP).",
    )
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"[config] {exc}", file=sys.stderr)
        return 2

    _setup_logging(settings)
    log = logging.getLogger("mcp_sqlserver.server")
    log.info("Arrancando MCP SQL Server (read-only) v1.0.0")

    db = Database(settings)

    try:
        if args.selftest:
            return _run_selftest(settings, db)

        import asyncio

        asyncio.run(_run_stdio(settings, db))
        return 0
    except KeyboardInterrupt:
        log.info("Interrupción por el usuario.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
