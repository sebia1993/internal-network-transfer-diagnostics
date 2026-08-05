@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Python 가상 환경을 준비합니다...
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -m venv .venv
    ) else (
        where python >nul 2>nul
        if errorlevel 1 (
            echo Python 3를 찾을 수 없습니다. Python 3 설치 후 다시 실행하세요.
            pause
            exit /b 2
        )
        python -m venv .venv
    )
    if errorlevel 1 (
        echo Python 가상 환경을 만들지 못했습니다.
        pause
        exit /b 2
    )
    if not exist ".venv\Scripts\python.exe" (
        echo Python 가상 환경이 올바르게 생성되지 않았습니다.
        pause
        exit /b 2
    )
)

echo 필요한 라이브러리를 설치합니다...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo 라이브러리 설치에 실패했습니다.
    pause
    exit /b 1
)

echo 사내 업로드 서버를 시작합니다...
".venv\Scripts\python.exe" app.py
pause
