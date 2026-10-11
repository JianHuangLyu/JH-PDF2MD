@echo off
rem Build "dist\JH PDF2MD.exe" with PyInstaller.
rem A clean virtual environment (build\venv) is used so the executable only
rem bundles what the program needs. The whole "build" folder can be deleted.
setlocal
cd /d "%~dp0"

if not exist "build\venv\Scripts\python.exe" (
    python -m venv "build\venv" || goto :error
)
set "PY=build\venv\Scripts\python.exe"

"%PY%" -m pip install --quiet --disable-pip-version-check -r requirements.txt pyinstaller || goto :error
"%PY%" -m PyInstaller --noconfirm --clean --onefile --console ^
    --name "JH PDF2MD" ^
    --distpath dist --workpath "build\work" --specpath build ^
    pdf2md.py || goto :error

echo.
echo Build finished: dist\JH PDF2MD.exe
exit /b 0

:error
echo.
echo Build failed.
exit /b 1
