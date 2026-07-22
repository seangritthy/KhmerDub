# ════════════════════════════════════════════════════════════
#  KhmerDub — Automated Build Script
#  Builds a standalone Windows .exe with bundled FFmpeg
#  Run from the project root:  .\build_exe.ps1
# ════════════════════════════════════════════════════════════
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

function Banner($msg) {
    Write-Host ""
    Write-Host ("=" * 54) -ForegroundColor Cyan
    Write-Host "  $msg" -ForegroundColor Cyan
    Write-Host ("=" * 54) -ForegroundColor Cyan
}

function OK($msg)   { Write-Host "  ✅ $msg" -ForegroundColor Green }
function WARN($msg) { Write-Host "  ⚠️  $msg" -ForegroundColor Yellow }
function ERR($msg)  { Write-Host "  ❌ $msg" -ForegroundColor Red; exit 1 }
function INFO($msg) { Write-Host "  ℹ️  $msg" -ForegroundColor White }

# ── Step 0: Find Python ──────────────────────────────────────
Banner "Step 0 — Locating Python"

$PythonExe = $null
$candidates = @(
    "C:\Python312\python.exe",
    "C:\Python311\python.exe",
    "C:\Python310\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python39\python.exe",
    "C:\ProgramData\miniconda3\python.exe",
    "C:\ProgramData\anaconda3\python.exe",
    "$env:USERPROFILE\miniconda3\python.exe",
    "$env:USERPROFILE\anaconda3\python.exe"
)

foreach ($c in $candidates) {
    if (Test-Path $c) { $PythonExe = $c; break }
}

if (-not $PythonExe) {
    WARN "Python not found in common locations. Trying winget install..."
    try {
        winget install -e --id Python.Python.3.11 --silent `
            --accept-package-agreements --accept-source-agreements
        $PythonExe = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
        if (-not (Test-Path $PythonExe)) { throw }
        OK "Python 3.11 installed via winget."
    } catch {
        ERR "Could not install Python automatically.`nPlease install Python 3.10+ from https://www.python.org/downloads/ and re-run this script."
    }
}

$pyVer = & $PythonExe --version 2>&1
OK "Using: $pyVer  ($PythonExe)"

# ── Step 1: Upgrade pip ──────────────────────────────────────
Banner "Step 1 — Upgrading pip"
& $PythonExe -m pip install --upgrade pip --quiet
OK "pip upgraded"

# ── Step 2: Install dependencies ─────────────────────────────
Banner "Step 2 — Installing Python packages"
INFO "This may take 5-10 minutes on first run (downloading Whisper etc.)..."

$packages = @(
    "flask>=2.3.0",
    "openai-whisper>=20231117",
    "deep-translator>=1.11.4",
    "edge-tts>=6.1.9",
    "librosa>=0.10.1",
    "numpy>=1.24.0",
    "ffmpeg-python>=0.2.0",
    "werkzeug>=2.3.0",
    "yt-dlp",
    "customtkinter",
    "demucs",
    "opencv-python",
    "pillow",
    "google-generativeai",
    "pyinstaller"
)

foreach ($pkg in $packages) {
    INFO "Installing $pkg..."
    & $PythonExe -m pip install $pkg --quiet
    if ($LASTEXITCODE -ne 0) { ERR "Failed to install $pkg" }
}
OK "All packages installed"

# ── Step 3: Download FFmpeg ──────────────────────────────────
Banner "Step 3 — Bundling FFmpeg"

$FfmpegDir = Join-Path $ProjectDir "ffmpeg_bin"
$FfmpegExe = Join-Path $FfmpegDir "ffmpeg.exe"

