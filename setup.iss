[Setup]
AppName=BongbeeAI dub
AppVersion=1.0
AppPublisher=Rithy Seang
DefaultDirName={autopf}\BongbeeAI dub
DefaultGroupName=BongbeeAI dub
DisableProgramGroupPage=yes
OutputBaseFilename=BongbeeAI_dub_Installer_v1.0
Compression=lzma2/ultra64
SolidCompression=yes
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\BongbeeAI_dub.exe

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
Source: "dist\BongbeeAI_dub\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\BongbeeAI dub"; Filename: "{app}\BongbeeAI_dub.exe"
Name: "{autodesktop}\BongbeeAI dub"; Filename: "{app}\BongbeeAI_dub.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\BongbeeAI_dub.exe"; Description: "{cm:LaunchProgram,BongbeeAI dub}"; Flags: nowait postinstall skipifsilent
