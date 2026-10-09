@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0.."

set "PYTHON_EXE=%~1"
if "%PYTHON_EXE%"=="" set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python310-32\python.exe"
set "VENV_DIR=%CD%\.venv-adk32"

if not exist "%PYTHON_EXE%" (
  echo ERROR: 32-bit Python 3.10 not found: %PYTHON_EXE%
  echo Usage: adk_gateway_x86\setup.bat C:\Path\To\Python310-32\python.exe
  exit /b 1
)

"%PYTHON_EXE%" -c "import struct,sys; raise SystemExit(0 if sys.platform=='win32' and sys.version_info[:2]==(3,10) and struct.calcsize('P')==4 else 2)"
if errorlevel 1 (
  echo ERROR: ADK environment requires Windows 32-bit CPython 3.10.
  exit /b 1
)

if exist "%VENV_DIR%" rmdir /s /q "%VENV_DIR%"
"%PYTHON_EXE%" -m venv "%VENV_DIR%"
if errorlevel 1 exit /b 1

"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 exit /b 1

rem Binary-only prevents accidental C/Rust source builds on win32.
"%VENV_DIR%\Scripts\python.exe" -m pip install --no-cache-dir --only-binary=:all: -r adk_gateway_x86\requirements.txt
if errorlevel 1 exit /b 1


if not exist ".env.gateway" copy /y ".env.gateway.example" ".env.gateway" >nul

echo ADK x86 environment ready: %VENV_DIR%
exit /b 0
