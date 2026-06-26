"""
Tests para la inyección de límite de filas en src/tools.py.

Verifican la corrección del bug por el que envolver la consulta en una
derived table (``SELECT TOP (N) * FROM (<query>) AS _mcp_sub``) rompía
``ORDER BY`` sin TOP (error 1033 de SQL Server) y los CTE.
"""

from __future__ import annotations

import pytest

from src.tools import _inject_row_limit


# ---------------------------------------------------------------------------
# Inyección de TOP en SELECT planos
# ---------------------------------------------------------------------------


def test_select_plano_recibe_top() -> None:
    out = _inject_row_limit("SELECT Id, Name FROM dbo.Users", 50)
    assert out == "SELECT TOP (50) Id, Name FROM dbo.Users"


def test_order_by_sin_top_no_se_envuelve() -> None:
    # Caso del bug: antes se envolvía en una subconsulta -> error 1033.
    out = _inject_row_limit("SELECT Id FROM dbo.Users ORDER BY Id", 10)
    assert out == "SELECT TOP (10) Id FROM dbo.Users ORDER BY Id"
    assert "_mcp_sub" not in out


def test_distinct_conserva_orden_de_palabras() -> None:
    # En T-SQL el orden válido es SELECT DISTINCT TOP (n), no al revés.
    out = _inject_row_limit("SELECT DISTINCT City FROM dbo.Users", 5)
    assert out == "SELECT DISTINCT TOP (5) City FROM dbo.Users"


# ---------------------------------------------------------------------------
# Consultas que ya limitan filas: se respetan tal cual
# ---------------------------------------------------------------------------


def test_top_existente_se_respeta() -> None:
    sql = "SELECT TOP (3) Id FROM dbo.Users"
    assert _inject_row_limit(sql, 100) == sql


def test_top_existente_con_distinct_se_respeta() -> None:
    sql = "SELECT DISTINCT TOP (3) Id FROM dbo.Users"
    assert _inject_row_limit(sql, 100) == sql


def test_offset_fetch_se_respeta() -> None:
    sql = "SELECT Id FROM dbo.Users ORDER BY Id OFFSET 0 ROWS FETCH NEXT 5 ROWS ONLY"
    assert _inject_row_limit(sql, 100) == sql


# ---------------------------------------------------------------------------
# CTE: no se toca (lo limita fetchmany en la capa de BD)
# ---------------------------------------------------------------------------


def test_cte_no_se_envuelve() -> None:
    sql = "WITH cte AS (SELECT Id FROM dbo.Users) SELECT * FROM cte"
    out = _inject_row_limit(sql, 100)
    assert out == sql
    assert "_mcp_sub" not in out


# ---------------------------------------------------------------------------
# Normalización
# ---------------------------------------------------------------------------


def test_punto_y_coma_final_se_elimina() -> None:
    out = _inject_row_limit("SELECT 1;", 10)
    assert out == "SELECT TOP (10) 1"
