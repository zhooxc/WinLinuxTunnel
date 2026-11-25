# wlt - build the self-contained Python runtime shipped in win\runtime.
#
# The end user needs no Python, pip or PuTTY. This produces a portable
# interpreter with working tkinter from two public sources:
#   * python.org embeddable package (interpreter + stdlib + ctypes)
#   * a full CPython install's tcl/tk runtime + _tkinter extension, which the
#     embeddable package deliberately omits
#
# The Tk donor defaults to whatever CPython 3.13 is on this machine. The
# embeddable version auto-matches the donor's major.minor.patch.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File tools\build-runtime.ps1
#   powershell ... -ProductVersion 3.13.9 -TkinterFrom 'C:\Python313'
# ASCII comments only.

param(
    [string]$ProductVersion = '',
    [string]$TkinterFrom = '',
    [string]$OutDir = '',
    [switch]$Force
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = Split-Path -Parent $PSScriptRoot
if (-not $OutDir) { $OutDir = Join-Path $root 'win\runtime' }
$cache = Join-Path $env:USERPROFILE '.wlt\cache'
New-Item -ItemType Directory -Path $cache -Force | Out-Null

function Say { param($T) Write-Host "[build] $T" }
function Fail { param($T) Write-Host "[build] ERROR $T" -ForegroundColor Red; exit 1 }

if ((Test-Path -LiteralPath (Join-Path $OutDir 'python.exe')) -and -not $Force) {
    Say 'runtime already present (use -Force to rebuild)'; exit 0
}

# ---- 1. find the full install that donates tcl/tk ------------------------- #
$donorVersion = ''
if (-not $TkinterFrom) {
    foreach ($p in @(@{ Exe = 'py'; Args = @('-3') }, @{ Exe = 'python'; Args = @() }, @{ Exe = 'python3'; Args = @() })) {
        if (-not (Get-Command $p.Exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $out = & $p.Exe @($p.Args + @('-c', 'import sys,os,tkinter;print(sys.base_prefix);print(os.path.dirname(os.path.dirname(tkinter.__file__)));print(sys.version.split()[0])')) 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) {
                $TkinterFrom = ($out | Select-Object -First 1).Trim()
                $tkinterDir = ($out | Select-Object -Index 1).Trim()
                $donorVersion = ($out | Select-Object -Last 1).Trim()
                Say "tcl/tk source   : $TkinterFrom (CPython $donorVersion)"
                break
            }
        } catch { }
    }
}
if (-not $TkinterFrom) { Fail 'no CPython with tkinter found. Install Python 3.13 and rerun, or pass -TkinterFrom <prefix>.' }
if (-not $tkinterDir) { $tkinterDir = Join-Path $TkinterFrom 'Lib' }
if (-not $ProductVersion) { $ProductVersion = $donorVersion }
if (-not $ProductVersion) { Fail 'cannot determine embeddable version; pass -ProductVersion' }

foreach ($rel in @('DLLs\_tkinter.pyd', 'DLLs\tcl86t.dll', 'DLLs\tk86t.dll')) {
    if (-not (Test-Path -LiteralPath (Join-Path $TkinterFrom $rel))) { Fail "missing dependency: $rel" }
}
$tclLib = Join-Path $TkinterFrom 'tcl\tcl8.6'
$tkLib = Join-Path $TkinterFrom 'tcl\tk8.6'
if (-not (Test-Path -LiteralPath $tclLib)) { Fail "missing Tcl script library: $tclLib" }
if (-not (Test-Path -LiteralPath $tkLib)) { Fail "missing Tk script library: $tkLib" }

# ---- 2. download + extract the embeddable interpreter --------------------- #
$zipName = "python-$ProductVersion-embed-amd64.zip"
$zipPath = Join-Path $cache $zipName
if (-not (Test-Path -LiteralPath $zipPath)) {
    $url = "https://www.python.org/ftp/python/$ProductVersion/$zipName"
    Say "downloading $url"
    try { Invoke-WebRequest -Uri $url -OutFile $zipPath -TimeoutSec 300 -UseBasicParsing }
    catch { Fail "download failed: $($_.Exception.Message) (check version, or drop the zip into $cache)" }
}
Say ("embeddable zip  : {0:N1} MB" -f ((Get-Item $zipPath).Length / 1MB))
if (Test-Path -LiteralPath $OutDir) { Remove-Item -LiteralPath $OutDir -Recurse -Force }
New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
Expand-Archive -Path $zipPath -DestinationPath $OutDir -Force
Say 'extracted embeddable package'

# ---- 3. graft tkinter ----------------------------------------------------- #
foreach ($f in @('_tkinter.pyd', 'tcl86t.dll', 'tk86t.dll')) {
    Copy-Item -LiteralPath (Join-Path $TkinterFrom "DLLs\$f") -Destination $OutDir -Force
}
$zlib = Join-Path $TkinterFrom 'DLLs\zlib1.dll'
if (Test-Path -LiteralPath $zlib) { Copy-Item -LiteralPath $zlib -Destination $OutDir -Force }
New-Item -ItemType Directory -Path (Join-Path $OutDir 'tcl') -Force | Out-Null
Copy-Item -LiteralPath $tclLib -Destination (Join-Path $OutDir 'tcl\tcl8.6') -Recurse -Force
Copy-Item -LiteralPath $tkLib -Destination (Join-Path $OutDir 'tcl\tk8.6') -Recurse -Force
New-Item -ItemType Directory -Path (Join-Path $OutDir 'lib') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $tkinterDir 'tkinter') -Destination (Join-Path $OutDir 'lib\tkinter') -Recurse -Force
Say 'grafted tkinter'

