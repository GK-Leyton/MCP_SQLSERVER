@echo off
REM Wrapper para arrancar el MCP SQL Server (read-only) sin depender de "cwd"
REM en el config de Claude Desktop. Cambiamos al directorio del proyecto
REM y llamamos al Python del venv con el modulo del servidor.

cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" -m src.server
