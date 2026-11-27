@echo off
chcp 65001 >nul
title Podesavanje automatskog pokretanja
cd /d "%~dp0"

echo ===================================================
echo  Automatsko pokretanje scrapera svakog sata
echo ===================================================
echo.

set "PYW="
for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"
if not defined PYW (
    echo GRESKA: pythonw.exe nije pronadjen. Da li je Python instaliran?
    pause
    exit /b 1
)
echo Python: %PYW%
echo Folder: %~dp0
echo.

pip install -r requirements.txt --quiet

REM Svaki sat, u 5 minuta posle punog sata, bez prozora (pythonw).
schtasks /create /tn "Stanovi scraper" /tr "\"%PYW%\" \"%~dp0scraper.py\" --local" /sc hourly /mo 1 /st 08:05 /f
if %errorlevel% neq 0 (
    echo GRESKA pri pravljenju zadatka!
    pause
    exit /b 1
)

echo.
echo Pokrecem prvi run odmah (tih - samo pamti postojece oglase)...
schtasks /run /tn "Stanovi scraper"
echo.
echo GOTOVO. Scraper ce raditi svakog sata dok je racunar ukljucen.
echo Log:     %~dp0scraper_log.txt
echo Ukloni:  ukloni_automatski.bat
pause
