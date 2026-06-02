@echo off
setlocal

REM Build Windows executable with PyInstaller
python -m pip install --upgrade pip
python -m pip install -r requirements.txt -r requirements-build.txt

pyinstaller --noconfirm --clean --name VSR-annotation ^
  --add-data "static;static" ^
  --add-data "config.json;." ^
  --add-data "persons.json;." ^
  --add-data "video_index.json;." ^
  app.py

echo.
echo Build complete. Copy dist\VSR-annotation\* to your target machine.
echo Keep videos\ and annotations\ as external folders next to the exe.
echo.
endlocal
