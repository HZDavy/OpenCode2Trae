@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 优先用 pythonw：不弹黑窗口，只有界面
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "OpenCode2Trae.py"
    exit /b
)

rem 没找到 pythonw 就退回 python（会带一个黑窗口，方便看报错）
where python >nul 2>nul
if %errorlevel%==0 (
    python "OpenCode2Trae.py"
    if errorlevel 1 pause
    exit /b
)

echo.
echo 没找到 Python，请先安装 Python 3 并勾选 "Add Python to PATH"。
echo.
pause
