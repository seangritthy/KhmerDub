# KhmerDub Setup Script for Windows
# Run this in PowerShell as Administrator

Write-Host "=== KhmerDub Setup ===" -ForegroundColor Cyan

# 1. Check for Winget / Chocolatey / manual Python
$pythonPath = $null

# Try common install locations
$candidates = @(
    "C:\Python312\python.exe",
    "C:\Python311\python.exe",
    "C:\Python310\python.exe",
    "C:\Users\$env:USERNAME\AppData\Local\Programs\Python\Python312\python.exe",
    "C:\Users\$env:USERNAME\AppData\Local\Programs\Python\Python311\python.exe",
    "C:\Users\$env:USERNAME\AppData\Local\Programs\Python\Python310\python.exe",
    "C:\ProgramData\miniconda3\python.exe",
    "C:\ProgramData\anaconda3\python.exe"
)

foreach ($c in $candidates) {
    if (Test-Path $c) { $pythonPath = $c; break }
}

if (-not $pythonPath) {
    Write-Host ""
    Write-Host "Python not found. Installing via winget..." -ForegroundColor Yellow
    winget install -e --id Python.Python.3.11 --silent --accept-package-agreements --accept-source-agreements
    $pythonPath = "C:\Users\$env:USERNAME\AppData\Local\Programs\Python\Python311\python.exe"
    if (-not (Test-Path $pythonPath)) {
        Write-Host "Please install Python 3.10+ from https://python.org and re-run this script." -ForegroundColor Red
        exit 1
    }
}

Write-Host "Using Python: $pythonPath" -ForegroundColor Green

# 2. Install pip packages
Write-Host "`nInstalling Python packages..." -ForegroundColor Cyan
& $pythonPath -m pip install --upgrade pip
& $pythonPath -m pip install flask openai-whisper deep-translator edge-tts librosa numpy werkzeug

# 3. Check FFmpeg
$ffmpegOk = $null -ne (Get-Command ffmpeg -ErrorAction SilentlyContinue)
if (-not $ffmpegOk) {
    Write-Host "`nFFmpeg not found. Installing via winget..." -ForegroundColor Yellow
    winget install -e --id Gyan.FFmpeg --silent --accept-package-agreements --accept-source-agreements
    $env:PATH += ";C:\Program Files\FFmpeg\bin"
    [Environment]::SetEnvironmentVariable("PATH", $env:PATH + ";C:\Program Files\FFmpeg\bin", "User")
    Write-Host "FFmpeg installed. You may need to restart your terminal." -ForegroundColor Green
} else {
    Write-Host "FFmpeg is already installed." -ForegroundColor Green
}

# 4. Launch the app
Write-Host "`n=== Setup Complete! Launching KhmerDub ===" -ForegroundColor Green
Write-Host "Open your browser at: http://localhost:5000" -ForegroundColor Cyan
Write-Host ""
& $pythonPath app.py
