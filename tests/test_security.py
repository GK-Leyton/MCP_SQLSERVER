"""
Tests para src/security.py.

Cubrimos los casos críticos: el validador NUNCA debe permitir DML/DDL,
ejecución de procedimientos, múltiples sentencias o comentarios.
"""

from __future__ import annotations

import pytest

from src.security import UnsafeQueryError, validate_select


# ---------------------------------------------------------------------------
# Casos que DEBEN ser aceptados
# ---------------------------------------------------------------------------


VALID_QUERIES = [
    "SELECT 1",
    "SELECT 1 AS one",
    "SELECT TOP 10 * FROM dbo.Users",
    "SELECT TOP (10) Id, Name FROM dbo.Users WHERE Id > 5",
    "SELECT u.Id, p.Name FROM dbo.Users u JOIN dbo.Profiles p ON p.UserId = u.Id",
    "SELECT COUNT(*) FROM dbo.Orders",
    """SELECT TOP (100) o.Id, o.Total
       FROM dbo.Orders o
       WHERE o.Total > 100
       ORDER BY o.Id DESC""",
    "WITH cte AS (SELECT Id FROM dbo.Users) SELECT * FROM cte",
    "SELECT Id FROM dbo.Users WHERE Name = 'Robert; DROP TABLE x'",  # string literal
]


@pytest.mark.parametrize("sql", VALID_QUERIES)
def test_valid_queries_are_accepted(sql: str) -> None:
    result = validate_select(sql)
    assert result.ok, f"Debería aceptarse, pero fue rechazada: {result.reason}"


# ---------------------------------------------------------------------------
# Casos que DEBEN ser rechazados
# ---------------------------------------------------------------------------


INVALID_QUERIES = [
    # DML escritura
    "INSERT INTO dbo.Users (Id) VALUES (1)",
    "UPDATE dbo.Users SET Name = 'x' WHERE Id = 1",
    "DELETE FROM dbo.Users WHERE Id = 1",
    "MERGE dbo.Users AS T USING dbo.Tmp AS S ON T.Id=S.Id WHEN MATCHED THEN DELETE",
    "TRUNCATE TABLE dbo.Users",
    # DDL
    "DROP TABLE dbo.Users",
    "ALTER TABLE dbo.Users ADD Foo INT",
    "CREATE TABLE dbo.Foo (Id INT)",
    "RENAME OBJECT::dbo.Users TO Foo",
    # Ejecución
    "EXEC sp_help",
    "EXECUTE sp_help",
    "EXEC sp_executesql N'SELECT 1'",
    "exec xp_cmdshell 'dir'",
    # Multi-sentencia
    "SELECT 1; DROP TABLE dbo.Users",
    "SELECT 1; SELECT 2",
    # Comentarios (anti-bypass)
    "SELECT 1 -- comentario",
    "SELECT 1 /* comentario */",
    "SELECT 1 -- ; DROP TABLE x",
    "SELECT /* malicioso */ 1",
    # Transacciones
    "BEGIN TRAN SELECT 1 COMMIT",
    "ROLLBACK",
    # Bulk / utilitarios
    "BACKUP DATABASE foo TO DISK = 'x'",
    "RESTORE DATABASE foo FROM DISK = 'x'",
    "DBCC CHECKDB",
    "BULK INSERT foo FROM 'x'",
    "WAITFOR DELAY '00:00:05'",
    # SELECT INTO (crea tabla nueva → escritura)
    "SELECT * INTO dbo.NewTable FROM dbo.Users",
    # Strings vacíos / no SELECT
    "",
    "   ",
    "ROLLBACK TRANSACTION",
    # OPENROWSET / linked servers
    "SELECT * FROM OPENROWSET('SQLNCLI','...','SELECT 1')",
    "SELECT * FROM OPENQUERY(srv, 'SELECT 1')",
]


@pytest.mark.parametrize("sql", INVALID_QUERIES)
def test_invalid_queries_are_rejected(sql: str) -> None:
    result = validate_select(sql)
    assert not result.ok, f"Debería rechazarse, pero pasó: {sql!r}"


def test_raise_if_invalid_lanza_excepcion() -> None:
    result = validate_select("DROP TABLE x")
    with pytest.raises(UnsafeQueryError):
        result.raise_if_invalid()


def test_none_es_rechazado() -> None:
    # type: ignore[arg-type]
    result = validate_select(None)  # noqa
    assert not result.ok


def test_no_string_es_rechazado() -> None:
    # type: ignore[arg-type]
    result = validate_select(123)  # noqa
    assert not result.ok


def test_select_con_punto_y_coma_final_es_aceptado() -> None:
    # Un único `;` al final es práctica común y no debería bloquear.
    result = validate_select("SELECT 1;")
    assert result.ok, result.reason
