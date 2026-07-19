@echo off
title Stanovi - Telegram listener (/svi komande)
cd /d "%~dp0"

echo ===================================
echo  Telegram listener za /svi komande
echo ===================================
echo.
echo Ovaj prozor treba da ostane otvoren (ili minimiziran) da bi
echo /svi i /svi^<broj^> komande radile u Telegramu.
echo Zatvori prozor ili pritisni Ctrl+C da zaustavis.
echo.

:loop
python scraper.py --listen
echo.
echo [%date% %time%] Listener je stao, restartujem za 10 sekundi...
timeout /t 10 /nobreak >nul
goto loop
