# MCP SQL Server (Read-Only)

Servidor **MCP (Model Context Protocol)** seguro de **solo lectura** para conectar Claude Desktop (o cualquier cliente MCP) a una base de datos **SQL Server 2019**.

> Diseñado bajo el principio **secure by default**: el MCP nunca puede modificar datos ni estructura. La seguridad se aplica en múltiples capas (validación de SQL, permisos del usuario, configuración de sesión, timeouts y límites de filas).

---

## Tabla de contenido

1. [Características](#características)
2. [Arquitectura de seguridad](#arquitectura-de-seguridad)
3. [Requisitos previos](#requisitos-previos)
4. [Instalación paso a paso](#instalación-paso-a-paso)
5. [Configuración del usuario SQL](#configuración-del-usuario-sql)
6. [Variables de entorno](#variables-de-entorno)
7. [Registro en Claude Desktop](#registro-en-claude-desktop)
8. [Tools expuestos](#tools-expuestos)
9. [Pruebas](#pruebas)
10. [Recomendaciones de seguridad adicionales](#recomendaciones-de-seguridad-adicionales)
11. [Solución de problemas](#solución-de-problemas)

---

## Características

- 100% **solo lectura**: cualquier intento de `INSERT/UPDATE/DELETE/MERGE/TRUNCATE/DROP/ALTER/CREATE/EXEC` es rechazado **antes** de llegar a la base de datos.
- Validador de SQL multicapa: tokenización con `sqlparse`, lista blanca de tipos de sentencia, lista negra de palabras clave, detección de comentarios maliciosos y de múltiples sentencias.
- Conexión configurada como **READ ONLY** a nivel de sesión (`SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED` y `ApplicationIntent=ReadOnly` cuando aplica).
- Límite de filas obligatorio (TOP N) en todos los tools.
- Timeout de query configurable.
- Logging estructurado de cada consulta ejecutada.
- Manejo de errores que **no expone** información sensible (cadenas de conexión, stack traces) al cliente MCP.
- Credenciales gestionadas exclusivamente vía `.env` / variables de entorno.

---

## Arquitectura de seguridad

El MCP aplica **defensa en profundidad**:

| Capa | Control |
|------|---------|
| 1. Cliente MCP | Solo expone tools de lectura. No hay tool que acepte DDL/DML. |
| 2. Validador SQL (`security.py`) | Whitelist (`SELECT`/CTE) + blacklist de keywords + análisis con `sqlparse` + bloqueo de múltiples sentencias y `EXEC`. |
| 3. Capa de aplicación (`database.py`) | Conexión `autocommit=True`, `readonly=True`, timeout de query, parámetros enlazados. |
| 4. Sesión SQL | `SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED` y rechazo de cualquier batch con más de un statement. |
| 5. Permisos en SQL Server | Usuario dedicado con **únicamente** rol `db_datareader` (sin `db_ddladmin`, sin `db_datawriter`, sin `db_owner`). |
| 6. Red / SO | Recomendaciones para firewall, TLS, y rotación de credenciales. |

> Aunque se rompiera una capa (p. ej. una validación), las demás siguen impidiendo la escritura. El control **definitivo** son los permisos del usuario SQL.

---

## Requisitos previos

- **Python 3.10+**
- **SQL Server 2019** accesible por red
- **ODBC Driver 17 for SQL Server** (o 18) instalado en la máquina donde corre el MCP
  - Windows: [Microsoft ODBC Driver](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)
  - Linux/macOS: ver guía oficial de Microsoft
- **Claude Desktop** (o cualquier cliente MCP compatible)

---

## Instalación paso a paso

```bash
# 1. Clonar / situarse en el proyecto
cd C:\xampp\htdocs\MCP_SQLSERVER

# 2. Crear y activar un entorno virtual
python -m venv .venv
.venv\Scripts\activate           # Windows
# source .venv/bin/activate      # Linux / macOS

# 3. Instalar dependencias
pip install -r requirements.txt

# 4. Copiar la plantilla de variables de entorno
copy .env.example .env           # Windows
# cp .env.example .env           # Linux / macOS

# 5. Editar .env con tus credenciales (ver sección siguiente)

# 6. Probar que conecta y los tools funcionan
python -m src.server --selftest
```

---

## Configuración del usuario SQL

Crea un usuario dedicado con **solo el rol `db_datareader`**. No le otorgues `db_owner`, `db_ddladmin` ni `db_datawriter`.

```sql
-- Ejecutar en SQL Server (con un usuario admin)
USE master;
CREATE LOGIN mcp_reader WITH PASSWORD = 'CONTRASEÑA_FUERTE_AQUI';

USE TuBaseDeDatos;
CREATE USER mcp_reader FOR LOGIN mcp_reader;
ALTER ROLE db_datareader ADD MEMBER mcp_reader;

-- (Opcional) Denegar explícitamente cualquier intento de escritura
DENY INSERT, UPDATE, DELETE, ALTER, EXECUTE TO mcp_reader;

-- (Opcional) Si el servidor admite ApplicationIntent=ReadOnly,
-- conviene apuntar a una réplica de solo lectura.
```

Repite el `CREATE USER ... db_datareader` en cada base de datos que deba poder consultar.

---

## Variables de entorno

Todas las credenciales viven en `.env` (nunca en el código).

```ini
# .env  (NO subir a git)
SQLSERVER_HOST=localhost
SQLSERVER_PORT=1433
SQLSERVER_DATABASE=TuBaseDeDatos
SQLSERVER_USER=mcp_reader
SQLSERVER_PASSWORD=CONTRASEÑA_FUERTE_AQUI
SQLSERVER_DRIVER=ODBC Driver 17 for SQL Server
SQLSERVER_ENCRYPT=yes
SQLSERVER_TRUST_SERVER_CERT=no

# Límites operativos
MCP_QUERY_TIMEOUT_SECONDS=15
MCP_MAX_ROWS=100
MCP_LOG_LEVEL=INFO
MCP_LOG_FILE=mcp_sqlserver.log
```

> **Manejo de credenciales (no van quemadas en el código):**
> - El código **nunca** contiene credenciales: todo sale de variables de entorno / `.env`.
> - **Precedencia:** las variables del sistema operativo ganan sobre el `.env` (`load_dotenv(override=False)`). En producción puedes definir las variables a nivel de SO/usuario de Windows y **prescindir del archivo `.env`**.
> - `.env` está en `.gitignore`; la plantilla `.env.example` **no** lleva secretos (solo placeholders).
> - La contraseña **no se escribe en logs** (`Settings.safe_repr()` la omite); sí se registran host, base y usuario para diagnóstico.
> - Si una contraseña estuvo expuesta en un archivo compartido, **rótala** en SQL Server.

---

## Registro en Claude Desktop

Edita `claude_desktop_config.json` (Windows: `%APPDATA%\Claude\claude_desktop_config.json`).

```json
{
  "mcpServers": {
    "sqlserver-readonly": {
      "command": "C:\\xampp\\htdocs\\MCP_SQLSERVER\\.venv\\Scripts\\python.exe",
      "args": ["-m", "src.server"],
      "cwd": "C:\\xampp\\htdocs\\MCP_SQLSERVER"
    }
  }
}
```

Reinicia Claude Desktop. Si todo está bien, verás los tools `execute_select_query`, `list_databases`, `list_tables`, `describe_table`, `preview_table_data` disponibles.

> Hay un ejemplo listo para copiar en `claude_desktop_config.example.json`.

---

## Tools expuestos

| Tool | Descripción | Parámetros principales |
|------|-------------|------------------------|
| `execute_select_query` | Ejecuta una consulta `SELECT` validada. | `query: str`, `max_rows: int (opcional)` |
| `list_databases` | Lista todas las bases de datos visibles. | — |
| `list_tables` | Lista tablas y vistas de un esquema. | `database: str (opcional)`, `schema: str (opcional)` |
| `describe_table` | Devuelve columnas, tipos, nullability y PK de una tabla. | `table: str`, `schema: str (opcional)`, `database: str (opcional)` |
| `preview_table_data` | Devuelve las primeras N filas de una tabla. | `table: str`, `rows: int (≤ MCP_MAX_ROWS)`, `schema: str (opcional)` |

Todos los tools:
- Aplican el validador de seguridad.
- Aplican `TOP N` (N ≤ `MCP_MAX_ROWS`).
- Aplican el timeout de query.
- Registran la consulta en el log.

---

## Pruebas

```bash
pip install -r requirements-dev.txt
pytest -v
```

La suite cubre el validador (`tests/test_security.py`) con casos como:
- `INSERT/UPDATE/DELETE/MERGE/TRUNCATE/DROP/ALTER/CREATE` → rechazo
- `EXEC sp_x` y `EXECUTE` → rechazo
- Multi-sentencia (`SELECT 1; DROP TABLE x`) → rechazo
- Inyección por comentarios (`SELECT 1 -- ; DROP ...`) → rechazo
- `SELECT` válido → aceptado

---

## Recomendaciones de seguridad adicionales

1. **Réplica de solo lectura**. Apunta el MCP a una *Always On Availability Group* secundaria con `ApplicationIntent=ReadOnly`. Aunque algo se escapara, físicamente no se puede escribir.
2. **Cifrado en tránsito**. Mantén `SQLSERVER_ENCRYPT=yes` y, salvo desarrollo, `SQLSERVER_TRUST_SERVER_CERT=no` con un certificado válido.
3. **Rotación de credenciales**. Usa un gestor (Azure Key Vault, AWS Secrets Manager, HashiCorp Vault) en producción en lugar de un `.env` plano.
4. **Firewall**. Limita el acceso al puerto 1433 solo desde la máquina del MCP.
5. **Auditoría**. Activa SQL Server Audit sobre `mcp_reader` para registrar 100% de las consultas a nivel servidor (no solo del MCP).
6. **No conceder `VIEW SERVER STATE`** ni `VIEW ANY DEFINITION` salvo que sea necesario; el MCP funciona sin ellos sobre catálogos estándar.
7. **Quotas de recursos**. Considera un Resource Governor pool para `mcp_reader` que limite CPU/IO.
8. **`.env` fuera de control de versiones** (ya está en `.gitignore`).
9. **Actualiza dependencias** regularmente (`pip-audit`).
10. **No deshabilites el validador**, ni siquiera “temporalmente”.

---

## Solución de problemas

- **`Login failed for user 'mcp_reader'`** → revisa usuario/contraseña en `.env` y que el usuario exista en la base destino.
- **`Driver not found`** → instala el ODBC Driver 17/18 y verifica el valor de `SQLSERVER_DRIVER`.
- **`Query rejected: ...`** → el validador detectó algo no permitido. Revisa que sea una sola sentencia `SELECT` sin `EXEC`, sin `;` extra y sin comentarios sospechosos.
- **Timeouts** → ajusta `MCP_QUERY_TIMEOUT_SECONDS` o revisa el plan de la consulta. No incrementes el timeout para “tapar” consultas problemáticas.

---

## Licencia

Uso interno. Ajustar según política de tu organización.
