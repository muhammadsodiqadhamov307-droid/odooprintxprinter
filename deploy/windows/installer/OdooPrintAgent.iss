; Inno Setup script for Odoo Custom Print Agent
; Build with: iscc deploy\windows\installer\OdooPrintAgent.iss

#define MyAppName "Odoo Custom Print Agent"
#define MyAppVersion "1.0.9"
#define MyAppPublisher "Your Company"
#define MyAppExeName "print_agent.py"

[Setup]
AppId={{C54172AA-5A17-4599-B0D7-98BC90D29A22}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\OdooPrintAgent
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=.
OutputBaseFilename=OdooPrintAgentSetup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
UninstallDisplayIcon={app}\deploy\windows\assets\app_logo.ico
SetupIconFile=..\assets\app_logo.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: checkedonce

[Files]
Source: "..\..\..\print_agent.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\..\agent_manager.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\..\requirements.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\install_service.ps1"; DestDir: "{app}\deploy\windows"; Flags: ignoreversion
Source: "..\uninstall_service.ps1"; DestDir: "{app}\deploy\windows"; Flags: ignoreversion
Source: "..\restart_service.ps1"; DestDir: "{app}\deploy\windows"; Flags: ignoreversion
Source: "..\monitor_logs.ps1"; DestDir: "{app}\deploy\windows"; Flags: ignoreversion
Source: "..\run_manager.ps1"; DestDir: "{app}\deploy\windows"; Flags: ignoreversion
Source: "..\run_manager.vbs"; DestDir: "{app}\deploy\windows"; Flags: ignoreversion
Source: "..\tools\nssm.exe"; DestDir: "{app}\deploy\windows\tools"; Flags: ignoreversion
Source: "..\assets\app_logo.ico"; DestDir: "{app}\deploy\windows\assets"; Flags: ignoreversion
Source: "..\assets\app_logo.png"; DestDir: "{app}\deploy\windows\assets"; Flags: ignoreversion
Source: "..\..\..\pos_custom_print\*"; DestDir: "{app}\pos_custom_print"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__\*,*.pyc,*.pyo"

[Icons]
Name: "{group}\Odoo Print Agent Manager"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\deploy\windows\run_manager.vbs"" ""{app}"""; WorkingDir: "{app}"; IconFilename: "{app}\deploy\windows\assets\app_logo.ico"
Name: "{autodesktop}\Odoo Print Agent Manager"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\deploy\windows\run_manager.vbs"" ""{app}"""; WorkingDir: "{app}"; Tasks: desktopicon; IconFilename: "{app}\deploy\windows\assets\app_logo.ico"

[Run]
Filename: "powershell.exe"; \
  Parameters: "-ExecutionPolicy Bypass -File ""{app}\deploy\windows\install_service.ps1"" -ServiceName ""OdooPrintAgent"" -ProjectRoot ""{app}"" -AutoInstallPython"; \
  StatusMsg: "Installing and starting Odoo Print Agent service..."; \
  Flags: runhidden waituntilterminated

[UninstallRun]
Filename: "powershell.exe"; \
  Parameters: "-ExecutionPolicy Bypass -File ""{app}\deploy\windows\uninstall_service.ps1"" -ServiceName ""OdooPrintAgent"" -NssmPath ""{app}\deploy\windows\tools\nssm.exe"" -RemoveShortcuts"; \
  RunOnceId: "uninstall_odoo_print_agent_service"; \
  Flags: runhidden waituntilterminated
