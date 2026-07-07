@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title E2E Assistant

if not exist ".venv\Scripts\python.exe" (
    echo [X] Not set up yet. Run setup.bat first - one time only.
    pause
    exit /b 1
)

rem Already running? Then just open the browser again.
powershell -NoProfile -Command "try { (New-Object Net.Sockets.TcpClient('localhost', 8501)).Close(); exit 0 } catch { exit 1 }" >nul 2>nul
if not errorlevel 1 (
    start "" http://localhost:8501
    exit /b 0
)

echo Starting E2E Assistant server...
start "E2E Assistant server (close this window to stop)" /min ".venv\Scripts\python.exe" -m streamlit run app.py

rem Wait until the server answers on port 8501 - up to 60 seconds.
powershell -NoProfile -Command "for ($i = 0; $i -lt 120; $i++) { try { (New-Object Net.Sockets.TcpClient('localhost', 8501)).Close(); exit 0 } catch { Start-Sleep -Milliseconds 500 } }; exit 1" >nul 2>nul
if errorlevel 1 (
    echo [X] Server did not start within 60 seconds.
    echo     Check the minimized "E2E Assistant server" window for errors.
    pause
    exit /b 1
)

start "" http://localhost:8501
exit /b 0
