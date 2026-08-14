; FormControl 2.0 安装脚本（本地服务 + 多用户登录方案）
; 默认安装到 C:\Users\Public\FORMCONTROL，桌面快捷方式启动本地数据服务 FormControl.exe，
; 服务自动打开浏览器并托管 login.html -> 表格分类汇总.html / admin.html；
; 业务数据落盘到 安装路径\data.sqlite，账户数据在 auth.sqlite。
; 数据完全不依赖浏览器存储，清除任何浏览数据均不影响；数据文件在安装目录内，卸载时不被删除。
; 注：Inno Setup 6 默认未捆绑中文语言包，安装向导使用内置英文（应用界面本身为中文）。

[Setup]
AppName=质量文控平台 · 表格与文件分类汇总
AppVersion=2.0.0
AppPublisher=质量文控平台
DefaultDirName={%PUBLIC}\FORMCONTROL
DefaultGroupName=FORMCONTROL
OutputDir=..\..\dist_pkg
OutputBaseFilename=FormControl_Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
UsedUserAreasWarning=no
DisableProgramGroupPage=yes
UninstallDisplayName=质量文控平台
AppMutex=FormControlAppMutex

[Files]
; 释放本地数据服务程序、HTML 与图标到安装目录（保留原文件名，避免破坏任何内部引用）
Source: "dist_release\FormControl.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\login.html"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\admin.html"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\表格分类汇总.html"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\图标.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; 桌面快捷方式：启动本地数据服务（FormControl.exe），由服务打开浏览器并托管 HTML；
; 参数为安装目录，服务把业务数据落盘到 {app}\data.sqlite。图标使用随包释放的 图标.ico。
; 名称按需求固定为「质量文控平台」。
Name: "{userdesktop}\质量文控平台"; Filename: "{app}\FormControl.exe"; Parameters: "{app}"; WorkingDir: "{app}"; IconFilename: "{app}\图标.ico"

[Code]
// 安装前：强制结束任何残留进程（安装时 {app} 尚未初始化，无法使用优雅关闭）
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM FormControl.exe /T', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := true;
end;

// 卸载前：优雅关闭服务；若失败则强制结束进程
function InitializeUninstall(): Boolean;
var
  ResultCode: Integer;
  ExePath: String;
begin
  ExePath := ExpandConstant('{app}\FormControl.exe');
  if FileExists(ExePath) then
  begin
    Exec(ExePath, ExpandConstant('--shutdown "{app}"'), '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM FormControl.exe /T', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := true;
end;
