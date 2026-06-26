"""
config.py
=========

Carga y valida la configuración del MCP a partir de variables de entorno
(o un archivo `.env`). Falla rápido si falta algo crítico, para que el
servidor no arranque con una configuración insegura o incompleta.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


# Cargar .env si existe en la raíz del proyecto. `override=False` para
# respetar variables ya definidas en el entorno (p. ej. en producción).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=_PROJECT_ROOT / ".env", override=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class ConfigError(RuntimeError):
    """Error de configuración. Se lanza al arrancar si algo no cuadra."""


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise ConfigError(f"Falta la variable de entorno requerida: {name}")
    return value.strip()


def _optional(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _bool_yn(value: str) -> str:
    """Normaliza 'yes'/'no' (acepta true/false). Devuelve 'yes' o 'no'."""
    v = value.strip().lower()
    if v in ("yes", "true", "1", "y"):
        return "yes"
    if v in ("no", "false", "0", "n"):
        return "no"
    raise ConfigError(f"Valor booleano inválido: {value!r} (esperado yes/no)")


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        v = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} debe ser un entero, recibido: {raw!r}") from exc
    if v <= 0:
        raise ConfigError(f"{name} debe ser > 0, recibido: {v}")
    return v


# ---------------------------------------------------------------------------
# Dataclass principal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    # Conexión
    host: str
    port: int
    database: str
    user: str
    password: str
    driver: str
    encrypt: str  # 'yes' / 'no'
    trust_server_cert: str  # 'yes' / 'no'
    application_intent: str  # 'ReadOnly' o ''

    # Límites
    query_timeout_seconds: int
    max_rows: int

    # Logging
    log_level: str
    log_file: str

    # ------------------------------------------------------------------
    @property
    def odbc_connection_string(self) -> str:
        """
        Construye el connection string ODBC. NO lo loguees.
        """
        parts = [
            f"DRIVER={{{self.driver}}}",
            f"SERVER={self.host},{self.port}",
            f"DATABASE={self.database}",
            f"UID={self.user}",
            f"PWD={self.password}",
            f"Encrypt={self.encrypt}",
            f"TrustServerCertificate={self.trust_server_cert}",
        ]
        if self.application_intent:
            parts.append(f"ApplicationIntent={self.application_intent}")
        return ";".join(parts) + ";"

    def safe_repr(self) -> str:
        """Representación segura para logs (sin contraseña)."""
        return (
            f"Settings(host={self.host!r}, port={self.port}, "
            f"database={self.database!r}, user={self.user!r}, "
            f"driver={self.driver!r}, encrypt={self.encrypt}, "
            f"trust_server_cert={self.trust_server_cert}, "
            f"application_intent={self.application_intent!r}, "
            f"query_timeout_seconds={self.query_timeout_seconds}, "
            f"max_rows={self.max_rows}, log_level={self.log_level})"
        )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_settings() -> Settings:
    """Lee variables de entorno y devuelve un ``Settings`` validado."""
    host = _required("SQLSERVER_HOST")
    port = _positive_int("SQLSERVER_PORT", default=1433)
    database = _required("SQLSERVER_DATABASE")
    user = _required("SQLSERVER_USER")
    password = _required("SQLSERVER_PASSWORD")
    driver = _optional("SQLSERVER_DRIVER", "ODBC Driver 17 for SQL Server")
    encrypt = _bool_yn(_optional("SQLSERVER_ENCRYPT", "yes"))
    trust_server_cert = _bool_yn(_optional("SQLSERVER_TRUST_SERVER_CERT", "no"))
    application_intent = _optional("SQLSERVER_APPLICATION_INTENT", "")

    timeout = _positive_int("MCP_QUERY_TIMEOUT_SECONDS", default=15)
    max_rows = _positive_int("MCP_MAX_ROWS", default=100)

    log_level = _optional("MCP_LOG_LEVEL", "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError(f"MCP_LOG_LEVEL inválido: {log_level}")

    log_file = _optional("MCP_LOG_FILE", "")

    if max_rows > 10_000:
        # Tope duro: aunque el usuario configure algo absurdo, no
        # devolveremos más de 10k filas en una sola tool call.
        raise ConfigError(
            "MCP_MAX_ROWS no debe superar 10000 por seguridad/operación. "
            f"Recibido: {max_rows}"
        )

    return Settings(
        host=host,
        port=port,
        database=database,
        user=user,
        password=password,
        driver=driver,
        encrypt=encrypt,
        trust_server_cert=trust_server_cert,
        application_intent=application_intent,
        query_timeout_seconds=timeout,
        max_rows=max_rows,
        log_level=log_level,
        log_file=log_file,
    )
