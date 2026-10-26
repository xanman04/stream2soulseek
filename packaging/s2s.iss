; Windows installer for S2S (Inno Setup 6). Built by packaging/build.py, which passes:
;   /DAppVersion=1.2.3 /DAppDir=<dist\S2S> /DOutDir=<dist> /DIconFile=<s2s.ico>
;
; Installs for the current user only (no administrator rights) into %LOCALAPPDATA%\Programs\S2S, so the in-app
; updater can swap that folder later. The uninstaller lives in a sibling folder, because an in-app update
; replaces the whole S2S folder; it removes the S2S folder entirely. User data (%LOCALAPPDATA%\S2S) is kept.

#ifndef AppVersion
  #error AppVersion is required
#endif

[Setup]
AppId={{6B2F4E1A-93C7-4D58-A1E2-7F0C5D3B9A64}
AppName=S2S
AppVersion={#AppVersion}
AppVerName=S2S {#AppVersion}
AppPublisher=S2S
AppPublisherURL=https://github.com/xanman04/stream2soulseek
AppSupportURL=https://github.com/xanman04/stream2soulseek/issues
DefaultDirName={localappdata}\Programs\S2S
UninstallFilesDir={localappdata}\Programs\S2S Uninstall
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#OutDir}
OutputBaseFilename=S2S-{#AppVersion}-windows-x64-setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\S2S.exe
UninstallDisplayName=S2S
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; files of an older version that a newer one no longer ships
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#AppDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\S2S"; Filename: "{app}\S2S.exe"
Name: "{autodesktop}\S2S"; Filename: "{app}\S2S.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\S2S.exe"; Description: "{cm:LaunchProgram,S2S}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
