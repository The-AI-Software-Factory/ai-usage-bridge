@echo off
rem Desinstala AI Usage Bridge (pide permisos de administrador).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1"
