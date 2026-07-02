@echo off
:: HUD 오버레이 시작/중지/상태 확인
:: 사용법: overlay_start.bat [start|stop|status]

set TEMP_DIR=%TEMP%
set PID_FILE=%TEMP_DIR%\cu_overlay.pid
set SCRIPT_DIR=%~dp0

set CMD=%1
if "%CMD%"=="" set CMD=status

if "%CMD%"=="start" goto :start
if "%CMD%"=="stop"  goto :stop
if "%CMD%"=="status" goto :status
echo 사용법: overlay_start.bat [start^|stop^|status]
exit /b 1

:start
if exist "%PID_FILE%" (
    set /p PID=<"%PID_FILE%"
    tasklist /fi "PID eq %PID%" /fo csv 2>nul | find /i "python" >nul
    if not errorlevel 1 (
        echo 오버레이 이미 실행 중 (PID %PID%)
        exit /b 0
    )
)
start "" /b pythonw "%SCRIPT_DIR%overlay.py"
timeout /t 1 /nobreak >nul
if exist "%PID_FILE%" (
    set /p PID=<"%PID_FILE%"
    echo 오버레이 시작됨 (PID %PID%)
) else (
    echo 오버레이 시작됨
)
exit /b 0

:stop
if not exist "%PID_FILE%" (
    echo 오버레이 실행 중이지 않음
    exit /b 0
)
set /p PID=<"%PID_FILE%"
taskkill /pid %PID% /f >nul 2>&1
del "%PID_FILE%" 2>nul
echo 오버레이 종료됨
exit /b 0

:status
if not exist "%PID_FILE%" (
    echo 오버레이 실행 중이지 않음
    exit /b 1
)
set /p PID=<"%PID_FILE%"
tasklist /fi "PID eq %PID%" /fo csv 2>nul | find /i "python" >nul
if not errorlevel 1 (
    echo 오버레이 실행 중 (PID %PID%)
    exit /b 0
) else (
    del "%PID_FILE%" 2>nul
    echo 오버레이 실행 중이지 않음 (PID 파일 정리됨)
    exit /b 1
)
