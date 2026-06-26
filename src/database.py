"""
database.py
===========

Capa fina sobre ``pyodbc`` que asegura una conexión de **solo lectura**:

- ``autocommit=True`` (no abrimos transacciones; cualquier intento de
  ``BEGIN/COMMIT`` además es rechazado por el validador).
- ``readonly=True`` en el cursor.
- Timeout de query a partir de la configuración.
- Sesión con ``SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED`` para
  no bloquear escritores y dejar claro que es una sesión "de lectura".
- Reconexión perezosa: si la conexión cae, se restablece en el próximo
  uso.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

import pyodbc

from .config import Settings


log = logging.getLogger("mcp_sqlserver.db")


class DatabaseError(RuntimeError):
    """Error genérico de base de datos para exponer al cliente MCP.

    Mensajes de esta excepción son seguros de mostrar (no incluyen cadena
    de conexión ni stack interno).
    """


class Database:
    """Wrapper thread-safe sobre una conexión pyodbc de solo lectura."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._conn: pyodbc.Connection | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def _connect(self) -> pyodbc.Connection:
        log.info("Abriendo conexión a SQL Server (%s)", self._settings.safe_repr())
        try:
            conn = pyodbc.connect(
                self._settings.odbc_connection_string,
                autocommit=True,
                timeout=self._settings.query_timeout_seconds,
            )
        except pyodbc.Error as exc:
            # No exponer el connection string ni la contraseña.
            log.error("Error conectando a SQL Server: %s", _safe_pyodbc_msg(exc))
            raise DatabaseError("No se pudo conectar a SQL Server.") from None

        # Timeout de query (en pyodbc va en la conexión, no en el cursor).
        # 0 = sin timeout. Usamos el configurado.
        try:
            conn.timeout = self._settings.query_timeout_seconds
        except pyodbc.Error:
            pass

        # Configuración a nivel de sesión.
        try:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;")
                cur.execute("SET NOCOUNT ON;")
                # En pyodbc, timeout del cursor se hereda; lo reforzamos:
                cur.execute("SET LOCK_TIMEOUT 5000;")  # 5s para esperar locks
        except pyodbc.Error as exc:
            log.warning("No se pudieron aplicar SET de sesión: %s", _safe_pyodbc_msg(exc))

        return conn

    # ------------------------------------------------------------------
    def _get_conn(self) -> pyodbc.Connection:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    # ------------------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except pyodbc.Error:
                    pass
                self._conn = None

    # ------------------------------------------------------------------
    @contextmanager
    def cursor(self) -> Iterator[pyodbc.Cursor]:
        """Context manager que entrega un cursor de solo lectura."""
        with self._lock:
            conn = self._get_conn()
            cur = conn.cursor()
            try:
                # El timeout está aplicado a nivel de conexión en _connect().
                yield cur
            finally:
                try:
                    cur.close()
                except pyodbc.Error:
                    pass

    # ------------------------------------------------------------------
    def execute_select(
        self,
        sql: str,
        params: Sequence[Any] = (),
        *,
        max_rows: int | None = None,
    ) -> dict[str, Any]:
        """
        Ejecuta una query (asumida ya validada por ``security.validate_select``)
        y devuelve un dict con ``columns``, ``rows`` y ``row_count``.

        Si ``max_rows`` se especifica, actúa como tope **autoritativo** e
        independiente de la forma de la consulta: solo se traen hasta
        ``max_rows`` filas (``fetchmany``). Así, aunque el SQL no lleve
        ``TOP`` (p. ej. un CTE), el MCP nunca devuelve de más.
        """
        log.info("Ejecutando query (params=%d): %s", len(params), _truncate(sql))
        try:
            with self.cursor() as cur:
                cur.execute(sql, *params)
                if cur.description is None:
                    # Esto no debería ocurrir en una SELECT válida.
                    return {"columns": [], "rows": [], "row_count": 0}

                columns = [col[0] for col in cur.description]
                if max_rows is not None:
                    fetched = cur.fetchmany(max_rows)
                else:
                    fetched = cur.fetchall()
                rows = [_row_to_jsonable(r) for r in fetched]
                return {
                    "columns": columns,
                    "rows": rows,
                    "row_count": len(rows),
                }
        except pyodbc.Error as exc:
            msg = _safe_pyodbc_msg(exc)
            log.error("Error ejecutando query: %s", msg)
            # Reseteamos la conexión por si quedó en estado inconsistente.
            self.close()
            raise DatabaseError(f"Error ejecutando la consulta: {msg}") from None


# ---------------------------------------------------------------------------
# Utilidades privadas
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int = 500) -> str:
    one_line = " ".join(text.split())
    if len(one_line) <= limit:
        return one_line
    return one_line[:limit] + "..."


def _safe_pyodbc_msg(exc: pyodbc.Error) -> str:
    """
    Devuelve un mensaje útil pero sin filtrar credenciales.

    pyodbc.Error.args suele ser (sqlstate, message). Tomamos solo la
    parte humana y la sanitizamos por si el driver echó el connection
    string en el mensaje.
    """
    msg = ""
    if exc.args:
        if len(exc.args) >= 2 and isinstance(exc.args[1], str):
            msg = exc.args[1]
        else:
            msg = str(exc.args[0])
    else:
        msg = str(exc)

    # Sanitizar tokens sensibles si aparecieran.
    for token in ("PWD=", "Pwd=", "pwd="):
        if token in msg:
            msg = msg.split(token)[0] + token + "***"
            break
    return msg.strip()


def _row_to_jsonable(row: pyodbc.Row) -> list[Any]:
    """Convierte una fila a tipos JSON-serializables básicos."""
    out: list[Any] = []
    for value in row:
        if value is None:
            out.append(None)
        elif isinstance(value, (str, int, float, bool)):
            out.append(value)
        elif isinstance(value, (bytes, bytearray, memoryview)):
            out.append(f"<binary {len(bytes(value))} bytes>")
        else:
            # datetime, Decimal, uuid, etc. -> str() es suficiente para el cliente MCP.
            out.append(str(value))
    return out
