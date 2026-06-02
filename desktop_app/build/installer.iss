; Inno Setup script for ST MCP Desktop App
; Build with: iscc installer.iss  (from build\ directory)
; Output: build\Output\ST_MCP_Setup.exe

#define AppName       "ST MCP Connector"
#define AppVersion    "1.0.0"
#define AppPublisher  "Denomme & Plumbing"
#define AppURL        "https://github.com/adamsbenjamin8-boop/st-mcp"
#define AppExeName    "ST_MCP_Launcher.exe"
#define AppDirName    "ST_MCP"

[Setup]
AppId={{B4F2A1C8-3D7E-4F9A-B5C2-8E1D6A3F7B9C}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
DefaultDirName={autopf}\{#AppDirName}
DefaultGroupName={#AppName}
AllowNoIcons=yes
OutputDir=Output
OutputBaseFilename=ST_MCP_Setup
SetupIconFile=
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; After install: grant write access, install Python dependencies, then launch the app
[Run]
Filename: "icacls.exe"; Parameters: """{app}"" /grant Users:(OI)(CI)F /T"; Flags: runhidden waituntilterminated
Filename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -File ""{app}\setup_claude_config.ps1"""; Flags: runhidden waituntilterminated
Filename: "python.exe"; Parameters: "-m pip install mcp httpx pdfplumber --quiet"; \
  Description: "Installing Python dependencies"; \
  Flags: runhidden waituntilterminated
Filename: "{app}\{#AppExeName}"; Description: "Launch ST MCP Connector"; Flags: nowait postinstall skipifsilent

[Files]
; Main launcher executable (built by PyInstaller)
Source: "..\dist\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion

; The MCP script files — these get updated in-place by the auto-updater
Source: "..\..\servicetitan_writer.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\st_cache_sync.py";       DestDir: "{app}"; Flags: ignoreversion
Source: "..\version.py";                DestDir: "{app}"; Flags: ignoreversion

; Credentials sidecar — pre-filled with the shared app key
Source: "..\assets\.env.template";     DestDir: "{app}"; DestName: ".env"; Flags: onlyifdoesntexist

; Claude config setup script
Source: "..\assets\setup_claude_config.ps1"; DestDir: "{app}"; Flags: ignoreversion

; Quote app — processes vendor quotes into ST PO Requests
Source: "..\..\quote_app\main.py";             DestDir: "{app}\quote_app"; Flags: ignoreversion
Source: "..\..\quote_app\config.py";           DestDir: "{app}\quote_app"; Flags: ignoreversion
Source: "..\..\quote_app\vendor_router.py";    DestDir: "{app}\quote_app"; Flags: ignoreversion
Source: "..\..\quote_app\st_client.py";        DestDir: "{app}\quote_app"; Flags: ignoreversion
Source: "..\..\quote_app\teams_notifier.py";   DestDir: "{app}\quote_app"; Flags: ignoreversion
Source: "..\..\quote_app\smartsheet_logger.py"; DestDir: "{app}\quote_app"; Flags: ignoreversion
Source: "..\..\quote_app\quote_processor.py";  DestDir: "{app}\quote_app"; Flags: ignoreversion
Source: "..\..\quote_app\folder_watcher.py";   DestDir: "{app}\quote_app"; Flags: ignoreversion

; Vendor parsers
Source: "..\..\quote_parsers\__init__.py";     DestDir: "{app}\quote_parsers"; Flags: ignoreversion
Source: "..\..\quote_parsers\ferguson.py";     DestDir: "{app}\quote_parsers"; Flags: ignoreversion
Source: "..\..\quote_parsers\johnstone.py";    DestDir: "{app}\quote_parsers"; Flags: ignoreversion
Source: "..\..\quote_parsers\fwwebb.py";       DestDir: "{app}\quote_parsers"; Flags: ignoreversion
Source: "..\..\quote_parsers\generic_csv.py";  DestDir: "{app}\quote_parsers"; Flags: ignoreversion

[Icons]
; Start menu shortcut
Name: "{group}\{#AppName}";        Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

; Desktop shortcut (optional — user can skip)
Name: "{autodesktop}\{#AppName}";  Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Dirs]
; Create the C:\ST\ directory so the cache database has a home on every computer
Name: "C:\ST"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Registry]
; Auto-start with Windows (current user, no admin needed)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
  ValueType: string; ValueName: "{#AppName}"; \
  ValueData: """{app}\{#AppExeName}"""; \
  Flags: uninsdeletevalue

[UninstallRun]
; Stop the app before uninstalling
Filename: "taskkill"; Parameters: "/f /im {#AppExeName}"; Flags: runhidden; RunOnceId: "KillApp"

[Code]
function IsPythonInstalled(): Boolean;
var
  PythonPath: String;
begin
  // Check registry for Python 3 (64-bit and 32-bit)
  Result := RegQueryStringValue(HKLM, 'SOFTWARE\Python\PythonCore\3.14\InstallPath', '', PythonPath)
         or RegQueryStringValue(HKLM, 'SOFTWARE\Python\PythonCore\3.13\InstallPath', '', PythonPath)
         or RegQueryStringValue(HKLM, 'SOFTWARE\Python\PythonCore\3.12\InstallPath', '', PythonPath)
         or RegQueryStringValue(HKLM, 'SOFTWARE\Python\PythonCore\3.11\InstallPath', '', PythonPath)
         or RegQueryStringValue(HKLM, 'SOFTWARE\Python\PythonCore\3.10\InstallPath', '', PythonPath)
         or RegQueryStringValue(HKLM, 'SOFTWARE\Python\PythonCore\3.9\InstallPath',  '', PythonPath)
         or RegQueryStringValue(HKCU, 'SOFTWARE\Python\PythonCore\3.14\InstallPath', '', PythonPath)
         or RegQueryStringValue(HKCU, 'SOFTWARE\Python\PythonCore\3.13\InstallPath', '', PythonPath)
         or RegQueryStringValue(HKCU, 'SOFTWARE\Python\PythonCore\3.12\InstallPath', '', PythonPath)
         or RegQueryStringValue(HKCU, 'SOFTWARE\Python\PythonCore\3.11\InstallPath', '', PythonPath)
         or RegQueryStringValue(HKCU, 'SOFTWARE\Python\PythonCore\3.10\InstallPath', '', PythonPath)
         or RegQueryStringValue(HKCU, 'SOFTWARE\Python\PythonCore\3.9\InstallPath',  '', PythonPath);
end;

function InitializeSetup(): Boolean;
var
  Answer: Integer;
begin
  if not IsPythonInstalled() then
  begin
    Answer := MsgBox('Python 3.9 or newer is required to run ST MCP Connector.'
      + #13#10 + #13#10
      + 'Click OK to open the Python download page in your browser.'
      + #13#10
      + 'Make sure to check "Add Python to PATH" during installation.'
      + #13#10 + #13#10
      + 'After installing Python, run this installer again.',
      mbConfirmation, MB_OKCANCEL);
    if Answer = IDOK then
      ShellExec('open', 'https://www.python.org/downloads/', '', '', SW_SHOWNORMAL, ewNoWait, Answer);
    Result := False;
  end else
    Result := True;
end;
