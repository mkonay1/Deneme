@echo off
rem Xray Test Kosum Paneli - Windows baslatici
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo  Python bulunamadi!
  echo  https://www.python.org/downloads/ adresinden Python 3.9+ kurun.
  echo  Kurulumda "Add python.exe to PATH" kutusunu isaretlemeyi unutmayin.
  echo.
  pause
  exit /b 1
)

echo Panel baslatiliyor... Tarayici otomatik acilacak.
echo Jira token'i panel acilinca arayuzde istenecek (her acilista).
python server.py
pause
