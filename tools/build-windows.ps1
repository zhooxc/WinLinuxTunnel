# Build the Windows app: PyInstaller onedir + a one-file installer.
#
# The resident program is dist\WinLinuxTunnel\WinLinuxTunnel.exe (a directory
# layout). dist\WinLinuxTunnel-Setup.exe is an install-time wrapper: it copies
# that directory into %LOCALAPPDATA%\WinLinuxTunnel and creates shortcuts.
# It is not the process users leave running.
#
# Requirements on the build machine (Windows 10 1809+):
#   * Python 3.8+ from python.org (includes Tcl/Tk). The embeddable zip does
#     not include Tk and will fail the check below.
#   * No admin rights. OpenSSH is not required to build.
#
# Usage (from anywhere):
#   powershell -ExecutionPolicy Bypass -File tools\build-windows.ps1
#
# This script does not run in CI on Linux. Shipping it, the spec, and
# wlt_main.py is enough for a Windows developer to produce the exe.
# ASCII comments only.

[CmdletBinding()]
param(
    [switch]$SkipVenv
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

function Say { param($T) Write-Host "[build] $T" }
function Fail { param($T) Write-Host "[build] ERROR $T" -ForegroundColor Red; exit 1 }

if ($env:OS -ne 'Windows_NT') {
    Fail 'tools\build-windows.ps1 must run on Windows (PyInstaller builds a Win32 exe).'
}

function Find-Python {
    foreach ($c in @(
            @{ Exe = 'py'; Args = @('-3') },
            @{ Exe = 'python'; Args = @() },
            @{ Exe = 'python3'; Args = @() }
        )) {
        if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $out = & $c.Exe @($c.Args + @('-c', 'import sys,tkinter; print(sys.executable); print(sys.version.split()[0])'))
            if ($LASTEXITCODE -eq 0 -and $out) {
                return @{ Exe = ($out | Select-Object -First 1).Trim(); Version = ($out | Select-Object -Last 1).Trim() }
            }
        } catch { }
    }
    return $null
}

$found = Find-Python
if (-not $found) {
    Fail 'Python 3.8+ with tkinter is required. Install the python.org CPython (not the embeddable package).'
}
Say ("python          : {0} ({1})" -f $found.Exe, $found.Version)

$venvPy = Join-Path $root '.venv-build\Scripts\python.exe'
if (-not $SkipVenv) {
    if (-not (Test-Path -LiteralPath $venvPy)) {
        Say 'creating .venv-build'
        & $found.Exe -m venv (Join-Path $root '.venv-build')
        if ($LASTEXITCODE -ne 0) { Fail 'venv creation failed' }
    }
    Say 'installing PyInstaller'
    & $venvPy -m pip install --upgrade pip
    & $venvPy -m pip install -r (Join-Path $root 'tools\requirements-build.txt')
    if ($LASTEXITCODE -ne 0) { Fail 'pip install pyinstaller failed' }
    $py = $venvPy
} else {
    $py = $found.Exe
}

$spec = Join-Path $root 'tools\WinLinuxTunnel.spec'
Say 'PyInstaller onedir (resident app)'
& $py -m PyInstaller --noconfirm --clean $spec --distpath (Join-Path $root 'dist') --workpath (Join-Path $root 'build\pyi-app')
if ($LASTEXITCODE -ne 0) { Fail 'PyInstaller onedir build failed' }

$onedir = Join-Path $root 'dist\WinLinuxTunnel'
$exe = Join-Path $onedir 'WinLinuxTunnel.exe'
if (-not (Test-Path -LiteralPath $exe)) { Fail "missing $exe" }

$payload = Join-Path $root 'dist\payload.zip'
Say 'staging payload zip'
& $py (Join-Path $root 'tools\wlt_payload.py') --onedir $onedir --zip $payload --root $root
if ($LASTEXITCODE -ne 0) { Fail 'payload staging failed' }

Say 'PyInstaller onefile installer (install-time only)'
$setupWork = Join-Path $root 'build\pyi-setup'
New-Item -ItemType Directory -Force -Path $setupWork | Out-Null
& $py -m PyInstaller --noconfirm --clean --onefile --console `
    --name WinLinuxTunnel-Setup `
    --distpath (Join-Path $root 'dist') `
    --workpath $setupWork `
    --specpath $setupWork `
    --add-data "$payload;." `
    (Join-Path $root 'tools\wlt_setup.py')
if ($LASTEXITCODE -ne 0) { Fail 'installer build failed' }

$setup = Join-Path $root 'dist\WinLinuxTunnel-Setup.exe'
if (-not (Test-Path -LiteralPath $setup)) { Fail "missing $setup" }

$onedirSize = (Get-ChildItem -LiteralPath $onedir -Recurse -File | Measure-Object -Sum Length).Sum
$setupSize = (Get-Item -LiteralPath $setup).Length
Say ("onedir          : {0:N1} MB  {1}" -f ($onedirSize / 1MB), $onedir)
Say ("installer       : {0:N1} MB  {1}" -f ($setupSize / 1MB), $setup)
Say 'done. Users run WinLinuxTunnel-Setup.exe once, then the Start Menu shortcut.'
