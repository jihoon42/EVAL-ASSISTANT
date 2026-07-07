@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title E2E Assistant - one-time setup

echo ============================================
echo  E2E Assistant - one-time setup
echo ============================================

echo.
echo [1/4] Checking Python...
python -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>nul
if errorlevel 1 (
    echo [X] Python 3.10+ not found on PATH.
    echo     Install from https://www.python.org/downloads/
    echo     and CHECK "Add python.exe to PATH" in the installer.
    pause
    exit /b 1
)

echo [2/4] Creating venv and installing packages...
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 ( echo [X] venv creation failed. & pause & exit /b 1 )
)
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 ( echo [X] pip install failed. Check internet or proxy. & pause & exit /b 1 )

echo [3/4] Preparing local LLM model...
set "OLLAMA_EXE=ollama"
where ollama >nul 2>nul
if errorlevel 1 (
    if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" (
        set "OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
    ) else (
        echo [i] Ollama not installed - the app still works in template mode.
        echo     To enable LLM mode: run OllamaSetup.exe, then run setup.bat again.
        goto shortcut
    )
)
echo     Downloading qwen3:4b - about 2.5 GB, one time...
"%OLLAMA_EXE%" pull qwen3:4b
if errorlevel 1 echo [!] Model download failed - you can run "ollama pull qwen3:4b" later.

:shortcut
echo [4/4] Creating desktop shortcut...
powershell -NoProfile -Command "$ws = New-Object -ComObject WScript.Shell; $lnk = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'E2E-Assistant.lnk')); $lnk.TargetPath = '%~dp0run.bat'; $lnk.WorkingDirectory = '%~dp0'; $lnk.Description = 'E2E quality review assistant'; $lnk.Save()"
if errorlevel 1 ( echo [!] Shortcut failed - create one manually for run.bat. ) else ( echo     Done: "E2E-Assistant" on the desktop. )

echo.
echo Setup complete. Daily use: double-click "E2E-Assistant" on the desktop.
pause
