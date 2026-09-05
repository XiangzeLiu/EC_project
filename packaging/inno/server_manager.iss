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
#ifndef InstallerHelper
  #error InstallerHelper is required
#endif
#ifndef ArtifactSuffix
  #define ArtifactSuffix ""
#endif

#define AppName "SC Server Manager"
#define AppExeName "ServerManager.exe"

[Setup]
AppId={{8F27C081-DF73-4247-95C3-F2337836FFFB}
AppMutex=SCServerManagerInstaller
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
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标："

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
Source: "{#InstallerHelper}"; DestDir: "{tmp}"; Flags: dontcopy deleteafterinstall

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\start_sm.bat"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"
Name: "{commondesktop}\{#AppName}"; Filename: "{app}\start_sm.bat"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"; Tasks: desktopicon

[UninstallRun]
Filename: "{app}\caddy\caddy.exe"; Parameters: "stop --address 127.0.0.1:2019"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "StopServerManagerCaddy"

[Code]
var
  DeploymentModePage: TInputOptionWizardPage;
  DataSourcePage: TInputDirWizardPage;
  ConfigurationPage: TInputQueryWizardPage;
  CertificatePage: TInputFileWizardPage;
  FixedConfigurationPage: TInputQueryWizardPage;
  RequestFilePath: String;
  StateFilePath: String;
  Prepared: Boolean;

function QuoteArgument(Value: String): String;
begin
  Result := '"' + Value + '"';
end;

function HelperFilePath: String;
begin
  Result := ExpandConstant('{tmp}\SC_SM_InstallerHelper.exe');
end;

function RunHelper(Arguments: String): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(HelperFilePath, Arguments, '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
    and (ResultCode = 0);
end;

function InitializeSetup: Boolean;
var
  ExistingStatePath: String;
  ExistingLockPath: String;
  ResultCode: Integer;
begin
  Result := True;
  ExistingStatePath := ExpandConstant('{commonappdata}\SC\ServerManager\.installer\transaction.json');
  ExistingLockPath := ExpandConstant('{commonappdata}\SC\ServerManager\.installer\install.lock');
  if FileExists(ExistingStatePath) or FileExists(ExistingLockPath) then begin
    ExtractTemporaryFile('SC_SM_InstallerHelper.exe');
    if (not Exec(HelperFilePath,
        '--discard-stale --state-file ' + QuoteArgument(ExistingStatePath) +
        ' --runtime-root ' + QuoteArgument(ExpandConstant('{commonappdata}\SC\ServerManager')) +
        ' --app-dir ' + QuoteArgument(ExpandConstant('{app}')) +
        ' --data-dir ' + QuoteArgument(ExpandConstant('{commonappdata}\SC\ServerManager\data')), '', SW_HIDE,
        ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then begin
      MsgBox('上次 SM 安装残留清理失败，请关闭 SM 后重试。',
        mbError, MB_OK);
      Result := False;
    end;
  end;
end;

procedure ProtectRequestFile;
var
  ResultCode: Integer;
  Arguments: String;
begin
  Arguments := '"' + RequestFilePath + '" /inheritance:r /grant:r ' +
    '"*S-1-5-18:F" "*S-1-5-32-544:F"';
  if (not Exec(ExpandConstant('{sys}\icacls.exe'), Arguments, '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
    RaiseException('无法保护临时 SM 安装配置文件。');
end;

procedure WriteInstallerRequest;
var
  Mode: String;
begin
  if DeploymentModePage.SelectedValueIndex = 0 then
    Mode := 'fresh'
  else
    Mode := 'upgrade';

  DeleteFile(RequestFilePath);
  SetIniString('install', 'mode', Mode, RequestFilePath);
  SetIniString('install', 'app_dir', ExpandConstant('{app}'), RequestFilePath);
  SetIniString('install', 'data_dir', ExpandConstant('{commonappdata}\SC\ServerManager\data'), RequestFilePath);
  SetIniString('install', 'source_data', DataSourcePage.Values[0], RequestFilePath);
  SetIniString('install', 'server_host', '127.0.0.1', RequestFilePath);
  SetIniString('install', 'server_port', '18800', RequestFilePath);
  SetIniString('install', 'public_http_port', '8800', RequestFilePath);
  SetIniString('install', 'public_https_port', '4430', RequestFilePath);
  SetIniString('install', 'public_base_url', 'https://scjrdomain.com:4430', RequestFilePath);
  SetIniString('install', 'caddy_admin', '127.0.0.1:2019', RequestFilePath);
  SetIniString('install', 'bootstrap_admin_username', ConfigurationPage.Values[0], RequestFilePath);
  SetIniString('install', 'bootstrap_admin_password', ConfigurationPage.Values[1], RequestFilePath);
  SetIniString('install', 'dnspod_secret_id', ConfigurationPage.Values[2], RequestFilePath);
  SetIniString('install', 'dnspod_secret_key', ConfigurationPage.Values[3], RequestFilePath);
  SetIniString('install', 'certificate_source', CertificatePage.Values[0], RequestFilePath);
  SetIniString('install', 'key_source', CertificatePage.Values[1], RequestFilePath);
  SetIniString('install', 'application_version', '{#AppVersion}', RequestFilePath);
  ProtectRequestFile;
end;

function RunPreflight: Boolean;
var
  ReportFilePath: String;
begin
  ExtractTemporaryFile('SC_SM_InstallerHelper.exe');
  ReportFilePath := ExpandConstant('{tmp}\SC_SM_Preflight.json');
  Result := RunHelper(
    '--preflight --request-file ' + QuoteArgument(RequestFilePath) +
    ' --report-file ' + QuoteArgument(ReportFilePath));
  if not Result then
    MsgBox('SM 安装前检查失败。请查看生成的检查报告：' + ReportFilePath,
      mbError, MB_OK);
end;

function RunEnvironmentPreflight: Boolean;
var
  ReportFilePath: String;
begin
  ExtractTemporaryFile('SC_SM_InstallerHelper.exe');
  ReportFilePath := ExpandConstant('{tmp}\SC_SM_EnvironmentPreflight.json');
  Result := RunHelper(
    '--environment-preflight --app-dir ' + QuoteArgument(ExpandConstant('{app}')) +
    ' --data-dir ' + QuoteArgument(ExpandConstant('{commonappdata}\SC\ServerManager\data')) +
    ' --report-file ' + QuoteArgument(ReportFilePath));
  if not Result then
    MsgBox('SM 环境自检失败。请查看检查报告：' + ReportFilePath,
      mbError, MB_OK);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if Prepared then
    exit;
  WriteInstallerRequest;
  ExtractTemporaryFile('SC_SM_InstallerHelper.exe');
  StateFilePath := ExpandConstant('{commonappdata}\SC\ServerManager\.installer\transaction.json');
  if not RunHelper(
    '--prepare --request-file ' + QuoteArgument(RequestFilePath) +
    ' --state-file ' + QuoteArgument(StateFilePath)) then
    Result := 'SM 安装准备失败，未替换现有程序文件。';
  if Result = '' then
    Prepared := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and Prepared then begin
    if not RunHelper(
      '--commit --request-file ' + QuoteArgument(RequestFilePath) +
      ' --state-file ' + QuoteArgument(StateFilePath)) then
      RaiseException('SM 安装提交失败，失败产物已清理；升级时选择的旧 data 目录不会被修改。');
    DeleteFile(RequestFilePath);
  end;
end;

procedure InitializeWizard;
begin
  RequestFilePath := ExpandConstant('{tmp}\SC_SM_InstallerRequest.ini');
  DeploymentModePage := CreateInputOptionPage(
    wpSelectDir,
    '部署方式',
    '请选择 SM 的部署方式',
    '全新部署会创建新的运行数据；升级迁移会从选择的旧 data 目录导入业务数据。',
    True,
    True);
  DeploymentModePage.Add('全新部署');
  DeploymentModePage.Add('升级迁移');
  DeploymentModePage.SelectedValueIndex := 0;

  DataSourcePage := CreateInputDirPage(
    DeploymentModePage.ID,
    '旧 data 目录',
    '选择需要迁移的旧 SM data 目录',
    '仅升级迁移时必填。目录必须包含 server_manager.db；安装器只读导入，不会执行其中的脚本。',
    False,
    '');
  DataSourcePage.Add('旧 data 目录（升级迁移必填）：');

  ConfigurationPage := CreateInputQueryPage(
    DataSourcePage.ID,
    'SM 初始配置',
    '填写 SM 的初始运行配置',
    '管理员账号必填。全新部署时管理员密码和 DNSPod 密钥必填；升级迁移可沿用旧 data 中的有效配置。DNSPod 密钥填写腾讯云 CAM API 密钥，不是 DNSPod Token 或腾讯云登录密码。');
  ConfigurationPage.Add('SM 管理员账号（必填）：', False);
  ConfigurationPage.Add('SM 管理员密码（条件必填）：', True);
  ConfigurationPage.Add('DNSPod SecretId（条件必填）：', False);
  ConfigurationPage.Add('DNSPod SecretKey（条件必填）：', True);
  ConfigurationPage.Values[0] := 'admin';

  CertificatePage := CreateInputFilePage(
    ConfigurationPage.ID,
    'SSL 证书配置',
    '选择 SSL 证书和私钥文件',
    '证书和私钥必须同时提供，并覆盖 scjrdomain.com。全新部署时必填；升级迁移时可沿用旧 data 中的有效证书对。');
  CertificatePage.Add('SSL 证书文件（条件必填）：',
    '证书文件 (*.crt;*.pem)|*.crt;*.pem|所有文件 (*.*)|*.*', '');
  CertificatePage.Add('SSL 私钥文件（条件必填）：',
    '私钥文件 (*.key;*.pem)|*.key;*.pem|所有文件 (*.*)|*.*', '');

  FixedConfigurationPage := CreateInputQueryPage(
    CertificatePage.ID,
    '固定生产访问配置',
    '确认 SM 固定生产访问参数',
    '以下内容由系统固定，仅展示，不可修改。');
  FixedConfigurationPage.Add('SM 域名（系统固定不可修改）：', False);
  FixedConfigurationPage.Add('公网 HTTP 端口（系统固定不可修改）：', False);
  FixedConfigurationPage.Add('公网 HTTPS 端口（系统固定不可修改）：', False);
  FixedConfigurationPage.Add('Client 访问地址（系统固定不可修改）：', False);
  FixedConfigurationPage.Add('SM 本地应用端口（系统固定不可修改）：', False);
  FixedConfigurationPage.Values[0] := 'scjrdomain.com';
  FixedConfigurationPage.Values[1] := '8800';
  FixedConfigurationPage.Values[2] := '4430';
  FixedConfigurationPage.Values[3] := 'https://scjrdomain.com:4430';
  FixedConfigurationPage.Values[4] := '18800';
  FixedConfigurationPage.Edits[0].ReadOnly := True;
  FixedConfigurationPage.Edits[1].ReadOnly := True;
  FixedConfigurationPage.Edits[2].ReadOnly := True;
  FixedConfigurationPage.Edits[3].ReadOnly := True;
  FixedConfigurationPage.Edits[4].ReadOnly := True;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = DataSourcePage.ID) and (DeploymentModePage.SelectedValueIndex = 0);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpSelectDir then begin
    Result := RunEnvironmentPreflight;
  end else if CurPageID = FixedConfigurationPage.ID then begin
    WriteInstallerRequest;
    Result := RunPreflight;
  end;
end;

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
    RaiseException('无法保护 Server Manager 运行目录。');
end;