if (Test-Path $FfmpegExe) {
    OK "FFmpeg already present — skipping download."
} else {
    New-Item -ItemType Directory -Force -Path $FfmpegDir | Out-Null
    INFO "Downloading FFmpeg essentials for Windows..."

    # Use GitHub releases for a minimal ffmpeg build
    $FfmpegUrl = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
    $ZipPath   = Join-Path $env:TEMP "ffmpeg_download.zip"
    $ExtractTo = Join-Path $env:TEMP "ffmpeg_extract"

    try {
        # Try with curl (available on Win10+)
        INFO "Downloading from GitHub (this may take a minute)..."
        curl.exe -L -o $ZipPath $FfmpegUrl --progress-bar
    } catch {
        # Fallback: Invoke-WebRequest
        Invoke-WebRequest -Uri $FfmpegUrl -OutFile $ZipPath -UseBasicParsing
    }

    INFO "Extracting FFmpeg..."
    if (Test-Path $ExtractTo) { Remove-Item $ExtractTo -Recurse -Force }
    Expand-Archive -Path $ZipPath -DestinationPath $ExtractTo -Force

    # Find ffmpeg.exe in extracted folder
    $FfmpegFound = Get-ChildItem $ExtractTo -Recurse -Filter "ffmpeg.exe" |
                   Select-Object -First 1
    if (-not $FfmpegFound) { ERR "Could not find ffmpeg.exe in downloaded archive." }

    Copy-Item $FfmpegFound.FullName -Destination $FfmpegDir
    # Also copy ffprobe if present
    $FfprobeFound = Get-ChildItem $ExtractTo -Recurse -Filter "ffprobe.exe" |
                    Select-Object -First 1
    if ($FfprobeFound) { Copy-Item $FfprobeFound.FullName -Destination $FfmpegDir }

    Remove-Item $ZipPath -Force -ErrorAction SilentlyContinue
    Remove-Item $ExtractTo -Recurse -Force -ErrorAction SilentlyContinue

    OK "FFmpeg bundled to ./ffmpeg_bin/"
}

# ── Step 4: Generate app icon (optional) ─────────────────────
Banner "Step 4 — Preparing icon"
$IconPath = Join-Path $ProjectDir "static\icon.ico"
if (-not (Test-Path $IconPath)) {
    INFO "No icon.ico found — build will use default PyInstaller icon."
} else {
    OK "Icon found: $IconPath"
}

# ── Step 5: Clean previous build ─────────────────────────────
Banner "Step 5 — Cleaning previous build"
@('build', 'dist') | ForEach-Object {
    $p = Join-Path $ProjectDir $_
    if (Test-Path $p) {
        Remove-Item $p -Recurse -Force
        INFO "Removed: $p"
    }
}
OK "Build directories cleaned"

# ── Step 6: Run PyInstaller ───────────────────────────────────
Banner "Step 6 — Building .exe with PyInstaller"
INFO "This may take 3-8 minutes..."

& $PythonExe -m PyInstaller KhmerDub.spec --noconfirm --clean

if ($LASTEXITCODE -ne 0) {
    ERR "PyInstaller build failed. Check the output above for details."
}

# ── Step 7: Verify output ─────────────────────────────────────
Banner "Step 7 — Verifying output"
$DistExe = Join-Path $ProjectDir "dist\KhmerDub.exe"
if (-not (Test-Path $DistExe)) {
    ERR "Build seems to have failed — KhmerDub.exe not found in dist\"
}

$SizeMB = [math]::Round((Get-Item $DistExe).Length / 1MB, 1)
OK "KhmerDub.exe built successfully! ($SizeMB MB)"

# ── Done ──────────────────────────────────────────────────────
Banner "🎉 BUILD COMPLETE!"
Write-Host ""
Write-Host "  Output folder: " -NoNewline
Write-Host (Join-Path $ProjectDir "dist\KhmerDub.exe") -ForegroundColor Cyan
Write-Host ""
Write-Host "  To run the app:" -ForegroundColor White
Write-Host "  .\dist\KhmerDub.exe" -ForegroundColor Green
Write-Host ""
# ── Step 8: Create ZIP archive ────────────────────────────────
Banner 'Step 8 — Creating ZIP archive for GitHub Release'
$ZipTarget = Join-Path $ProjectDir 'KhmerDub-v1.7.0-windows-standalone.zip'
if (Test-Path $ZipTarget) { Remove-Item $ZipTarget -Force }
Compress-Archive -Path (Join-Path $ProjectDir 'dist\KhmerDub.exe') -DestinationPath $ZipTarget
$ZipSizeMB = [math]::Round((Get-Item $ZipTarget).Length / 1MB, 1)
OK ('Zip created: KhmerDub-v1.7.0-windows-standalone.zip (' + $ZipSizeMB + ' MB)')

Write-Host ''
Write-Host '  To share: upload the KhmerDub-v1.7.0-windows-standalone.zip file to GitHub Releases.' -ForegroundColor White
Write-Host ''

# Ask user if they want to launch now
$launch = Read-Host '  Launch KhmerDub now? (Y/n)'
if ($launch -ne 'n' -and $launch -ne 'N') {
    Start-Process -FilePath $DistExe -WorkingDirectory (Split-Path $DistExe)
}
