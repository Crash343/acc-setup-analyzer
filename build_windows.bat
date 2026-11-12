@echo off
rem Baut ACC-Analyzer.exe lokal. Benötigt einmalig Python 3.10+ (python.org, "Add to PATH" anhaken).
setlocal
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% --version || (echo Python nicht gefunden. Bitte von python.org installieren. & pause & exit /b 1)
%PY% -m venv .venv || (pause & exit /b 1)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install numpy pandas matplotlib markdown pyinstaller || (pause & exit /b 1)
python build_exe.py || (pause & exit /b 1)
echo.
echo Fertig: dist\ACC-Analyzer.exe  (diese Datei kannst du kopieren, sie braucht keine Installation)
pause
