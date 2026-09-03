@echo off
setlocal EnableDelayedExpansion
title Cloud Crucix Community - start

rem ============================================================
rem  Cloud Crucix Community Edition - one click to start.
rem
rem  This file is plain text; open it in Notepad to read every
rem  step first. It does four things:
rem
rem   1) Makes sure a  secrets  folder exists next to this file.
rem      Your Google service-account JSON key goes in there.
rem   2) Builds the Docker image if it is not built yet.
rem   3) Finds a free port.
rem   4) Starts the dashboard and opens your browser.
rem
rem  Nothing is uploaded anywhere. Everything runs on THIS computer.
rem ============================================================

cd /d "%~dp0"
set IMAGE=cloud-crucix-community:local

rem ============================================================
rem  STEP 1 - Secrets folder (your credentials)
rem ============================================================
if not exist "secrets" mkdir "secrets"
if not exist "secrets\*.json" (
    echo.
    echo  ------------------------------------------------------------------
    echo   Heads up: no Google service-account key file found.
    echo   It should be a .json file inside this folder:
    echo       %CD%\secrets
    echo   ^(Ask your IT/cloud team - it only needs READ access to BigQuery.^)
    echo.
    echo   Press ENTER to continue anyway: the page will open, but it
    echo   will not be able to read BigQuery without a key.
    echo  ------------------------------------------------------------------
    pause >nul
)

rem ============================================================
rem  STEP 2 - Docker image
rem ============================================================
echo.
echo  [1/4] Checking Docker...
docker version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  ERROR: Docker is not running or not installed.
    echo  Start Docker Desktop, wait until it is ready, then run this again.
    echo.
    pause
    exit /b 1
)

echo  [2/4] Building the image ^(first time takes a minute^)...
docker build -t %IMAGE% "%~dp0."
if errorlevel 1 (
    echo.
    echo  ERROR: docker build failed. See the output above.
    echo.
    pause
    exit /b 1
)

rem ============================================================
rem  STEP 3 - Find a free port
rem ============================================================
set PORT=5006
set MAXPORT=5106

docker ps --format "{{.Ports}}" > "%TEMP%\crucix-ports.txt" 2>nul

:findport
netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 goto portbusy
findstr /C:":%PORT%->" "%TEMP%\crucix-ports.txt" >nul 2>&1
if not errorlevel 1 goto portbusy
goto portfound

:portbusy
set /a PORT+=1
if %PORT% gtr %MAXPORT% (
    echo.
    echo  ERROR: no free port between 5006 and %MAXPORT%.
    echo  Stop something that is using them, then try again.
    echo.
    del "%TEMP%\crucix-ports.txt" >nul 2>&1
    pause
    exit /b 1
)
goto findport

:portfound
del "%TEMP%\crucix-ports.txt" >nul 2>&1
echo  [3/4] Using port %PORT% ...

set NAME=cloud-crucix-community-%PORT%
docker rm -f %NAME% >nul 2>&1

rem ============================================================
rem  STEP 4 - Start it and open the browser
rem ============================================================
echo  [4/4] Starting Cloud Crucix Community...
echo.
echo  ------------------------------------------------------------------
echo   Your browser will open:   http://localhost:%PORT%
echo.
echo   This window shows which service account it authenticated as.
echo   Keep it open while you use the dashboard; Ctrl+C stops it.
echo.
echo   This is the Community Edition with the Activity tab.
echo   Upgrade to Full Edition for workload, cost, and findings.
echo  ------------------------------------------------------------------
echo.

start "" "http://localhost:%PORT%"

docker run --rm --name %NAME% -e PORT=%PORT% ^
       -v "%~dp0secrets:/app/secrets" ^
       -p 127.0.0.1:%PORT%:%PORT% ^
       %IMAGE%

echo.
echo  Cloud Crucix Community has stopped. You can close this window.
pause
