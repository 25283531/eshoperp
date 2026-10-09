@echo off
REM ---------------------------------------------------------------
REM  ERP dev server launcher (Windows)
REM  Double-click to start the backend on http://127.0.0.1:8000
REM  NOTE: this file is intentionally ASCII-only to avoid encoding
REM        corruption on Windows (GBK codepage).
REM ---------------------------------------------------------------

setlocal

cd /d "%~dp0.."

set PYTHONPATH=.
set APP_ENV=dev
set SCHEDULER_ENABLED=true
set TASK_RECOVERY_ENABLED=true
set ADMIN_TOKEN=admin-token
set OPERATOR_TOKEN=operator-token
set SECRET_KEY=demo-secret-key

set PY=C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe

if not exist "%PY%" (
  echo [ERROR] Python311 not found at:
  echo         %PY%
  echo         Edit PY in this .bat to point at your python.exe
  pause
  exit /b 1
)

echo Checking whether port 8000 is already in use ...
netstat -ano | findstr ":8000" | findstr LISTENING >nul
if %errorlevel%==0 (
  echo.
  echo [ERROR] Port 8000 is already in use.
  echo         A second uvicorn would exit silently on bind conflict.
  echo         Stop the existing instance first, then run this again.
  echo.
  netstat -ano | findstr ":8000" | findstr LISTENING
  pause
  exit /b 1
)

echo.
echo Starting ERP backend on http://127.0.0.1:8000
echo   SCHEDULER_ENABLED=%SCHEDULER_ENABLED%  (required: periodic tasks incl. auto purchase)
echo   DB: %CD%\..\data\erp.db
echo.
echo After it starts, verify you are on the CURRENT code:
echo   curl http://127.0.0.1:8000/api/v1/health
echo   curl http://127.0.0.1:8000/openapi.json   ^(must contain /api/v1/orders/{order_id}/place-purchase^)
echo.

"%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000

echo.
echo [server exited]
pause
endlocal
