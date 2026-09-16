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
; （0.6.0 起三处一致，见 AGENTS.md）。

#define MyAppName "SkySheep"
#define MyAppVersion "1.0"
#define MyAppPublisher "SkySheep contributors"
#define MyAppURL "https://github.com/Sky-scrape/SkySheep"
#define MyAppExeName "SkySheep.exe"

[Setup]
AppId={{7C1B6E9A-52C4-4B7D-9A34-A1B2C3D4E5F6}
AppName={#MyAppName}
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
PrivilegesRequiredOverridesAllowed=dialog
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

[UninstallDelete]
; 清理运行产生的临时日志目录外的安装目录残留（用户数据 ~/.skysheep 不在安装目录，不受影响）
Type: filesandordirs; Name: "{app}"
