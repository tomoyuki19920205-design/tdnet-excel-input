@echo off
REM ============================================================
REM run_extract_ir_docs.bat — IR文書抽出パイプライン
REM ============================================================
REM 既存の run_update_all.bat とは独立して動作する。
REM .venv の python を使用する。
REM
REM 処理順序:
REM   1. classify_documents — 文書分類
REM   2. fetch_documents    — 文書ダウンロード
REM   3. extract_html       — HTML表抽出
REM   4. extract_pdf        — PDF表抽出
REM   5. normalize          — ファクト正規化
REM ============================================================

cd /d "%~dp0"

SET PYTHON=%~dp0.venv\Scripts\python.exe
IF NOT EXIST "%PYTHON%" (
    echo [ERROR] .venv not found. Run: python -m venv .venv
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  IR文書抽出パイプライン (.venv)
echo ============================================================
echo.

echo [Step 1/5] 文書分類...
"%PYTHON%" tools\classify_documents.py
if errorlevel 1 (
    echo [ERROR] classify_documents failed
    pause
    exit /b 1
)

echo.
echo [Step 2/5] 文書ダウンロード...
"%PYTHON%" tools\fetch_documents.py
if errorlevel 1 (
    echo [ERROR] fetch_documents failed
    pause
    exit /b 1
)

echo.
echo [Step 3/5] HTML抽出...
"%PYTHON%" tools\extract_html.py
if errorlevel 1 (
    echo [ERROR] extract_html failed
    pause
    exit /b 1
)

echo.
echo [Step 4/5] PDF抽出...
"%PYTHON%" tools\extract_pdf.py
if errorlevel 1 (
    echo [ERROR] extract_pdf failed
    pause
    exit /b 1
)

echo.
echo [Step 5/5] ファクト正規化...
"%PYTHON%" tools\normalize_extracted_facts.py
if errorlevel 1 (
    echo [ERROR] normalize failed
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  IR文書抽出パイプライン 完了
echo ============================================================
echo.
