; Inno Setup script: wraps dist\CollectionSearch in a normal installer
; with a Start menu entry, optional start-at-login, and an uninstaller.
; Build with packaging\build.ps1 -Installer (needs Inno Setup 6).

#define AppName "Collection Search"
#define AppVersion "0.2.0"
#define AppExe "CollectionSearch.exe"

[Setup]
AppId={{6C1B7E0B-3E51-4C1F-9C2A-5F0B1C6A9E41}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\Collection Search
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=CollectionSearch-Setup
Compression=lzma2
SolidCompression=yes
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#AppExe}

[Files]
Source: "..\dist\CollectionSearch\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Tasks]
Name: "startup"; Description: "Start Collection Search when I sign in"; Flags: unchecked

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--tray"; Tasks: startup

[Run]
Filename: "{app}\{#AppExe}"; Description: "Open {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /IM {#AppExe} /F"; Flags: runhidden; RunOnceId: "StopApp"
