@echo off
rem ==========================================================================
rem  start_bot.bat — start de FP Zero bot met auto-herstart
rem  Gebruik op de VPS: plan deze .bat in Taakplanner "Bij aanmelden"
rem  (de bot heeft zelf een lock-file, dus dubbel starten kan geen kwaad)
rem ==========================================================================
cd /d "%~dp0"
:loop
echo [%date% %time%] Bot starten...
python lbo_bot.py
echo [%date% %time%] Bot gestopt (exitcode %errorlevel%) — herstart over 30s
timeout /t 30 /nobreak >nul
goto loop
