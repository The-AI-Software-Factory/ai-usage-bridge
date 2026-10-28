#Requires -Version 7.2
<#
.SYNOPSIS
  Compila los instaladores de AI Usage Bridge a partir del codigo del repo.
.DESCRIPTION
  Genera en .\dist:
    - ai-usage-bridge.zip        paquete portable (install.cmd) con Python embebido
    - AIUsageBridge-Setup.exe    instalador con asistente (Inno Setup)
    - AIUsageBridge.msi          instalador MSI (WiX Toolset)
    - SHA256SUMS.txt

  Requisitos (en Windows 10/11 x64):
    - PowerShell 7.2+          winget install Microsoft.PowerShell
    - Python 3.12              winget install Python.Python.3.12   (solo para bajar las dependencias)
    - .NET SDK 8+              winget install Microsoft.DotNet.SDK.8   (para WiX)
    - Inno Setup 6             se instala solo con winget si falta
    - WiX 5                    se instala solo como herramienta de dotnet si falta

.EXAMPLE
  .\build\build.ps1
.EXAMPLE
  .\build\build.ps1 -Version 1.2.0 -SkipMsi
#>
param(
    [string]$Version,
    [string]$PythonVersion = '3.12.10',
    [string]$WixVersion = '5.0.2',
    [switch]$SkipSetup,
    [switch]$SkipMsi
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$dist = Join-Path $root 'dist'
$stage = Join-Path $dist 'stage\ai-usage-bridge'
$cache = Join-Path $root 'build\.cache'
$WinSWUrl = 'https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe'
$WinSWSha256 = '05B82D46AD331CC16BDC00DE5C6332C1EF818DF8CEEFCD49C726553209B3A0DA'

function Write-Step([string]$Text) { Write-Host "`n==> $Text" -ForegroundColor Cyan }
function Assert-Exit([string]$What) { if ($LASTEXITCODE -ne 0) { throw "$What fallo (codigo $LASTEXITCODE)" } }

function Get-Cached([string]$Url, [string]$Name) {
    New-Item -ItemType Directory -Force -Path $cache | Out-Null
    $path = Join-Path $cache $Name
    if (-not (Test-Path $path)) {
        Write-Host "Descargando $Url"
        Invoke-WebRequest -Uri $Url -OutFile "$path.part"
        Move-Item "$path.part" $path
    }
    return $path
}

# --- Version ---------------------------------------------------------------
if (-not $Version) {
    $Version = ([regex]::Match((Get-Content "$root\app\ai_usage_bridge\__init__.py" -Raw), '__version__ = "([^"]+)"')).Groups[1].Value
}
$Version = $Version.TrimStart('v')
if ($Version -notmatch '^\d+\.\d+\.\d+') { throw "Version invalida: $Version" }
$numericVersion = ([regex]::Match($Version, '^\d+\.\d+\.\d+')).Value
Write-Host "AI Usage Bridge $Version" -ForegroundColor Green

# --- 1. Validar scripts de PowerShell --------------------------------------
Write-Step 'Validando sintaxis de los scripts de PowerShell'
foreach ($file in Get-ChildItem $root -Recurse -Filter *.ps1 | Where-Object { $_.FullName -notmatch '\\dist\\|\\.cache\\' }) {
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$null, [ref]$errors) | Out-Null
    if ($errors) { $errors | ForEach-Object { Write-Host "$($file.Name):$($_.Extent.StartLineNumber) $($_.Message)" -ForegroundColor Red }; throw 'Errores de sintaxis' }
}

