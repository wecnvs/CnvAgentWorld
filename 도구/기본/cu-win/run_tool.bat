@echo off
:: computer-use-win 도구 실행 래퍼
:: 사용법: run_tool.bat <script.py> [인수...]
:: 예시:  run_tool.bat screenshot.py C:\Users\user\Desktop\screen.png
::        run_tool.bat click.py 960 540
::        run_tool.bat type_text.py "안녕하세요"

if "%~1"=="" (
    echo 사용법: run_tool.bat ^<script.py^> [인수...]
    exit /b 1
)

set SCRIPT=%~1
shift

:: 나머지 인수 수집
set ARGS=
:collect
if "%~1"=="" goto run
set ARGS=%ARGS% %1
shift
goto collect

:run
python "%~dp0%SCRIPT%"%ARGS%
