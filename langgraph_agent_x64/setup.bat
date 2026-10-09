@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0.."

set "PYTHON_EXE=%~1"
if "%PYTHON_EXE%"=="" set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
set "VENV_DIR=%CD%\.venv-agent64"

if not exist "%PYTHON_EXE%" (
  echo ERROR: 64-bit Python 3.10+ not found: %PYTHON_EXE%
  echo Usage: langgraph_agent_x64\setup.bat C:\Path\To\Python64\python.exe
  exit /b 1
)

"%PYTHON_EXE%" -c "import struct,sys; raise SystemExit(0 if sys.platform=='win32' and sys.version_info>=(3,10) and struct.calcsize('P')==8 else 2)"
if errorlevel 1 (
  echo ERROR: LangGraph environment requires Windows 64-bit Python 3.10+.
  exit /b 1
)

if exist "%VENV_DIR%" rmdir /s /q "%VENV_DIR%"
"%PYTHON_EXE%" -m venv "%VENV_DIR%"
if errorlevel 1 exit /b 1

"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 exit /b 1
"%VENV_DIR%\Scripts\python.exe" -m pip install --no-cache-dir -r langgraph_agent_x64\requirements.txt
if errorlevel 1 exit /b 1


if not exist ".env.agent" copy /y ".env.agent.example" ".env.agent" >nul

echo LangGraph x64 environment ready: %VENV_DIR%
exit /b 0