$pth = Get-ChildItem -LiteralPath $OutDir -Filter '*._pth' | Select-Object -First 1
if (-not $pth) { Fail 'no ._pth file in embeddable package' }
$zipInner = (Get-ChildItem -LiteralPath $OutDir -Filter 'python*.zip' | Select-Object -First 1).Name
$pthLines = @($zipInner, '.', 'lib', '# import site intentionally off: no pip, stdlib only')
[IO.File]::WriteAllLines($pth.FullName, $pthLines, (New-Object Text.ASCIIEncoding))
Say "wrote $($pth.Name)"

# ---- 4. self test: real Tk window ----------------------------------------- #
$exe = Join-Path $OutDir 'python.exe'
$test = @(
    'import sys, os',
    'root = os.path.dirname(sys.executable)',
    'os.add_dll_directory(root)',
    'os.environ.setdefault("TCL_LIBRARY", os.path.join(root, "tcl", "tcl8.6"))',
    'os.environ.setdefault("TK_LIBRARY", os.path.join(root, "tcl", "tk8.6"))',
    'import tkinter',
    'r = tkinter.Tk(); r.withdraw()',
    'info = r.tk.call("info", "patchlevel")',
    'r.destroy()',
    'import ctypes, json, urllib.request, http.server, zlib',
    'print("TK=" + str(tkinter.TkVersion) + " PATCH=" + info + " PY=" + sys.version.split()[0])'
) -join "`n"
$testFile = Join-Path $env:TEMP ("wl_rt_test_" + [guid]::NewGuid().ToString('N') + ".py")
[IO.File]::WriteAllText($testFile, $test, (New-Object Text.UTF8Encoding($false)))
try {
    $result = & $exe $testFile 2>&1
    if ($LASTEXITCODE -ne 0) { Fail "runtime self test failed: $(($result | Out-String).Trim())" }
    Say "self test       : $(($result | Out-String).Trim())"
} finally { Remove-Item -LiteralPath $testFile -Force -ErrorAction SilentlyContinue }

$lic = Join-Path $TkinterFrom 'LICENSE.txt'
if (Test-Path -LiteralPath $lic) { Copy-Item -LiteralPath $lic -Destination (Join-Path $OutDir 'LICENSE-Python.txt') -Force }
$size = (Get-ChildItem -LiteralPath $OutDir -Recurse -File | Measure-Object -Sum Length).Sum
Say ("runtime ready   : {0:N1} MB in {1}" -f ($size / 1MB), $OutDir)