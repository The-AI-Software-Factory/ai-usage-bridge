@echo off
rem Instala o actualiza AI Usage Bridge (pide permisos de administrador).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -UserProfileDir "%USERPROFILE%" -AppDataDir "%APPDATA%"
