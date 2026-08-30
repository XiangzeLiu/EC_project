#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef SourceDir
  #error SourceDir is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif
#ifndef InstalledLauncher
  #error InstalledLauncher is required
#endif
#ifndef InstalledLocalConfig
  #error InstalledLocalConfig is required
#endif
#ifndef ArtifactSuffix
  #define ArtifactSuffix ""
#endif

#define AppName "SC Server Manager"
#define AppExeName "ServerManager.exe"

[Setup]
AppId={{8F27C081-DF73-4247-95C3-F2337836FFFB}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=SC Project
VersionInfoCompany=SC Project
VersionInfoDescription=SC Server Manager Installer
DefaultDirName={autopf}\SC\Server Manager
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=SC_SM_Setup_{#AppVersion}{#ArtifactSuffix}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}
#ifdef EnableSigning
SignTool=scsign
SignedUninstaller=yes
SignToolRetryCount=3
SignToolRetryDelay=500
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Dirs]
Name: "{commonappdata}\SC\ServerManager"; Flags: uninsneveruninstall; AfterInstall: HardenRuntimeAcl
Name: "{commonappdata}\SC\ServerManager\data"; Flags: uninsneveruninstall
Name: "{commonappdata}\SC\ServerManager\caddy"; Flags: uninsneveruninstall

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\caddy"
Type: files; Name: "{app}\ServerManager.exe"
Type: files; Name: "{app}\start_sm.bat"
Type: files; Name: "{app}\BUILD_INFO.json"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Excludes: "start_sm.bat,sm.local.bat.example,sm.env.example"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#InstalledLauncher}"; DestDir: "{app}"; DestName: "start_sm.bat"; Flags: ignoreversion
Source: "{#InstalledLocalConfig}"; DestDir: "{commonappdata}\SC\ServerManager"; DestName: "sm.local.bat"; Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\start_sm.bat"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"
Name: "{commondesktop}\{#AppName}"; Filename: "{app}\start_sm.bat"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"; Tasks: desktopicon

[UninstallRun]
Filename: "{app}\caddy\caddy.exe"; Parameters: "stop --address 127.0.0.1:2019"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "StopServerManagerCaddy"

[Code]
procedure HardenRuntimeAcl;
var
  ResultCode: Integer;
  RuntimeDir: String;
  Arguments: String;
begin
  RuntimeDir := ExpandConstant('{commonappdata}\SC\ServerManager');
  Arguments := '"' + RuntimeDir + '" /inheritance:r /grant:r ' +
    '"*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" /T /C';
  if (not Exec(ExpandConstant('{sys}\icacls.exe'), Arguments, '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
    RaiseException('Unable to secure the Server Manager runtime directory.');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  CaddyExe: String;
  ResultCode: Integer;
begin
  Result := '';
  CaddyExe := ExpandConstant('{app}\caddy\caddy.exe');
  if FileExists(CaddyExe) then
    Exec(CaddyExe, 'stop --address 127.0.0.1:2019', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode);
end;
