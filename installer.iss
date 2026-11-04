; Script de instalador Inno Setup para o SteamShotBridge
; Requer o Inno Setup 6 instalado (https://jrsoftware.org/isdl.php)

#define MyAppName "SteamShotBridge"
#define MyAppVersion "0.1.1"
#define MyAppPublisher "Marcos Pena"
#define MyAppURL "https://github.com/Marcospena12/steam-screenshot-importer"
#define MyAppExeName "SteamShotBridge.exe"

[Setup]
AppId={{D1A8F234-8F7A-456B-A9C1-7299E4A981C0}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=LICENSE
OutputDir=dist-installer
OutputBaseFilename=SteamShotBridge-Setup-{#MyAppVersion}
SetupIconFile=assets\steamshotbridge.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "autostart"; Description: "Iniciar automaticamente com o Windows"; GroupDescription: "Opções de inicialização:"

[Files]
Source: "dist\SteamShotBridge\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "assets\*"; DestDir: "{app}\assets"; Flags: ignoreversion recursesubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; Opcional se selecionado na tarefa
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "SteamShotBridge"; ValueData: """{app}\{#MyAppExeName}"" --minimized"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
