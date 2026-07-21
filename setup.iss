[Setup]
AppName=KhmerDub
AppVersion=1.6.1
AppPublisher=Rithy Seang
DefaultDirName={autopf}\KhmerDub
DefaultGroupName=KhmerDub
DisableProgramGroupPage=yes
OutputBaseFilename=KhmerDub_Installer_v1.6.1
Compression=lzma2/ultra64
SolidCompression=yes
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\KhmerDub.exe

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
Source: "dist\KhmerDub\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\KhmerDub"; Filename: "{app}\KhmerDub.exe"
Name: "{autodesktop}\KhmerDub"; Filename: "{app}\KhmerDub.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\KhmerDub.exe"; Description: "{cm:LaunchProgram,KhmerDub}"; Flags: nowait postinstall skipifsilent
