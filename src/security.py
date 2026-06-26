"""
security.py
===========

Validador de SQL de **solo lectura** para el MCP.

Reglas (defensa en profundidad):

1. La cadena debe contener exactamente UNA sentencia ejecutable.
2. La sentencia debe ser SELECT (o un WITH ... SELECT, es decir, un CTE
   que termina en SELECT).
3. Se rechazan keywords de DDL/DML/control de transacciones aunque
   aparezcan en lugares "creativos" (subqueries, CTEs, hints).
4. Se rechazan comentarios SQL (`--` y `/* */`) que puedan usarse para
   ocultar payloads o partir el statement (defensa anti-bypass).
5. Se rechaza cualquier `;` adicional que sugiera múltiples sentencias.
6. Se rechaza `EXEC`/`EXECUTE` y `sp_executesql`.

NO confiamos en la validación como única capa: el usuario SQL debe ser
`db_datareader`. Pero esta capa evita que consultas peligrosas siquiera
salgan del proceso del MCP.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

import sqlparse
from sqlparse.sql import Statement
from sqlparse.tokens import (
    Comment,
    DDL,
    DML,
    Keyword,
    Punctuation,
    Whitespace,
    Newline,
)


# ---------------------------------------------------------------------------
# Configuración de listas
# ---------------------------------------------------------------------------

# Tipos de sentencia permitidos (resultado de Statement.get_type()).
ALLOWED_STATEMENT_TYPES: frozenset[str] = frozenset({"SELECT"})

# Keywords absolutamente prohibidos en cualquier posición del query.
FORBIDDEN_KEYWORDS: frozenset[str] = frozenset(
    {
        # DDL
        "CREATE",
        "ALTER",
        "DROP",
        "TRUNCATE",
        "RENAME",
        # DML de escritura
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "UPSERT",
        "REPLACE",
        # Control / ejecución
        "EXEC",
        "EXECUTE",
        "CALL",
        "GRANT",
        "REVOKE",
        "DENY",
        # Transacciones
        "COMMIT",
        "ROLLBACK",
        "SAVEPOINT",
        "BEGIN",
        # Bulk / utilitarios destructivos
        "BULK",
        "BACKUP",
        "RESTORE",
        "SHUTDOWN",
        "RECONFIGURE",
        # T-SQL específicos potencialmente peligrosos
        "DBCC",
        "KILL",
        "OPENROWSET",
        "OPENQUERY",
        "OPENDATASOURCE",
        "WAITFOR",
        # Funciones/procedimientos peligrosos
        "XP_CMDSHELL",
        "SP_EXECUTESQL",
        "SP_CONFIGURE",
    }
)

# Patrones compuestos prohibidos (post-normalización de espacios).
FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bSELECT\b[\s\S]+?\bINTO\b", re.IGNORECASE),  # SELECT ... INTO new_table
    re.compile(r"\bFOR\s+UPDATE\b", re.IGNORECASE),
    re.compile(r"\bFOR\s+XML\s+EXPLICIT\b", re.IGNORECASE),
)


# ---------------------------------------------------------------------------
# Resultado de la validación
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationResult:
    """Resultado inmutable de validar una query."""

    ok: bool
    reason: str = ""
    normalized_sql: str = ""

    def raise_if_invalid(self) -> None:
        """Lanza UnsafeQueryError si la query no pasó."""
        if not self.ok:
            raise UnsafeQueryError(self.reason)


class UnsafeQueryError(ValueError):
    """Se lanza cuando una query no supera la validación de seguridad."""


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def validate_select(sql: str) -> ValidationResult:
    """
    Valida que ``sql`` sea una única sentencia SELECT segura.
    """
    if sql is None:
        return ValidationResult(False, "La query es None.")
    if not isinstance(sql, str):
        return ValidationResult(False, "La query debe ser un string.")

    raw = sql.strip()
    if not raw:
        return ValidationResult(False, "La query está vacía.")

    # 1) Comentarios -> NO permitidos.
    if _contains_sql_comment(raw):
        return ValidationResult(
            False,
            "Los comentarios SQL ('--' o '/* */') no están permitidos.",
        )

    # 2) Más de un statement detectable.
    parsed_statements = [s for s in sqlparse.parse(raw) if str(s).strip()]
    if len(parsed_statements) != 1:
        return ValidationResult(
            False,
            f"Se esperaba exactamente 1 sentencia, se detectaron "
            f"{len(parsed_statements)}.",
        )

    statement: Statement = parsed_statements[0]

    # 3) ';' interno (ignorando los que están dentro de strings).
    if _has_internal_semicolon(raw):
        return ValidationResult(
            False,
            "Se detectó ';' separando sentencias. Solo se permite una sola "
            "sentencia SELECT.",
        )

    # 4) Tipo de sentencia.
    stmt_type = (statement.get_type() or "").upper()
    if stmt_type not in ALLOWED_STATEMENT_TYPES:
        if not _is_safe_cte(statement):
            return ValidationResult(
                False,
                f"Solo se permiten sentencias SELECT. Detectado: "
                f"{stmt_type or 'desconocido'}.",
            )

    # 5) Token-walk: rechazar cualquier keyword prohibido.
    forbidden = _find_forbidden_keyword(statement)
    if forbidden is not None:
        return ValidationResult(False, f"Keyword prohibido detectado: '{forbidden}'.")

    # 6) Patrones compuestos.
    flat = " ".join(raw.split())
    for pattern in FORBIDDEN_PATTERNS:
        if pattern.search(flat):
            return ValidationResult(False, f"Patrón prohibido detectado: {pattern.pattern}")

    # 7) Fallback regex por palabra completa (defensa redundante). Para no
    # confundirse con keywords dentro de literales de cadena, removemos
    # primero los strings antes de la búsqueda.
    sanitized = _strip_string_literals(flat).upper()
    for kw in FORBIDDEN_KEYWORDS:
        if re.search(rf"(?<![A-Z0-9_]){re.escape(kw)}(?![A-Z0-9_])", sanitized):
            return ValidationResult(False, f"Keyword prohibido detectado: '{kw}'.")

    return ValidationResult(True, "", normalized_sql=flat)


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------


_COMMENT_RE = re.compile(r"--|/\*|\*/")


def _contains_sql_comment(sql: str) -> bool:
    """True si la query contiene comentarios SQL `--` o `/* */`."""
    return bool(_COMMENT_RE.search(sql))


def _has_internal_semicolon(sql: str) -> bool:
    """
    True si hay un `;` separando sentencias. Ignora `;` dentro de literales
    de cadena ('...'), entre comillas dobles ("...") y entre corchetes
    ([...]). Un único `;` final está permitido.
    """
    candidate = sql.rstrip()
    if candidate.endswith(";"):
        candidate = candidate[:-1]

    in_single = False
    in_double = False
    in_bracket = False
    i = 0
    while i < len(candidate):
        ch = candidate[i]
        if in_single:
            if ch == "'":
                # '' es escape de comilla en T-SQL.
                if i + 1 < len(candidate) and candidate[i + 1] == "'":
                    i += 2
                    continue
                in_single = False
        elif in_double:
            if ch == '"':
                in_double = False
        elif in_bracket:
            if ch == "]":
                in_bracket = False
        else:
            if ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            elif ch == "[":
                in_bracket = True
            elif ch == ";":
                return True
        i += 1
    return False


def _strip_string_literals(sql: str) -> str:
    """Devuelve `sql` con todo lo que esté entre '...' reemplazado por ''."""
    out: list[str] = []
    in_single = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if in_single:
            if ch == "'":
                if i + 1 < len(sql) and sql[i + 1] == "'":
                    i += 2
                    continue
                in_single = False
                out.append("'")
            # else: drop char inside string
        else:
            if ch == "'":
                in_single = True
                out.append("'")
            else:
                out.append(ch)
        i += 1
    return "".join(out)


def _iter_meaningful_tokens(stmt: Statement) -> Iterable:
    """Recorre todos los tokens del árbol, ignorando whitespace y comentarios."""
    for token in stmt.flatten():
        if token.ttype in (Whitespace, Newline, Comment, Comment.Single, Comment.Multiline):
            continue
        if token.is_whitespace:
            continue
        yield token


def _find_forbidden_keyword(stmt: Statement) -> str | None:
    """Devuelve el primer keyword prohibido que encuentre, o None."""
    for token in _iter_meaningful_tokens(stmt):
        is_kw = (
            token.ttype in (Keyword, DDL, DML)
            or (token.ttype is not None and "Keyword" in str(token.ttype))
        )
        if not is_kw:
            continue
        value = token.value.upper().strip()
        # value puede traer "INSERT INTO" como una sola pieza.
        for piece in value.split():
            if piece in FORBIDDEN_KEYWORDS:
                return piece
    return None


def _is_safe_cte(stmt: Statement) -> bool:
    """
    Para un statement cuyo `get_type()` no es SELECT (típicamente CTE
    `WITH ... SELECT`), comprueba que:
      - empieza por WITH
      - eventualmente aparece un SELECT
      - no contiene ningún keyword prohibido.
    """
    first_kw = None
    saw_select = False

    for token in _iter_meaningful_tokens(stmt):
        if first_kw is None and (token.ttype in (Keyword, DDL, DML)):
            first_kw = token.value.upper().strip()

        if token.ttype is DML and token.value.upper() == "SELECT":
            saw_select = True

        if token.ttype is Punctuation:
            continue

    if first_kw != "WITH":
        return False
    if not saw_select:
        return False

    return _find_forbidden_keyword(stmt) is None
