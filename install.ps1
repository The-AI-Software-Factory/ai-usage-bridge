#Requires -Version 5.1
<#
.SYNOPSIS
  Instala (o actualiza) AI Usage Bridge como servicio de Windows.
.DESCRIPTION
  Lo usan el zip (install.cmd), el Setup.exe y el MSI.
  - Usa el Python embebido del paquete si existe; si no (repo clonado), busca
    Python 3.11+ e instala las dependencias.
  - Copia la app a %ProgramData%\AIUsageBridge.
  - Crea config.toml si no existe (si existe, lo conserva) y configura MQTT.
  - Registra el servicio "AIUsageBridge" (WinSW) con inicio automatico.
  - Crea accesos directos en el menu Inicio y la entrada en "Aplicaciones instaladas".
#>
param(
    [string]$UserProfileDir = $env:USERPROFILE,
    [string]$AppDataDir = $env:APPDATA,
    [string]$InstallDir = (Join-Path $env:ProgramData 'AIUsageBridge'),
    # Opcionales: datos del broker para instalar sin preguntas.
    [string]$MqttHost,
    [string]$MqttPort,
    [string]$MqttUser,
    [string]$MqttPassword,
    [string]$MqttPasswordFile,
    # "true"/"false": renovar automaticamente el token de Claude Code.
    [string]$ClaudeRefresh,
    # El MSI registra su propia entrada en "Aplicaciones instaladas".
    [switch]$NoArpEntry,
    [switch]$Unattended
)

