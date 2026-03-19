@echo off
REM ============================================================
REM EDINET 大量保有報告書 特定人物監視 (5分間隔タスクスケジューラ用)
REM ============================================================
cd /d "C:\Users\takuy\OneDrive\tdnet-excel-input"
call .venv\Scripts\python.exe scripts\edinet_large_holder_watch.py --once
