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
    $m = [regex]::Match($Text, "(?m)^\s*$Key\s*=\s*(\d+)")
    if ($m.Success) { return $m.Groups[1].Value }
    return ''
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
        Write-Host 'AI Usage Bridge - configuracion del broker MQTT' -ForegroundColor Green
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
