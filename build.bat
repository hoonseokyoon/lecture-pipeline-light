@echo off
setlocal

REM lecture-pipeline GUI exe build script.
REM Produces: dist\lecture-pipeline.exe

cd /d "%~dp0"

python -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
    echo Installing pyinstaller...
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo pip install failed.
        exit /b 1
    )
)

python -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name lecture-pipeline ^
    --clean ^
    --noconfirm ^
    --paths "%~dp0" ^
    --collect-all pypdfium2 ^
    --hidden-import file_order_dialog ^
    gui.py

if errorlevel 1 (
    echo.
    echo Build FAILED.
    exit /b 1
)

echo.
echo ================================================================
echo Build OK: dist\lecture-pipeline.exe
echo.
echo 배포: dist\lecture-pipeline.exe 를 이 폴더 또는 아래 파일들이 있는
echo       폴더로 복사해서 실행하세요.
echo   - skills\               (skill 폴더 — 외부 파일로 유지)
echo   - transcribe_folder.py  (transcribe 버튼이 subprocess로 호출)
echo   - transcribe.py, correct.py
echo   - codex_runner.py 는 .exe에 이미 번들됨
echo.
echo 요구사항: PATH의 python 에 requirements.txt 가 설치되어 있어야 함
echo           (transcribe 버튼의 NotebookLM/Codex 호출에 필요)
echo ================================================================

endlocal
