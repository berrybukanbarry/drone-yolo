@echo off
rem Klik dua kali file ini untuk membuka Panel Drone di browser.
rem Tutup jendela ini untuk mematikan panel dan detektor.
cd /d "%~dp0"
title Panel Drone YOLO

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

echo Menyalakan Panel Drone...
echo Jangan tutup jendela ini selama panel dipakai.
echo.
"%PY%" control_panel.py --open-browser
echo.
echo Panel berhenti. Jika ada error, lihat pesan di atas atau README.md bagian Masalah umum.
pause
