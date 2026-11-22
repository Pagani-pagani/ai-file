@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM AI File 啟動器 v1
REM 用已簽署的 pythonw.exe 啟動，不會跳黑視窗，也比較不會被 Smart App Control 擋。
where pythonw.exe >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw.exe "%~dp0ai_file_agent.py"
) else (
    echo 找不到 pythonw.exe，改用 python.exe 啟動…
    start "" python.exe "%~dp0ai_file_agent.py"
)
