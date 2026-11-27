@echo off
chcp 65001 >nul
title Provera scrapera
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

echo ===================================================
echo  PROVERA: da li svi sajtovi rade sa ovog racunara
echo ===================================================
echo.
pip install -r requirements.txt --quiet
echo Pokrecem proveru (traje 1-2 minuta)...
echo.
python scraper.py --provera > provera_rezultat.txt 2>&1
type provera_rezultat.txt | findstr /C:"Provera" /C:"oglasa" /C:"NE RADI" /C:"RADI" /C:"Telegram"
echo.
echo Ceo log je u provera_rezultat.txt
echo Izvestaj je poslat i na Telegram.
pause
