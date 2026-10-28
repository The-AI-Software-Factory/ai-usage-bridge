#Requires -Version 5.1
<#
.SYNOPSIS
  Desinstala AI Usage Bridge: detiene y elimina el servicio, borra las entidades
  de Home Assistant (MQTT Discovery), la entrada en "Aplicaciones instaladas" y
  la carpeta de instalacion.
#>
param(
    [string]$InstallDir = (Join-Path $env:ProgramData 'AIUsageBridge'),
    [switch]$KeepEntities,
    [switch]$Unattended
)

$ErrorActionPreference = 'Stop'
$ServiceId = 'AIUsageBridge'

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'Pidiendo permisos de administrador...'
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-InstallDir', "`"$InstallDir`"")
    if ($KeepEntities) { $argList += '-KeepEntities' }
    if ($Unattended) { $argList += '-Unattended' }
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $argList -Wait
    exit
}

$exitCode = 0
try {
    Write-Host 'AI Usage Bridge - desinstalador' -ForegroundColor Green
    if (-not $Unattended) {
        $ans = Read-Host "Se va a eliminar el servicio y la carpeta $InstallDir. Continuar? [S/n]"
        if ($ans -match '^(n|no)$') { exit 0 }
    }

    # 1. Servicio
    $wrapper = Join-Path $InstallDir "$ServiceId.exe"
    $svc = Get-Service -Name $ServiceId -ErrorAction SilentlyContinue
    if ($svc) {
        Write-Host 'Deteniendo y eliminando el servicio...'
        if ($svc.Status -ne 'Stopped') { Stop-Service -Name $ServiceId -Force -ErrorAction SilentlyContinue }
        if (Test-Path $wrapper) { & $wrapper uninstall | Out-Null } else { sc.exe delete $ServiceId | Out-Null }
    }

    # 2. Entidades de Home Assistant (antes de borrar la config)
    $config = Join-Path $InstallDir 'config.toml'
    $xmlPath = Join-Path $InstallDir "$ServiceId.xml"
    if (-not $KeepEntities -and (Test-Path $config) -and (Test-Path $xmlPath)) {
        $python = ([xml](Get-Content $xmlPath -Raw)).service.executable
        if ($python -and (Test-Path $python)) {
            Write-Host 'Borrando las entidades de Home Assistant...'
            $env:PYTHONPATH = (Join-Path $InstallDir 'app') + ';' + (Join-Path $InstallDir 'lib')
            $ErrorActionPreference = 'Continue'
            & $python -m ai_usage_bridge --config $config --remove-discovery 2>$null
            $ErrorActionPreference = 'Stop'
        }
    }

    # 3. Entrada en "Aplicaciones instaladas" y menu Inicio
    $key = "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$ServiceId"
    if (Test-Path $key) { Remove-Item -Path $key -Recurse -Force }
    $menu = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\AI Usage Bridge'
    if (Test-Path $menu) { Remove-Item -Path $menu -Recurse -Force }

    # 4. Carpeta (este script puede estar adentro: se borra al cerrar)
    if (Test-Path $InstallDir) {
        Set-Location $env:TEMP
        Start-Process -FilePath 'cmd.exe' -WindowStyle Hidden -ArgumentList "/c ping -n 4 127.0.0.1 >nul & rmdir /s /q `"$InstallDir`""
    }
    Write-Host 'AI Usage Bridge desinstalado.' -ForegroundColor Green
} catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    $exitCode = 1
}

if (-not $Unattended) { Read-Host 'Enter para cerrar' | Out-Null }
exit $exitCode
