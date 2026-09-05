@echo off
title Jarvis Installer
echo.
echo  ============================================
echo   J.A.R.V.I.S. - AI Voice Assistant Installer
echo  ============================================
echo.

:: Check for Python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [!] Python not found in PATH.
    echo     Checking common install locations...
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
        set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        echo     Found Python 3.12
    ) else if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
        set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        echo     Found Python 3.11
    ) else if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
        set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
        echo     Found Python 3.13
    ) else (
        echo [ERROR] Python 3.11+ not found. Install from https://python.org
        pause
        exit /b 1
    )
) else (
    set "PYTHON=python"
)

echo.
echo [1/4] Creating virtual environment...
"%PYTHON%" -m venv .venv
if %errorlevel% neq 0 (
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
)

echo [2/4] Activating environment...
call .venv\Scripts\activate.bat

echo [3/4] Installing dependencies (this may take a few minutes)...
pip install -r requirements.txt --quiet
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

echo [4/4] Downloading wake word models...
python -c "from openwakeword import utils; utils.download_models()" 2>nul
python -c "import openwakeword, os, urllib.request; d=os.path.join(os.path.dirname(openwakeword.__file__),'resources','models'); os.makedirs(d,exist_ok=True); [urllib.request.urlretrieve('https://github.com/dscripka/openWakeWord/raw/main/openwakeword/resources/models/'+f, os.path.join(d,f)) for f in ['melspectrogram.onnx','embedding_model.onnx'] if not os.path.exists(os.path.join(d,f))]" 2>nul

echo.
echo  ============================================
echo   Installation complete!
echo  ============================================
echo.
echo  To start Jarvis, run:  start.bat
echo  Or:  .venv\Scripts\activate ^& python -m jarvis.main
echo.
echo  Web UI will open at: http://localhost:7860
echo  Configure providers in Settings tab.
echo.
pause
