; ============================================================
;  RedOne Creative — Windows installer (Inno Setup 6)
;  Build:  build_installer.bat   (hoặc:  ISCC.exe installer\RedOne.iss)
;  Prereq: 1) Inno Setup 6 đã cài   2) dist\RedOne Creative\ đã build (build.bat)
; ------------------------------------------------------------
;  Gói app + TỰ ghi khóa force-install extension (HKLM policy)
;  → member chạy 1 file setup là xong cả app lẫn extension.
; ============================================================

#define MyAppName "RedOne Creative"
#define MyAppVersion "1.6.3"
#define MyAppPublisher "RedOne"
#define MyAppExeName "RedOne Creative.exe"

; Extension force-install (khớp chrome-ext\update.xml đã push lên GitHub)
#define ExtId "mjmcefhplbpghdpgpcefbaofenbegdmk"
#define UpdateUrl "https://raw.githubusercontent.com/chovuithoima9-dev/RedOne-Creative-tool/main/chrome-ext/update.xml"

; Đường dẫn nguồn — .iss nằm trong installer\, repo root là cấp trên
#define SrcRoot "..\"

[Setup]
AppId={{7C3D9A41-2E58-4B0F-9C6A-8D5E1F2A3B4C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
; Cài vào ổ hệ thống (C:) — KHÔNG dùng Program Files vì app ghi data/outputs
; cạnh exe và tự-update ghi đè tại chỗ (cần thư mục ghi được). User đổi sang
; D:\RedOne Creative... được ở trang chọn thư mục.
DefaultDirName={sd}\RedOne Creative
DisableProgramGroupPage=yes
DefaultGroupName={#MyAppName}
; Cần admin để ghi HKLM policy (force-install) + cấp quyền thư mục.
PrivilegesRequired=admin
; Đóng app đang chạy trước khi ghi đè (cho lần cài lại/nâng cấp).
CloseApplications=yes
RestartApplications=no
OutputDir={#SrcRoot}dist
OutputBaseFilename=RedOne-Creative-Setup-v{#MyAppVersion}
SetupIconFile={#SrcRoot}redone.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Toàn bộ output PyInstaller (--onedir). Bỏ folder extension\ vì đã force-install
; từ GitHub — tránh member lỡ Load-unpacked bản cũ (trùng ID).
Source: "{#SrcRoot}dist\RedOne Creative\*"; DestDir: "{app}"; Excludes: "\extension\*"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Gỡ cài đặt {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; Force-install extension qua Chrome Enterprise policy (máy lẻ vẫn đọc HKLM\...\Policies).
; SOFTWARE\Policies là khóa SHARED (không bị WOW64 redirect) → 32-bit setup ghi vẫn đúng.
Root: HKLM; Subkey: "SOFTWARE\Policies\Google\Chrome\ExtensionInstallForcelist"; ValueType: string; ValueName: "1"; ValueData: "{#ExtId};{#UpdateUrl}"; Flags: uninsdeletevalue

[Run]
; Cấp quyền Modify cho nhóm Users (SID *S-1-5-32-545, độc lập ngôn ngữ) → app ghi
; được data/outputs + bản tự-update ghi đè exe/_internal khi chạy bằng user thường.
Filename: "{sys}\icacls.exe"; Parameters: """{app}"" /grant *S-1-5-32-545:(OI)(CI)M /T /C /Q"; Flags: runhidden waituntilterminated; StatusMsg: "Đang cấp quyền ghi cho thư mục cài đặt..."
; Khởi chạy app bằng USER THƯỜNG (không phải tiến trình admin của installer).
Filename: "{app}\{#MyAppExeName}"; Description: "Khởi chạy {#MyAppName}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[Messages]
FinishedLabel=Cài đặt {#MyAppName} hoàn tất.%n%nQUAN TRỌNG: hãy ĐÓNG hẳn Chrome rồi MỞ LẠI để extension "RedOne Auth Helper" tự cài (chrome://extensions sẽ hiện "Installed by enterprise policy"). Sau đó mở https://labs.google/fx/tools/flow đăng nhập Google.