# --- 2. Paquete -------------------------------------------------------------
Write-Step 'Armando el paquete'
if (Test-Path $dist) { Remove-Item $dist -Recurse -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null
Copy-Item "$root\app" "$stage\app" -Recurse
Get-ChildItem $stage -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
foreach ($f in 'install.ps1', 'install.cmd', 'uninstall.ps1', 'uninstall.cmd', 'configure.ps1', 'configure.cmd',
               'config.template.toml', 'requirements.txt', 'README.md', 'README.es.md', 'LICENSE') {
    Copy-Item "$root\$f" $stage
}
# Sella la version en la app y el instalador
$init = "$stage\app\ai_usage_bridge\__init__.py"
(Get-Content $init -Raw) -replace '__version__ = "[^"]+"', "__version__ = `"$Version`"" | Set-Content $init -NoNewline -Encoding utf8NoBOM
$inst = "$stage\install.ps1"
(Get-Content $inst -Raw) -replace "\`$Version = '[^']+'", "`$Version = '$Version'" | Set-Content $inst -NoNewline -Encoding utf8BOM

# --- 3. Python embebido -----------------------------------------------------
Write-Step "Python embebido $PythonVersion"
$pyZip = Get-Cached "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip" "python-$PythonVersion-embed-amd64.zip"
Expand-Archive $pyZip "$stage\python"
$pth = Get-ChildItem "$stage\python" -Filter 'python3*._pth' | Select-Object -First 1
# Con ._pth, Python ignora PYTHONPATH: declaramos aca las carpetas de la app.
Set-Content $pth.FullName -Value @("$($pth.BaseName).zip", '.', '..\app', '..\lib') -Encoding ascii

# --- 4. Dependencias --------------------------------------------------------
Write-Step 'Dependencias'
$pyMinor = ($PythonVersion -split '\.')[0..1] -join '.'
$hostPython = (Get-Command py -ErrorAction SilentlyContinue) ? @('py', "-$pyMinor") : @('python')
& $hostPython[0] @($hostPython | Select-Object -Skip 1) -m pip install --disable-pip-version-check --no-compile `
    --only-binary=:all: --platform win_amd64 --implementation cp --python-version $pyMinor `
    --target "$stage\lib" -r "$root\requirements.txt"
Assert-Exit 'pip install'
Get-ChildItem "$stage\lib" -Directory -Filter bin | Remove-Item -Recurse -Force

# --- 5. WinSW ---------------------------------------------------------------
Write-Step 'WinSW'
$winsw = Get-Cached $WinSWUrl 'WinSW-x64-2.12.0.exe'
if ((Get-FileHash $winsw -Algorithm SHA256).Hash -ne $WinSWSha256) { Remove-Item $winsw; throw 'El hash de WinSW no coincide' }
New-Item -ItemType Directory -Force -Path "$stage\winsw" | Out-Null
Copy-Item $winsw "$stage\winsw\WinSW-x64.exe"

# --- 6. Prueba con el Python embebido ---------------------------------------
Write-Step 'Prueba del paquete con el Python embebido'
$out = & "$stage\python\python.exe" -c "import ai_usage_bridge, ai_usage_bridge.main, paho.mqtt.client, requests, sqlite3, tomllib; print(ai_usage_bridge.__version__)"
Assert-Exit 'Prueba del Python embebido'
if ("$out".Trim() -ne $Version) { throw "La version empaquetada ($out) no coincide con $Version" }
Write-Host "OK ($out)"

# --- 7. Zip -----------------------------------------------------------------
Write-Step 'ai-usage-bridge.zip'
Compress-Archive -Path $stage -DestinationPath "$dist\ai-usage-bridge.zip"

# --- 8. Setup.exe (Inno Setup) ----------------------------------------------
if (-not $SkipSetup) {
    Write-Step 'AIUsageBridge-Setup.exe (Inno Setup)'
    $iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
              "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) {
        Write-Host 'Instalando Inno Setup con winget...'
        winget install -e --id JRSoftware.InnoSetup --silent --accept-package-agreements --accept-source-agreements
        $iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
                  "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
        if (-not $iscc) { throw 'No se encontro ISCC.exe. Instala Inno Setup 6 y volve a intentar (o usa -SkipSetup).' }
    }
    & $iscc /Q "/DAppVersion=$Version" "/DNumericVersion=$numericVersion" "/DStageDir=$stage" "/O$dist" "$root\installer\setup.iss"
    Assert-Exit 'Inno Setup'
}

# --- 9. MSI (WiX) -----------------------------------------------------------
if (-not $SkipMsi) {
    Write-Step 'AIUsageBridge.msi (WiX)'
    if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) { throw 'Falta el .NET SDK (winget install Microsoft.DotNet.SDK.8) o usa -SkipMsi.' }
    $wix = Get-Command wix -ErrorAction SilentlyContinue
    $wixOk = $wix -and ((& wix --version) -like "$WixVersion*")
    if (-not $wixOk) {
        if ($wix) { dotnet tool uninstall --global wix | Out-Null }
        dotnet tool install --global wix --version $WixVersion
        Assert-Exit 'dotnet tool install wix'
        $env:Path += ";$env:USERPROFILE\.dotnet\tools"
    }
    foreach ($ext in 'WixToolset.Util.wixext', 'WixToolset.UI.wixext') {
        wix extension add -g "$ext/$WixVersion" | Out-Null
        Assert-Exit "wix extension add $ext"
    }
    wix build "$root\installer\AIUsageBridge.wxs" -arch x64 -d "Version=$numericVersion" -d "StageDir=$stage" `
        -ext WixToolset.Util.wixext -ext WixToolset.UI.wixext -o "$dist\AIUsageBridge.msi"
    Assert-Exit 'wix build'
    Remove-Item "$dist\AIUsageBridge.wixpdb" -ErrorAction SilentlyContinue
}

# --- 10. Hashes -------------------------------------------------------------
Write-Step 'SHA256SUMS.txt'
$files = Get-ChildItem $dist -File | Where-Object { $_.Extension -in '.zip', '.exe', '.msi' }
$files | ForEach-Object { "{0}  {1}" -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(), $_.Name } |
    Set-Content "$dist\SHA256SUMS.txt" -Encoding ascii
Get-Content "$dist\SHA256SUMS.txt"
Write-Host "`nListo: $dist" -ForegroundColor Green
