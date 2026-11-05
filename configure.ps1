#Requires -Version 5.1
<#
.SYNOPSIS
  Configura los datos del broker MQTT de AI Usage Bridge y reinicia el servicio.
.DESCRIPTION
  Sin parametros, pregunta host, puerto, usuario y contrasena (mostrando los
  valores actuales). Con -Unattended usa solo los parametros recibidos.
  Lo usan el instalador y el acceso directo "Configurar AI Usage Bridge".
#>
param(
    [string]$InstallDir = (Join-Path $env:ProgramData 'AIUsageBridge'),
    [string]$MqttHost,
    [string]$MqttPort,
    [string]$MqttUser,
    [string]$MqttPassword,
    # Archivo con la contrasena (se borra despues de leerlo); evita pasarla por linea de comandos.
    [string]$MqttPasswordFile,
    # "true"/"false": renovar automaticamente el token de Claude Code.
    [string]$ClaudeRefresh,
    # URL que abre la app de bandeja al hacer clic ("-" para borrarla).
    [string]$DashboardUrl,
    [string]$PythonExe,
    [switch]$NoRestart,
    [switch]$NoPause,
    [switch]$Unattended
)

$ErrorActionPreference = 'Stop'
$ServiceId = 'AIUsageBridge'

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"")
    foreach ($p in $PSBoundParameters.GetEnumerator()) {
        if ($p.Value -is [switch]) { if ($p.Value) { $argList += "-$($p.Key)" } }
        else { $argList += @("-$($p.Key)", "`"$($p.Value)`"") }
    }
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $argList -Wait
    exit
}

try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

function ConvertTo-TomlString([string]$Value) { return $Value.Replace('\', '\\').Replace('"', '\"') }

function Get-TomlValue([string]$Text, [string]$Key) {
    $m = [regex]::Match($Text, "(?m)^\s*$Key\s*=\s*""((?:[^""\\]|\\.)*)""")
    if ($m.Success) { return $m.Groups[1].Value.Replace('\"', '"').Replace('\\', '\') }
    $m = [regex]::Match($Text, "(?m)^\s*$Key\s*=\s*(\d+|true|false)")
    if ($m.Success) { return $m.Groups[1].Value }
    return ''
}

# Agrega la seccion [tray] a configuraciones de versiones anteriores.
function Add-TraySection([string]$Text) {
    if ([regex]::IsMatch($Text, '(?m)^\s*dashboard_url\s*=')) { return $Text }
    $block = "[tray]`r`nenabled = true`r`nshow = [""claude"", ""codex"", ""cursor""]`r`ndashboard_url = """""
    $header = [regex]'(?m)^[ \t]*\[tray\][ \t]*\r?$'
    if ($header.IsMatch($Text)) {
        # La seccion existe sin dashboard_url: solo se agrega esa clave.
        return $header.Replace($Text, "[tray]`r`ndashboard_url = """"", 1)
    }
    return $Text.TrimEnd() + "`r`n`r`n" + $block + "`r`n"
}

function Set-TomlLine([string]$Text, [string]$Key, [string]$Literal) {
    $regex = [regex]"(?m)^(\s*$Key\s*=\s*)[^\r\n]*"
    if (-not $regex.IsMatch($Text)) { throw "No encontre la clave '$Key' en config.toml" }
    $evaluator = [Text.RegularExpressions.MatchEvaluator] ({ param($m) $m.Groups[1].Value + $Literal }.GetNewClosure())
    return $regex.Replace($Text, $evaluator, 1)
}

function Read-Value([string]$Prompt, [string]$Current, [switch]$Required) {
    while ($true) {
        $label = if ($Current) { "$Prompt [$Current]" } else { $Prompt }
        $value = Read-Host $label
        if (-not [string]::IsNullOrWhiteSpace($value)) { return $value.Trim() }
        if ($Current -or -not $Required) { return $Current }
        Write-Host '  Este dato es obligatorio.' -ForegroundColor Yellow
    }
}

$exitCode = 0
try {
    $config = Join-Path $InstallDir 'config.toml'
    if (-not (Test-Path $config)) { throw "No existe $config. Instala AI Usage Bridge primero." }
    $text = [IO.File]::ReadAllText($config)

    $curHost = Get-TomlValue $text 'host'
    $curPort = Get-TomlValue $text 'port'
    $curUser = Get-TomlValue $text 'username'
    $curPass = Get-TomlValue $text 'password'

    if (-not $Unattended) {
        Write-Host 'AI Usage Bridge - configuracion' -ForegroundColor Green
        Write-Host 'Enter mantiene el valor actual.'
    }

    $newHost = if ($PSBoundParameters.ContainsKey('MqttHost')) { $MqttHost.Trim() }
               elseif ($Unattended) { $curHost }
               else { Read-Value 'Host o IP del broker (ej. 192.168.1.10)' $curHost -Required }
    $newPort = if ($MqttPort) { $MqttPort.Trim() }
               elseif ($Unattended) { $curPort }
               else { Read-Value 'Puerto' $(if ($curPort) { $curPort } else { '1883' }) }
    if (-not $newPort) { $newPort = '1883' }
    if ($newPort -notmatch '^\d{1,5}$') { throw "Puerto invalido: $newPort" }
    $newUser = if ($PSBoundParameters.ContainsKey('MqttUser')) { $MqttUser.Trim() }
               elseif ($Unattended) { $curUser }
               else { Read-Value 'Usuario (vacio si el broker es anonimo; "-" para borrarlo)' $curUser }
    if ($newUser -eq '-') { $newUser = '' }

    if ($MqttPasswordFile -and (Test-Path $MqttPasswordFile)) {
        $newPass = ([IO.File]::ReadAllText($MqttPasswordFile)).TrimEnd("`r", "`n").TrimStart([char]0xFEFF)
        Remove-Item $MqttPasswordFile -Force
    } elseif ($PSBoundParameters.ContainsKey('MqttPassword')) {
        $newPass = $MqttPassword
    } elseif ($Unattended -or -not $newUser) {
        $newPass = if ($newUser) { $curPass } else { '' }
    } else {
        $hint = if ($curPass) { ' (Enter mantiene la actual)' } else { '' }
        $secure = Read-Host "Contrasena$hint" -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $typed = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
        $newPass = if ($typed) { $typed } else { $curPass }
    }

    # Renovacion del token de Claude Code
    $curRefresh = (Get-TomlValue $text 'refresh_token_enabled') -eq 'true'
    if ($ClaudeRefresh) {
        $newRefresh = $ClaudeRefresh.Trim().TrimStart('=') -match '^(1|true|yes|si|s)$'
    } elseif ($Unattended) {
        $newRefresh = $curRefresh
    } else {
        Write-Host ''
        Write-Host 'Claude: el token que se lee lo renueva Claude Code (terminal) solo cuando lo usas.'
        Write-Host 'Si usas Claude desde la app de escritorio o la web, conviene que lo renueve el servicio.'
        $def = if ($curRefresh) { 'S' } else { 'N' }
        $ans = Read-Host "Renovar automaticamente el token de Claude? (s/n) [$def]"
        $newRefresh = if ([string]::IsNullOrWhiteSpace($ans)) { $curRefresh } else { $ans.Trim() -match '^(s|si|y|yes|1|true)$' }
    }

    # URL del dashboard para la app de bandeja
    $text = Add-TraySection $text
    $curUrl = Get-TomlValue $text 'dashboard_url'
    if ($PSBoundParameters.ContainsKey('DashboardUrl')) {
        $newUrl = $DashboardUrl.Trim().TrimStart('=')
    } elseif ($Unattended) {
        $newUrl = $curUrl
    } else {
        $suggest = if ($curUrl) { $curUrl } elseif ($newHost) { "http://$($newHost):8123/" } else { '' }
        Write-Host ''
        Write-Host 'Bandeja: al hacer clic en un icono se abre esta URL (por ejemplo, tu dashboard de Home Assistant).'
        $newUrl = Read-Value 'URL del dashboard (vacio = sugerida; "-" = ninguna)' $suggest
    }
    if ($newUrl -eq '-') { $newUrl = '' }
    if ($newUrl -and $newUrl -notmatch '^https?://') { throw "La URL del dashboard tiene que empezar con http:// o https://" }
    $text = Set-TomlLine $text 'dashboard_url' ('"' + (ConvertTo-TomlString $newUrl) + '"')

    $text = Set-TomlLine $text 'refresh_token_enabled' $(if ($newRefresh) { 'true' } else { 'false' })
    $text = Set-TomlLine $text 'host' ('"' + (ConvertTo-TomlString $newHost) + '"')
    $text = Set-TomlLine $text 'port' ([string][int]$newPort)
    $text = Set-TomlLine $text 'username' ('"' + (ConvertTo-TomlString $newUser) + '"')
    $text = Set-TomlLine $text 'password' ('"' + (ConvertTo-TomlString $newPass) + '"')
    [IO.File]::WriteAllText($config, $text, (New-Object Text.UTF8Encoding($false)))
    $newPass = $null; $curPass = $null
    Write-Host 'Configuracion guardada.'

    # Prueba de conexion
    if (-not $PythonExe) {
        $bundled = Join-Path $InstallDir 'python\python.exe'
        $xmlPath = Join-Path $InstallDir "$ServiceId.xml"
        if (Test-Path $bundled) { $PythonExe = $bundled }
        elseif (Test-Path $xmlPath) { $PythonExe = ([xml](Get-Content $xmlPath -Raw)).service.executable }
    }
    if ($newHost -and $PythonExe -and (Test-Path $PythonExe)) {
        $env:PYTHONPATH = (Join-Path $InstallDir 'app') + ';' + (Join-Path $InstallDir 'lib')
        $ErrorActionPreference = 'Continue'
        $result = & $PythonExe -m ai_usage_bridge --config $config --test-mqtt 2>$null
        $ok = ($LASTEXITCODE -eq 0)
        $ErrorActionPreference = 'Stop'
        if ($ok) { Write-Host "Prueba de conexion: $result" -ForegroundColor Green }
        else { Write-Warning "Prueba de conexion: $result" }
    }

    if (-not $NoRestart -and (Get-Service -Name $ServiceId -ErrorAction SilentlyContinue)) {
        Restart-Service -Name $ServiceId -Force
        Write-Host 'Servicio reiniciado.'
    }
} catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    $exitCode = 1
}

if (-not $Unattended -and -not $NoPause) { Read-Host 'Enter para cerrar' | Out-Null }
exit $exitCode