$ErrorActionPreference = 'Stop'
$ServiceId = 'AIUsageBridge'
$Version = '1.0.2'
$WinSWUrl = 'https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe'
$WinSWSha256 = '05B82D46AD331CC16BDC00DE5C6332C1EF818DF8CEEFCD49C726553209B3A0DA'
$Src = $PSScriptRoot
$exitCode = 0

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# Se relanza como administrador conservando los parametros (y las rutas del usuario).
if (-not (Test-Admin)) {
    Write-Host 'Pidiendo permisos de administrador...'
    $PSBoundParameters['UserProfileDir'] = $UserProfileDir
    $PSBoundParameters['AppDataDir'] = $AppDataDir
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"")
    foreach ($p in $PSBoundParameters.GetEnumerator()) {
        if ($p.Value -is [switch]) { if ($p.Value) { $argList += "-$($p.Key)" } }
        else { $argList += @("-$($p.Key)", "`"$($p.Value)`"") }
    }
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $argList -Wait
    exit
}

# El MSI antepone "=" a cada valor para no pasar nunca argumentos vacios.
foreach ($name in @('MqttHost', 'MqttPort', 'MqttUser', 'MqttPassword', 'ClaudeRefresh')) {
    $value = Get-Variable -Name $name -ValueOnly
    if ($value -and $value.StartsWith('=')) { Set-Variable -Name $name -Value $value.Substring(1) }
}

# Normaliza rutas ("...\Roaming\." -> "...\Roaming").
$AppDataDir = [IO.Path]::GetFullPath($AppDataDir).TrimEnd('\')
# Si solo llega AppData (caso MSI, que corre como SYSTEM), el perfil es AppData\Roaming\..\..
if ($PSBoundParameters.ContainsKey('AppDataDir') -and -not $PSBoundParameters.ContainsKey('UserProfileDir')) {
    $UserProfileDir = Split-Path (Split-Path $AppDataDir -Parent) -Parent
}
$UserProfileDir = [IO.Path]::GetFullPath($UserProfileDir).TrimEnd('\')

function Write-Step([string]$Text) { Write-Host "`n==> $Text" -ForegroundColor Cyan }

# Python escribe en UTF-8; asi PowerShell muestra bien los acentos.
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

function Find-Python {
    $candidates = @()
    if (Get-Command py.exe -ErrorAction SilentlyContinue) { $candidates += , @('py.exe', '-3') }
    if (Test-Path "$env:windir\py.exe") { $candidates += , @("$env:windir\py.exe", '-3') }
    if (Get-Command python.exe -ErrorAction SilentlyContinue) { $candidates += , @('python.exe') }
    $dirs = @(Get-ChildItem "$env:ProgramFiles\Python3*", "$UserProfileDir\AppData\Local\Programs\Python\Python3*" -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending)
    foreach ($d in $dirs) { if (Test-Path "$($d.FullName)\python.exe") { $candidates += , @("$($d.FullName)\python.exe") } }
    foreach ($c in $candidates) {
        $exe = $c[0]
        $pre = @($c | Select-Object -Skip 1)
        try {
            $out = & $exe @pre -c "import sys;print(sys.executable);print(int(sys.version_info[:2]>=(3,11)))" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out.Count -ge 2 -and "$($out[1])".Trim() -eq '1') {
                $path = "$($out[0])".Trim()
                # El alias de Microsoft Store no funciona para servicios.
                if ($path -notmatch 'WindowsApps') { return $path }
            }
        } catch { }
    }
    return $null
}

function Remove-ExistingService {
    $svc = Get-Service -Name $ServiceId -ErrorAction SilentlyContinue
    if (-not $svc) { return }
    Write-Step 'Deteniendo el servicio existente (actualizacion)'
    if ($svc.Status -ne 'Stopped') { Stop-Service -Name $ServiceId -Force -ErrorAction SilentlyContinue }
    $wrapper = Join-Path $InstallDir "$ServiceId.exe"
    if (Test-Path $wrapper) { & $wrapper uninstall | Out-Null } else { sc.exe delete $ServiceId | Out-Null }
    for ($i = 0; $i -lt 20 -and (Get-Service -Name $ServiceId -ErrorAction SilentlyContinue); $i++) { Start-Sleep -Milliseconds 500 }
}

function New-Shortcut([string]$Path, [string]$Target, [string]$Arguments, [string]$Description) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($Path)
    $lnk.TargetPath = $Target
    if ($Arguments) { $lnk.Arguments = $Arguments }
    $lnk.Description = $Description
    $lnk.WorkingDirectory = $InstallDir
    $lnk.Save()
}

New-Item -ItemType Directory -Force -Path $InstallDir, (Join-Path $InstallDir 'logs') | Out-Null
try { Start-Transcript -Path (Join-Path $InstallDir 'logs\install.log') -Force | Out-Null } catch { }

try {
    Write-Host "AI Usage Bridge $Version - instalador" -ForegroundColor Green
    Write-Host "Perfil de usuario: $UserProfileDir"
    Write-Host "Destino:           $InstallDir"

    Remove-ExistingService

    # 1. Archivos de la app --------------------------------------------------
    Write-Step 'Copiando archivos'
    foreach ($dir in @('app', 'lib', 'python')) {
        $target = Join-Path $InstallDir $dir
        if (Test-Path $target) { Remove-Item $target -Recurse -Force }
    }
    Copy-Item (Join-Path $Src 'app') (Join-Path $InstallDir 'app') -Recurse
    foreach ($f in @('requirements.txt', 'uninstall.ps1', 'uninstall.cmd', 'configure.ps1', 'configure.cmd',
                     'README.md', 'README.es.md', 'LICENSE', 'config.template.toml')) {
        $path = Join-Path $Src $f
        if (Test-Path $path) { Copy-Item $path $InstallDir -Force }
    }

    # 2. Python y dependencias -----------------------------------------------
    $bundledPython = Join-Path $Src 'python\python.exe'
    if ((Test-Path $bundledPython) -and (Test-Path (Join-Path $Src 'lib'))) {
        Write-Step 'Usando el Python embebido del paquete'
        Copy-Item (Join-Path $Src 'python') (Join-Path $InstallDir 'python') -Recurse
        Copy-Item (Join-Path $Src 'lib') (Join-Path $InstallDir 'lib') -Recurse
        $python = Join-Path $InstallDir 'python\python.exe'
    } else {
        Write-Step 'Buscando Python 3.11 o superior'
        $python = Find-Python
        if (-not $python) {
            if ($Unattended) { throw 'No se encontro Python 3.11+. Usa el Setup.exe o el MSI, que ya lo incluyen.' }
            $ans = Read-Host 'No encontre Python 3.11+. Lo instalo con winget (Python 3.12, para todos los usuarios)? [S/n]'
            if ($ans -match '^(n|no)$') { throw 'Python 3.11 o superior es necesario.' }
            winget install -e --id Python.Python.3.12 --scope machine --accept-package-agreements --accept-source-agreements
            $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
            $python = Find-Python
            if (-not $python) { throw 'No se pudo encontrar Python despues de instalarlo.' }
        }
        Write-Host "Python: $python"
        Write-Step 'Instalando dependencias'
        & $python -m pip install --disable-pip-version-check --no-warn-script-location -q `
            --target (Join-Path $InstallDir 'lib') -r (Join-Path $InstallDir 'requirements.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Fallo la instalacion de dependencias con pip.' }
    }

    # 3. WinSW (envoltorio de servicio) -------------------------------------
    Write-Step 'Preparando el envoltorio de servicio (WinSW)'
    $wrapper = Join-Path $InstallDir "$ServiceId.exe"
    $bundledWinSW = Join-Path $Src 'winsw\WinSW-x64.exe'
    if (Test-Path $bundledWinSW) {
        Copy-Item $bundledWinSW $wrapper -Force
    } else {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Write-Host "Descargando $WinSWUrl"
        Invoke-WebRequest -Uri $WinSWUrl -OutFile $wrapper -UseBasicParsing
    }
    $hash = (Get-FileHash $wrapper -Algorithm SHA256).Hash
    if ($hash -ne $WinSWSha256) { Remove-Item $wrapper -Force; throw "El hash de WinSW no coincide ($hash)." }

    # 4. Configuracion -------------------------------------------------------
    $config = Join-Path $InstallDir 'config.toml'
    if (-not (Test-Path $config)) {
        Write-Step 'Creando config.toml'
        $text = [IO.File]::ReadAllText((Join-Path $Src 'config.template.toml'))
        $text = $text.Replace('__USERPROFILE__', $UserProfileDir).Replace('__APPDATA__', $AppDataDir)
        [IO.File]::WriteAllText($config, $text, (New-Object Text.UTF8Encoding($false)))
    } else {
        Write-Step 'Conservando config.toml existente'
    }
    # Solo SYSTEM y Administradores pueden leer la config (tiene la contrasena de MQTT).
    icacls $config /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null

    $mqttArgs = @{ InstallDir = $InstallDir; PythonExe = $python; NoRestart = $true; Unattended = $true }
    foreach ($k in @('MqttHost', 'MqttPort', 'MqttUser', 'MqttPassword', 'MqttPasswordFile', 'ClaudeRefresh')) {
        $value = Get-Variable -Name $k -ValueOnly
        if ("$value" -ne '') { $mqttArgs[$k] = $value }
    }
    $hasHost = [regex]::IsMatch([IO.File]::ReadAllText($config), '(?m)^\s*host\s*=\s*"[^"]+"')
    if ($mqttArgs.ContainsKey('MqttHost') -or $mqttArgs.ContainsKey('MqttPasswordFile') -or $mqttArgs.ContainsKey('ClaudeRefresh')) {
        Write-Step 'Guardando los datos del broker MQTT'
        & (Join-Path $InstallDir 'configure.ps1') @mqttArgs
    } elseif (-not $hasHost -and -not $Unattended) {
        Write-Step 'Datos del broker MQTT'
        $mqttArgs.Remove('Unattended')
        & (Join-Path $InstallDir 'configure.ps1') @mqttArgs -NoPause
    }
    $ErrorActionPreference = 'Stop'

    # 5. Prueba de lectura ---------------------------------------------------
    $env:PYTHONPATH = (Join-Path $InstallDir 'app') + ';' + (Join-Path $InstallDir 'lib')
    $env:PYTHONUTF8 = '1'
    Write-Step 'Probando lectura de Claude, Codex y Cursor'
    $ErrorActionPreference = 'Continue'
    $json = & $python -m ai_usage_bridge --config $config --once 2>$null | Out-String
    $ErrorActionPreference = 'Stop'
    try {
        $result = $json | ConvertFrom-Json
        foreach ($k in @('claude', 'codex', 'cursor')) {
            $s = $result.$k
            if ($null -eq $s) { continue }
            $color = if ($s.status -eq 'ok') { 'Green' } else { 'Yellow' }
            $err = if ($s.status -ne 'ok') { " - $($s.attributes.error)" } else { '' }
            $margin = if ($null -ne $s.margin) { "$($s.margin)%" } else { '-' }
            Write-Host ("  {0,-7} {1,-16} margen: {2}{3}" -f $k, $s.status, $margin, $err) -ForegroundColor $color
        }
        Write-Host ("  Mejor destino ahora: {0}" -f $result.summary.best)
    } catch {
        Write-Warning 'No pude interpretar la prueba; revisa los logs del servicio.'
    }

    # 6. Servicio ------------------------------------------------------------
    Write-Step 'Registrando el servicio'
    $app = Join-Path $InstallDir 'app'
    $lib = Join-Path $InstallDir 'lib'
    $logs = Join-Path $InstallDir 'logs'
    $xml = @"
<service>
  <id>$ServiceId</id>
  <name>AI Usage Bridge</name>
  <description>Publica en MQTT (Home Assistant) el uso de Claude, Codex y Cursor.</description>
  <executable>$python</executable>
  <arguments>-u -m ai_usage_bridge --config "$config"</arguments>
  <workingdirectory>$app</workingdirectory>
  <env name="PYTHONPATH" value="$app;$lib" />
  <env name="PYTHONUTF8" value="1" />
  <env name="PYTHONUNBUFFERED" value="1" />
  <startmode>Automatic</startmode>
  <delayedAutoStart>true</delayedAutoStart>
  <onfailure action="restart" delay="30 sec" />
  <onfailure action="restart" delay="60 sec" />
  <resetfailure>1 hour</resetfailure>
  <stoptimeout>15 sec</stoptimeout>
  <logpath>$logs</logpath>
  <log mode="roll-by-size">
    <sizeThreshold>1024</sizeThreshold>
    <keepFiles>5</keepFiles>
  </log>
</service>
"@
    [IO.File]::WriteAllText((Join-Path $InstallDir "$ServiceId.xml"), $xml, (New-Object Text.UTF8Encoding($false)))
    & $wrapper install
    if ($LASTEXITCODE -ne 0) { throw 'WinSW no pudo registrar el servicio.' }
    & $wrapper start
    Start-Sleep -Seconds 3
    $svc = Get-Service -Name $ServiceId
    Write-Host "Servicio: $($svc.Status)"

    # 7. Menu Inicio ---------------------------------------------------------
    $menu = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\AI Usage Bridge'
    New-Item -ItemType Directory -Force -Path $menu | Out-Null
    New-Shortcut (Join-Path $menu 'Configurar AI Usage Bridge.lnk') (Join-Path $InstallDir 'configure.cmd') '' 'Datos del broker MQTT'
    New-Shortcut (Join-Path $menu 'Logs de AI Usage Bridge.lnk') $logs '' 'Carpeta de logs del servicio'

    # 8. Entrada en "Aplicaciones instaladas" -------------------------------
    if (-not $NoArpEntry) {
        $key = "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$ServiceId"
        New-Item -Path $key -Force | Out-Null
        $uninstall = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $InstallDir 'uninstall.ps1')`""
        $props = @{
            DisplayName = 'AI Usage Bridge'; DisplayVersion = $Version; Publisher = 'The AI Software Factory'
            InstallLocation = $InstallDir; UninstallString = $uninstall
            URLInfoAbout = 'https://github.com/The-AI-Software-Factory/ai-usage-bridge'
        }
        foreach ($p in $props.GetEnumerator()) { Set-ItemProperty -Path $key -Name $p.Key -Value $p.Value }
        Set-ItemProperty -Path $key -Name NoModify -Value 1 -Type DWord
        Set-ItemProperty -Path $key -Name NoRepair -Value 1 -Type DWord
    }

    Write-Host "`nListo. Config: $config  |  Logs: $logs" -ForegroundColor Green
    if (-not [regex]::IsMatch([IO.File]::ReadAllText($config), '(?m)^\s*host\s*=\s*"[^"]+"')) {
        Write-Host 'Falta configurar el broker MQTT: menu Inicio > "Configurar AI Usage Bridge".' -ForegroundColor Yellow
    } else {
        Write-Host 'En Home Assistant van a aparecer los dispositivos "AI Claude", "AI Codex", "AI Cursor" y "AI Resumen".'
    }
} catch {
    Write-Host "`nERROR: $($_.Exception.Message)" -ForegroundColor Red
    $exitCode = 1
}

try { Stop-Transcript | Out-Null } catch { }
if (-not $Unattended) { Read-Host "`nEnter para cerrar" | Out-Null }
exit $exitCode
