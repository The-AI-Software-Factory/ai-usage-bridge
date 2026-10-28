@echo off
rem Configura los datos del broker MQTT de AI Usage Bridge (pide permisos de administrador).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0configure.ps1"
