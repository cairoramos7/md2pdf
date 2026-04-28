@echo off
cd /d "%~dp0"

:: Verificar se ja esta rodando na porta 8050
netstat -ano 2>nul | findstr ":8050 " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    start "" http://localhost:8050
    exit /b
)

:: Iniciar servidor (janela minimizada)
start "md2pdf-server" /min "%~dp0venv\Scripts\python.exe" "%~dp0app.py"

:: Aguardar servidor subir (3 segundos)
ping -n 4 127.0.0.1 >nul

:: Abrir navegador
start "" http://localhost:8050
