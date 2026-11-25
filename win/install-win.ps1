# wlx Windows installer - no admin, no plink, no system changes.
#
# Steps:
#   1. create two dedicated ed25519 keys (tunnel + management)
#   2. install their public keys on Linux via `ssh "bash -s"` - the ONE step
#      that uses your Linux password, typed interactively, never stored
#   3. upload linux/tools/docs to ~/wlx (Windows runtime stays Windows-only)
#   4. write local conf + token (ACL-protected) and the user env var
#   5. register current-user autostart (Startup folder) and start the agent
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File win\install-win.ps1
#   (or with params: -LinuxHost h -LinuxUser u -SshPort 22)

[CmdletBinding()]
param(
    [string]$LinuxHost,
    [string]$LinuxUser,
    [int]$SshPort = 22,
    [string]$RemoteRoot = "",
    [int]$AgentPort = 8788,
    [int]$TunnelPort = 18081,
    [switch]$NoStart
)
$ErrorActionPreference = 'Stop'
$win = $PSScriptRoot
$root = Split-Path -Parent $win
$app = Join-Path $env:USERPROFILE '.wlx'
$keys = Join-Path $app 'keys'

function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  ok: $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  warn: $m" -ForegroundColor Yellow }

function Run-Exe($exe, $argline) {
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    $psi.Arguments = $argline
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $p = [Diagnostics.Process]::Start($psi)
    $out = $p.StandardOutput.ReadToEnd()
    $err = $p.StandardError.ReadToEnd()
    $p.WaitForExit()
    return @{ rc = $p.ExitCode; out = $out; err = $err }
}

function Find-Exe($name) {
    $p = Join-Path $env:SystemRoot "System32\OpenSSH\$name.exe"
    if (Test-Path -LiteralPath $p) { return $p }
    $c = Get-Command $name -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    return $null
}

$ssh = Find-Exe 'ssh'
$scp = Find-Exe 'scp'
$keygen = Find-Exe 'ssh-keygen'
if (-not $ssh -or -not $scp -or -not $keygen) {
    throw 'Windows OpenSSH client (ssh/scp/ssh-keygen) is required (Win10 1809+ built-in)'
}

if (-not $LinuxHost) { $LinuxHost = Read-Host 'Linux host (the address Windows dials to)' }
if (-not $LinuxUser) { $LinuxUser = Read-Host 'Linux user (only ~ needed, no sudo)' }
if (-not $LinuxHost -or -not $LinuxUser) { throw 'Linux host and user are required' }
$target = "$LinuxUser@$LinuxHost"
if (-not $RemoteRoot) { $RemoteRoot = '~/wlx' }

Step 'Creating dedicated keys'
New-Item -ItemType Directory -Force -Path $keys | Out-Null
$tunnelKey = Join-Path $keys 'tunnel_ed25519'
$mgmtKey = Join-Path $keys 'mgmt_ed25519'
foreach ($spec in @(@{ p = $tunnelKey; c = 'wlx-tunnel' }, @{ p = $mgmtKey; c = 'wlx-mgmt' })) {
    if (-not (Test-Path -LiteralPath $spec.p)) {
        $a = "-t ed25519 -f `"$($spec.p)`" -N `"`" -C `"$($spec.c)`" -q"
        $r = Run-Exe $keygen $a
        if (-not (Test-Path -LiteralPath $spec.p)) { throw "ssh-keygen failed: $($spec.p) $($r.err)" }
        Ok "created $($spec.p)"
    } else { Ok "exists $($spec.p)" }
}
$tunnelPub = (Get-Content -LiteralPath "$tunnelKey.pub" -Raw).Trim()
$mgmtPub = (Get-Content -LiteralPath "$mgmtKey.pub" -Raw).Trim()

Step 'Generating agent token'
$token = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
Ok 'token generated (stored ACL-protected, never echoed)'

