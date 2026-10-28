; AI Usage Bridge - instalador con asistente (Inno Setup 6)
;
; Lo compila build\build.ps1:
;   ISCC.exe /DAppVersion=1.0.0 /DNumericVersion=1.0.0 /DStageDir=<carpeta del paquete> /O<salida> installer\setup.iss
;
; El asistente pide los datos del broker MQTT y despues ejecuta install.ps1, que
; hace la instalacion real (servicio, config, accesos directos y la entrada en
; "Aplicaciones instaladas", desde donde se desinstala con uninstall.ps1).
;
; Instalacion silenciosa:
;   AIUsageBridge-Setup.exe /VERYSILENT /MQTTHOST=192.168.1.10 /MQTTPORT=1883 /MQTTUSER=usuario /MQTTPASSWORD=clave /CLAUDEREFRESH=1

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef NumericVersion
  #define NumericVersion "0.0.0"
#endif
#ifndef StageDir
  #define StageDir "..\dist\stage\ai-usage-bridge"
#endif

[Setup]
AppId={{E3E0A6C8-03D9-47A6-92C5-A3618E2CC3FD}
AppName=AI Usage Bridge
AppVersion={#AppVersion}
AppVerName=AI Usage Bridge {#AppVersion}
AppPublisher=The AI Software Factory
AppPublisherURL=https://github.com/The-AI-Software-Factory/ai-usage-bridge
AppSupportURL=https://github.com/The-AI-Software-Factory/ai-usage-bridge/issues
DefaultDirName={commonappdata}\AIUsageBridge
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=no
; La desinstalacion la maneja uninstall.ps1 (entrada propia en Aplicaciones instaladas).
Uninstallable=no
CreateUninstallRegKey=no
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputBaseFilename=AIUsageBridge-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
LicenseFile=..\LICENSE
SetupLogging=yes
ShowLanguageDialog=auto
VersionInfoVersion={#NumericVersion}
VersionInfoProductName=AI Usage Bridge

[Languages]
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
es.MqttTitle=Broker MQTT
es.MqttSubtitle=Datos de conexión al broker MQTT de Home Assistant
es.MqttHelp=Dejá usuario y contraseña vacíos si tu broker acepta conexiones anónimas. Podés cambiarlos después desde el menú Inicio > "Configurar AI Usage Bridge".
es.MqttHost=Host o IP (por ejemplo 192.168.1.10):
es.MqttPort=Puerto:
es.MqttUser=Usuario:
es.MqttPassword=Contraseña:
es.MqttHostRequired=Ingresá el host o la IP del broker MQTT.
es.MqttPortInvalid=El puerto tiene que ser un número entre 1 y 65535.
es.MqttUpgrade=Ya hay una configuración guardada: dejá el host vacío para conservarla tal cual.
es.ClaudeTitle=Claude
es.ClaudeSubtitle=Renovación del token de Claude
es.ClaudeHelp=El uso de Claude se lee con el login de Claude Code. Ese token lo renueva Claude Code (terminal) solo cuando lo usás. Si usás Claude desde la app de escritorio o la web, marcá esta opción para que lo renueve el servicio. Si usás Claude Code en la terminal todos los días, podés dejarla desmarcada.
es.ClaudeRefresh=Renovar automáticamente el token de Claude
es.Installing=Instalando el servicio AI Usage Bridge...
es.InstallFailed=La instalación del servicio falló. Revisá el log:%n%1
en.MqttTitle=MQTT broker
en.MqttSubtitle=Connection details for your Home Assistant MQTT broker
en.MqttHelp=Leave username and password empty if your broker accepts anonymous connections. You can change them later from Start menu > "Configurar AI Usage Bridge".
en.MqttHost=Host or IP (for example 192.168.1.10):
en.MqttPort=Port:
en.MqttUser=Username:
en.MqttPassword=Password:
en.MqttHostRequired=Enter the host or IP of the MQTT broker.
en.MqttPortInvalid=The port must be a number between 1 and 65535.
en.MqttUpgrade=A configuration is already saved: leave the host empty to keep it as is.
en.ClaudeTitle=Claude
en.ClaudeSubtitle=Claude token renewal
en.ClaudeHelp=Claude usage is read with the Claude Code login. Claude Code (terminal) only renews that token when you use it. If you mostly use Claude from the desktop app or the web, check this option so the service renews it. If you use Claude Code in the terminal every day, you can leave it unchecked.
en.ClaudeRefresh=Renew the Claude token automatically
en.Installing=Installing the AI Usage Bridge service...
en.InstallFailed=The service installation failed. Check the log:%n%1

[Files]
Source: "{#StageDir}\*"; DestDir: "{tmp}\payload"; Flags: recursesubdirs createallsubdirs ignoreversion

[Code]
var
  MqttPage: TInputQueryWizardPage;
  ClaudePage: TInputOptionWizardPage;

{ Actualizacion: ya existe config.toml de una instalacion anterior. }
function IsUpgrade: Boolean;
begin
  Result := FileExists(ExpandConstant('{commonappdata}\AIUsageBridge\config.toml'));
end;

procedure InitializeWizard;
var
  Help: String;
begin
  Help := CustomMessage('MqttHelp');
  if IsUpgrade then
    Help := CustomMessage('MqttUpgrade') + #13#10#13#10 + Help;
  MqttPage := CreateInputQueryPage(wpLicense,
    CustomMessage('MqttTitle'), CustomMessage('MqttSubtitle'), Help);
  MqttPage.Add(CustomMessage('MqttHost'), False);
  MqttPage.Add(CustomMessage('MqttPort'), False);
  MqttPage.Add(CustomMessage('MqttUser'), False);
  MqttPage.Add(CustomMessage('MqttPassword'), True);
  MqttPage.Values[0] := ExpandConstant('{param:MQTTHOST|}');
  MqttPage.Values[1] := ExpandConstant('{param:MQTTPORT|1883}');
  MqttPage.Values[2] := ExpandConstant('{param:MQTTUSER|}');
  MqttPage.Values[3] := ExpandConstant('{param:MQTTPASSWORD|}');

  ClaudePage := CreateInputOptionPage(MqttPage.ID,
    CustomMessage('ClaudeTitle'), CustomMessage('ClaudeSubtitle'), CustomMessage('ClaudeHelp'), False, False);
  ClaudePage.Add(CustomMessage('ClaudeRefresh'));
  ClaudePage.Values[0] := ExpandConstant('{param:CLAUDEREFRESH|1}') = '1';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Port: Integer;
begin
  Result := True;
  if CurPageID = MqttPage.ID then
  begin
    if Trim(MqttPage.Values[0]) = '' then
    begin
      if IsUpgrade then
        Exit;
      MsgBox(CustomMessage('MqttHostRequired'), mbError, MB_OK);
      Result := False;
      Exit;
    end;
    Port := StrToIntDef(Trim(MqttPage.Values[1]), 0);
    if (Port < 1) or (Port > 65535) then
    begin
      MsgBox(CustomMessage('MqttPortInvalid'), mbError, MB_OK);
      Result := False;
    end;
  end;
end;

{ Quita comillas para no romper la linea de comandos de PowerShell. }
function Clean(const S: String): String;
begin
  Result := Trim(S);
  StringChangeEx(Result, '"', '', True);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  Params, PassFile, LogFile: String;
  Lines: TArrayOfString;
begin
  { ExitCode no se puede cambiar desde [Code]; el CI verifica el servicio instalado. }
  if CurStep <> ssPostInstall then
    Exit;

  Params := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + ExpandConstant('{tmp}\payload\install.ps1') + '"' +
    ' -Unattended' +
    ' -UserProfileDir "' + ExpandConstant('{%USERPROFILE}') + '"' +
    ' -AppDataDir "' + ExpandConstant('{userappdata}') + '"' +
    ' -InstallDir "' + ExpandConstant('{app}') + '"';
  if ClaudePage.Values[0] then
    Params := Params + ' -ClaudeRefresh true'
  else
    Params := Params + ' -ClaudeRefresh false';

  { Solo se tocan los datos del broker si se cargo un host (en una actualizacion se conservan). }
  PassFile := '';
  if Clean(MqttPage.Values[0]) <> '' then
  begin
    { La contrasena va por archivo temporal (lo borra configure.ps1), no por linea de comandos. }
    PassFile := ExpandConstant('{tmp}\mqtt.secret');
    SetArrayLength(Lines, 1);
    Lines[0] := MqttPage.Values[3];
    SaveStringsToUTF8File(PassFile, Lines, False);
    Params := Params +
      ' -MqttHost "' + Clean(MqttPage.Values[0]) + '"' +
      ' -MqttPort "' + Clean(MqttPage.Values[1]) + '"' +
      ' -MqttPasswordFile "' + PassFile + '"';
    if Clean(MqttPage.Values[2]) <> '' then
      Params := Params + ' -MqttUser "' + Clean(MqttPage.Values[2]) + '"';
  end;

  WizardForm.StatusLabel.Caption := CustomMessage('Installing');
  LogFile := ExpandConstant('{app}\logs\install.log');
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Params, '',
              SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
  begin
    SuppressibleMsgBox(FmtMessage(CustomMessage('InstallFailed'), [LogFile]), mbError, MB_OK, IDOK);
  end;
  if PassFile <> '' then
    DeleteFile(PassFile);
end;
