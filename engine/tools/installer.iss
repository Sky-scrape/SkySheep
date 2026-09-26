; SkySheep Windows 安装包脚本（Inno Setup 6+）
;
; 用法：
;   1. 先打包 PyInstaller 产物（在 engine/ 目录）：
;      .venv\Scripts\pyinstaller.exe --noconfirm --clean SkySheep.spec
;      产物在 dist\SkySheep\（SkySheep.exe 及随附文件）
;   2. 安装 Inno Setup 6（https://jrsoftware.org/isinfo.php），然后：
;      ISCC.exe tools\installer.iss
;      产物：installer\SkySheep-<版本>-setup.exe
;
; 发版时注意：版本号与 pyproject.toml / src/skysheep/__init__.py 保持一致
; （0.6.0 起三处一致，版本以 pyproject.toml 为准；变更记录见 CHANGELOG.md）。注：
; 静默部署：安装器默认要求管理员权限（{autopf}），无人值守场景加 /CURRENTUSER
; 落到用户目录，例如：
;   SkySheep-1.9-setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER /DIR="D:\SkySheep"
; 卸载（含静默）：应用运行中会被拒绝（退出码非零），请先退出应用。

#define MyAppName "SkySheep"
#define MyAppVersion "2.0"
#define MyAppPublisher "SkySheep contributors"
#define MyAppURL "https://github.com/Sky-scrape/SkySheep"
#define MyAppExeName "SkySheep.exe"

[Setup]
AppId={{7C1B6E9A-52C4-4B7D-9A34-A1B2C3D4E5F6}
AppName={#MyAppName}
; 安装器与桌面启动器共用同名单实例互斥体（default 身份，即 skysheep.instance 的
; mutex_name()）。否则安装程序会在应用运行时直接覆盖被占用的文件（PyInstaller
; 产物含 dll/pyd，占用中的文件无法替换，结果是装出一个半新半旧、启动即崩的目录）。
; 只声明 default 身份：源码版跑在 dev 身份、持的是另一个互斥体，装安装版不会
; 被它误拦，这正是两份并存应有的行为。
AppMutex=Local\SkySheepDesktopSingleton
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 用户数据都在 ~/.skysheep，卸载不碰它（会话记录保留）
UninstallDisplaySize=780
OutputDir=..\installer
OutputBaseFilename=SkySheep-{#MyAppVersion}-setup
SetupIconFile=..\src\skysheep\server\static\skysheep.ico
Compression=lzma
SolidCompression=yes
WizardStyle=modern
; 64 位优先，32 位系统退回 Program Files (x86)
ArchitecturesInstallIn64BitMode=x64compatible
; 允许命令行 /CURRENTUSER 降权：静默安装/沙箱验收依赖它（2026-09-25 审查 P3-18）
PrivilegesRequiredOverridesAllowed=dialog commandline
; 允许覆盖旧版本文件
UsePreviousAppDir=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; PyInstaller 整目录产物（dist\SkySheep\* → 安装目录）
Source: "..\dist\SkySheep\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
; 程序内更新链路（apply_update 传 /RESTARTAPP）：静默装完自动拉起新版，
; 免去「装完没动静、用户以为没更新」的空窗。runasoriginaluser 避免新版继承安装器的管理员权限。
Filename: "{app}\{#MyAppExeName}"; Flags: nowait runasoriginaluser; Check: RestartAppRequested

[UninstallDelete]
; 清理运行产生的临时日志目录外的安装目录残留（用户数据 ~/.skysheep 不在安装目录，不受影响）
Type: filesandordirs; Name: "{app}"

[Code]
function RestartAppRequested(): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if SameText(ParamStr(I), '/RESTARTAPP') then
      Result := True;
end;

// 审查 S-18（2026-09-25）：卸载前检查应用是否在运行。此前 VERYSILENT 卸载
// 不拦运行中的实例，AppMutex 检查被一并抑制，in-use 的主程序与 _internal/
// 残留在安装目录删不掉。现在卸载初始化时先探互斥体：
// - 交互卸载：提示用户退出后重试；
// - 静默卸载（/VERYSILENT，程序内更新等场景）：中止并返回非零退出码，
//   由调用方（apply_update）先停进程再发起卸载。
function InitializeUninstall(): Boolean;
var
  R: Integer;
begin
  Result := True;
  if CheckForMutexes('Local\SkySheepDesktopSingleton,Local\SkySheepDesktopSingleton-dev') then begin
    if WizardSilent() then begin
      Result := False;  // 静默卸载：不弹窗，直接失败退出（退出码非零）
    end else begin
      R := MsgBox('SkySheep 正在运行，必须先退出应用才能卸载。' + Chr(13) + Chr(10) + Chr(13) + Chr(10) + '请退出 SkySheep（系统托盘图标 → 退出），然后点「重试」继续卸载。', mbConfirmation, MB_RETRYCANCEL);
      if R = IDCANCEL then
        Result := False;
    end;
  end;
end;