Step 'Installing keys on Linux - type your Linux password ONCE when prompted'
$tplPath = Join-Path $root 'linux\install-linux.sh'
if (-not (Test-Path -LiteralPath $tplPath)) { throw "missing $tplPath" }
$script = (Get-Content -LiteralPath $tplPath -Raw)
$script = $script.Replace('__REMOTE_ROOT__', $RemoteRoot).Replace('__TOKEN__', $token)
$script = $script.Replace('__TUNNEL_PORT__', "$TunnelPort").Replace('__AGENT_PORT__', "$AgentPort")
$script = $script.Replace('__TUNNEL_PUB__', $tunnelPub).Replace('__MGMT_PUB__', $mgmtPub)
$script = $script.Replace("`r`n", "`n").Replace("`r", "`n")
$tmp = [IO.Path]::GetTempFileName()
[IO.File]::WriteAllText($tmp, $script, (New-Object Text.UTF8Encoding($false)))
# cmd '<' redirection feeds the LF file bytes verbatim (a PowerShell pipe would
# rewrite LF as CRLF and break bash). The password prompt still shows in console.
$cmdline = "`"$ssh`" -p $SshPort -o StrictHostKeyChecking=accept-new $target `"bash -s`" < `"$tmp`""
cmd /c $cmdline
$rc = $LASTEXITCODE
Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
if ($rc -ne 0) { throw "Linux key install failed (ssh exit $rc)" }
Ok 'keys installed on Linux'

Step 'Uploading linux/tools/docs to Linux'
$scpCommon = "-i `"$mgmtKey`" -P $SshPort -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes"
foreach ($item in @('linux', 'tools', 'docs', 'README.md', 'VERSION', 'LICENSE', 'NOTICE')) {
    $src = Join-Path $root $item
    if (-not (Test-Path -LiteralPath $src)) { Warn "skip (absent): $item"; continue }
    $a = "-r -p $scpCommon `"$src`" `"$target`:$RemoteRoot/`""
    $r = Run-Exe $scp $a
    if ($r.rc -ne 0) { Warn "upload failed: $item $($r.err)" } else { Ok "uploaded $item" }
}
# Make the CLI executable.
$null = Run-Exe $ssh "$scpCommon $target `"chmod +x $RemoteRoot/linux/wlx`""

Step 'Writing local config + token'
New-Item -ItemType Directory -Force -Path $app | Out-Null
$conf = @(
    "WLX_LINUX_HOST=$LinuxHost", "WLX_LINUX_USER=$LinuxUser", "WLX_SSH_PORT=$SshPort",
    "WLX_REMOTE_ROOT=$RemoteRoot", "WLX_AGENT_PORT=$AgentPort", "WLX_TUNNEL_PORT=$TunnelPort"
) -join "`r`n"
[IO.File]::WriteAllText((Join-Path $app 'wlx.conf'), $conf + "`r`n", (New-Object Text.UTF8Encoding($false)))
[IO.File]::WriteAllText((Join-Path $app 'agent.token'), $token, (New-Object Text.UTF8Encoding($false)))
# Restrict the token file to the current user only.
$null = Run-Exe 'icacls' "`"$app\agent.token`" /inheritance:r /grant:r `"$($env:USERNAME):(R,W)`""
[Environment]::SetEnvironmentVariable('WLX_AGENT_TOKEN', $token, 'User')
Ok 'config + token written; user env var WLX_AGENT_TOKEN set'

Step 'Registering autostart (current user)'
$startup = [Environment]::GetFolderPath('Startup')
$vbs = Join-Path $startup 'wlx.vbs'
$vbsText = "Set sh = CreateObject(`"WScript.Shell`")`r`nsh.Run `"`"`"$win\start-win.cmd`"`"`", 0, False`r`n"
[IO.File]::WriteAllText($vbs, $vbsText, (New-Object Text.UTF8Encoding($false)))
Ok "autostart: $vbs"

if (-not $NoStart) {
    Step 'Starting the agent'
    & wscript.exe (Join-Path $win 'start-win.vbs')
    Start-Sleep -Seconds 2
    Ok 'agent launched (hidden, with crash-restart loop)'
}

Write-Host ''
Write-Host 'Done. On Linux:' -ForegroundColor Cyan
Write-Host "  $RemoteRoot/linux/wlx status"
Write-Host "  $RemoteRoot/linux/wlx run `"ipconfig`""
Write-Host "  $RemoteRoot/linux/wlx tunnel add L 3080 127.0.0.1:3080"
Write-Host ''
Write-Host 'Tip: add ~/wlx/linux to PATH, or alias wlx=~/wlx/linux/wlx' -ForegroundColor Yellow