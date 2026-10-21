$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
function Check-Exit { if ($LASTEXITCODE -ne 0) { throw "Command failed with exit code $LASTEXITCODE" } }
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py -3.12 -m venv .venv
    Check-Exit
}
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $python -m pip install -r requirements.txt "pyinstaller==6.16.0"
Check-Exit
$env:PLAYWRIGHT_BROWSERS_PATH = "0"
& $python -m playwright install chromium
Check-Exit
& $python -m PyInstaller --noconfirm --clean --onedir --windowed --name SEGIOArchive --collect-all playwright --collect-all bs4 --collect-all paramiko launch.py
Check-Exit
Copy-Item README.md dist\SEGIOArchive\README.md
Copy-Item docs dist\SEGIOArchive\docs -Recurse
New-Item -ItemType Directory -Force -Path dist\SEGIOArchive\tools | Out-Null
Get-ChildItem tools -File | Copy-Item -Destination dist\SEGIOArchive\tools
$version = (& $python -c "from segio_archive import __version__; print(__version__)").Trim()
Check-Exit
if ($version -notmatch '^\d+\.\d+\.\d+(?:[A-Za-z0-9.-]*)$') { throw "Invalid program version: $version" }
$archivePath = "dist\segio-web-$version.zip"
Compress-Archive -Path dist\SEGIOArchive -DestinationPath $archivePath -Force
Write-Host "Built $archivePath. Keep the complete folder beside SEGIOArchive.exe."
