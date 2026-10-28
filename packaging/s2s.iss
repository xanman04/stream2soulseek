; Windows installer for S2S (Inno Setup 6). Built by packaging/build.py, which passes:
;   /DAppVersion=1.2.3 /DAppDir=<dist\S2S> /DOutDir=<dist> /DIconFile=<s2s.ico> /DFileList=<generated .iss>
;
; Installs for the current user only (no administrator rights) into %LOCALAPPDATA%\Programs\S2S, so the in-app
; updater can replace changed files later. The uninstaller lives in a sibling folder and removes the S2S
; installation entirely. User data (%LOCALAPPDATA%\S2S) is kept.

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
SetupLogging=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
#include FileList

[Code]
type
  TProgramFileInfo = record
    Attributes: Cardinal;
    Creation, Access, LastWrite: TFileTime;
    Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow: Cardinal;
  end;

function GetProgramFileInfo(Handle: THandle; var Info: TProgramFileInfo): Boolean;
  external 'GetFileInformationByHandle@kernel32.dll stdcall';

var
  ComparedFiles: TArrayOfString;
  ComparisonPage: TOutputProgressWizardPage;
  ComparedDir: String;
  CleanupError: String;
  CleanupSucceeded: Boolean;

procedure InitializeWizard;
begin
  SetArrayLength(ComparedFiles, {#RuntimeFileCount});
  ComparisonPage := CreateOutputProgressPage('Checking installed files',
    'Preparing the S2S installation');
end;

procedure ComparisonProgress(const S: String; const Error, FirstLine: Boolean);
begin
  if not Error and (Pos('PROGRESS ', S) = 1) then
    ComparisonPage.SetProgress(StrToIntDef(Copy(S, 10, Length(S)), 0), {#RuntimeFileCount});
end;

function RunRuntimeCleanup(Mode: String): String;
var
  ExitCode, I: Integer;
  Params, ErrorFile: String;
  Lines: TArrayOfString;
begin
  Result := '';
  ExtractTemporaryFile('s2s-runtime-layout.json');
  ExtractTemporaryFile('s2s-runtime-clean.exe');
  ErrorFile := ExpandConstant('{tmp}\s2s-cleanup-error.txt');
  DeleteFile(ErrorFile);
  Params := Mode + ' ' + AddQuotes(ExpandConstant('{app}')) + ' ' +
    AddQuotes(ExpandConstant('{localappdata}\Programs\S2S')) + ' ' +
    AddQuotes(ExpandConstant('{tmp}\s2s-runtime-layout.json')) + ' ' + AddQuotes(ErrorFile);
  if not ExecAndLogOutput(ExpandConstant('{tmp}\s2s-runtime-clean.exe'), Params, '',
    SW_SHOWNORMAL, ewWaitUntilTerminated, ExitCode, @ComparisonProgress) or (ExitCode <> 0) then begin
    Result := 'S2S could not clean up its program folder. Close S2S and run this installer again.';
    if LoadStringsFromFile(ErrorFile, Lines) then
      for I := 0 to GetArrayLength(Lines) - 1 do
        Result := Result + #13#10 + Lines[I];
    Log(Result);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  AppDir, ResultsFile, Params, Line: String;
  ExitCode, I, Index, Separator: Integer;
  Lines: TArrayOfString;
begin
  Result := '';
  AppDir := ExpandConstant('{app}');
  Result := RunRuntimeCleanup('--check');
  if Result <> '' then exit;
  if ComparedDir = AppDir then exit;
  for I := 0 to GetArrayLength(ComparedFiles) - 1 do ComparedFiles[I] := '';
  ComparedDir := AppDir;
  if not FileExists(AddBackslash(AppDir) + 'S2S.exe') then exit;
  ComparisonPage.SetText('Checking which program files need updating...', '');
  ComparisonPage.SetProgress(0, {#RuntimeFileCount});
  ComparisonPage.Show;
  try
    try
      ExtractTemporaryFile('s2s-runtime-hashes.json');
      ExtractTemporaryFile('s2s-runtime-compare.exe');
      ResultsFile := ExpandConstant('{tmp}\s2s-unchanged.txt');
      DeleteFile(ResultsFile);
      Params := AddQuotes(AppDir) + ' ' +
        AddQuotes(ExpandConstant('{tmp}\s2s-runtime-hashes.json')) + ' ' + AddQuotes(ResultsFile);
      Log('Starting program-file comparison');
      if ExecAndLogOutput(ExpandConstant('{tmp}\s2s-runtime-compare.exe'),
        Params, '', SW_SHOWNORMAL, ewWaitUntilTerminated, ExitCode, @ComparisonProgress) and
        (ExitCode = 0) and FileExists(ResultsFile) then begin
        Log('Comparison helper finished');
        if not LoadStringsFromFile(ResultsFile, Lines) then RaiseException('Missing comparison results');
        for I := 0 to GetArrayLength(Lines) - 1 do begin
          Line := Lines[I];
          Separator := Pos('|', Line);
          Index := StrToIntDef(Copy(Line, 1, Separator - 1), -1);
          if (Index < 0) or (Index >= {#RuntimeFileCount}) then
            RaiseException('Invalid comparison results');
          ComparedFiles[Index] := Copy(Line, Separator + 1, Length(Line));
        end;
        Log('Unchanged program files: ' + IntToStr(GetArrayLength(Lines)));
      end else
        Log('File comparison unavailable; installing all program files');
    except
      for I := 0 to GetArrayLength(ComparedFiles) - 1 do ComparedFiles[I] := '';
      Log('File comparison failed; installing all program files');
    end;
  finally
    ComparisonPage.Hide;
  end;
end;

function NeedsUpdate(FileIndex: Integer; RelativePath: String): Boolean;
var
  Info: TProgramFileInfo;
  Stream: TFileStream;
  Mark: String;
begin
  { Early wizard sizing checks stay cheap. Hashes are computed once during PrepareToInstall. }
  Result := True;
  if GetArrayLength(ComparedFiles) <> {#RuntimeFileCount} then exit;
  if ComparedDir <> ExpandConstant('{app}') then exit;
  if ComparedFiles[FileIndex] = '' then exit;
  { A file changed after hashing must be copied, even if it was initially identical. }
  try
    Stream := TFileStream.Create(AddBackslash(ExpandConstant('{app}')) + RelativePath,
                                fmOpenRead or fmShareDenyWrite);
    try
      if GetProgramFileInfo(Stream.Handle, Info) then begin
        Mark := IntToStr(Info.SizeHigh) + '|' + IntToStr(Info.SizeLow) + '|' +
          IntToStr(Info.LastWrite.dwHighDateTime) + '|' + IntToStr(Info.LastWrite.dwLowDateTime);
        Result := Mark <> ComparedFiles[FileIndex];
      end;
    finally
      Stream.Free;
    end;
  except
    Result := True;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then begin
    ComparisonPage.SetText('Removing obsolete program files...', 'Keeping the files included in this release.');
    ComparisonPage.Show;
    try
      try
        CleanupError := RunRuntimeCleanup('--clean');
      except
        CleanupError := 'S2S cleanup could not finish. Close S2S and run this installer again.';
        Log(CleanupError);
      end;
      CleanupSucceeded := CleanupError = '';
    finally
      ComparisonPage.Hide;
    end;
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and (CleanupError <> '') then begin
    WizardForm.FinishedHeadingLabel.Caption := 'Installation needs attention';
    WizardForm.FinishedLabel.Caption := CleanupError;
  end;
end;

function GetCustomSetupExitCode: Integer;
begin
  Result := 0;
  if CleanupError <> '' then Result := 1;
end;

function CanLaunchCleanInstall: Boolean;
begin
  Result := CleanupSucceeded;
end;

[Icons]
Name: "{autoprograms}\S2S"; Filename: "{app}\S2S.exe"
Name: "{autodesktop}\S2S"; Filename: "{app}\S2S.exe"; Tasks: desktopicon

[Run]
; Launch this executable directly in its own folder; no shell file-association lookup is needed.
Filename: "{app}\S2S.exe"; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,S2S}"; Flags: nowait postinstall skipifsilent; Check: CanLaunchCleanInstall

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
