# wlt Windows installer - no admin, no plink, no system changes.
#
# Steps:
#   1. create two dedicated ed25519 keys (tunnel + management)
#   2. install their public keys on Linux via `ssh "bash -s"` - the ONE step
#      that uses your Linux password, typed interactively, never stored
#   3. upload linux/docs to ~/wlt (the Windows app stays on Windows)
#   4. write local conf + token (ACL-protected) and the user env var
#   5. register current-user autostart of WinLinuxTunnel.exe --agent
#      (dev: pythonw.exe win\wlt_main.py --agent). No .vbs / .cmd wrapper.
#   6. restart the resident agent so it reloads the new conf and token
#
# The GUI calls this with -LinuxHost/-LinuxUser already filled in. The
# console is only here so ssh can ask for the Linux password.
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
$here = $PSScriptRoot
# Source tree: this script lives in win\ and linux\ is one level up.
# Installed app: build-windows.ps1 copies this script next to linux\ and the exe.
if (Test-Path -LiteralPath (Join-Path $here 'linux\wlt')) {
    $root = $here
} else {
    $root = Split-Path -Parent $here
}
if (-not (Test-Path -LiteralPath (Join-Path $root 'linux\wlt'))) {
    throw "cannot find linux\wlt (looked in $root)"
}
$app = Join-Path $env:USERPROFILE '.wlt'
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

function Find-AppExe {
    # Only the copy sitting next to this script. A source-tree install must
    # not latch onto an older dist\ or LocalAppData build.
    $beside = Join-Path $here 'WinLinuxTunnel.exe'
    if (Test-Path -LiteralPath $beside) { return (Resolve-Path -LiteralPath $beside).Path }
    return $null
}

function Find-Pythonw {
    $cmd = Get-Command pythonw -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $py = Get-Command python -ErrorAction SilentlyContinue
    if ($py) {
        $sibling = Join-Path (Split-Path -Parent $py.Source) 'pythonw.exe'
        if (Test-Path -LiteralPath $sibling) { return $sibling }
        return $py.Source
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        try {
            $exe = (& py -3 -c "import sys; print(sys.executable)").Trim()
            if ($exe) {
                $sibling = Join-Path (Split-Path -Parent $exe) 'pythonw.exe'
                if (Test-Path -LiteralPath $sibling) { return $sibling }
                return $exe
            }
        } catch { }
    }
    return $null
}

function Test-PortOpen([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $wait = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        $ok = $wait.AsyncWaitHandle.WaitOne(300, $false)
        if (-not $ok) { return $false }
        $client.EndConnect($wait)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Stop-WltAgent([string]$Token, [int]$Port) {
    # The running agent still has the previous token in memory. Ask it to
    # exit cleanly (it closes its ssh children) before we start a new one
    # that reads the token file we just wrote. If it is listening on a
    # different port than the one we are about to configure, the HTTP call
    # misses and the pid file below is the fallback.
    if ($Token -and (Test-PortOpen $Port)) {
        try {
            $headers = @{ Authorization = "Bearer $Token" }
            Invoke-WebRequest -Method POST -Uri "http://127.0.0.1:$Port/shutdown" `
                -Headers $headers -UseBasicParsing -TimeoutSec 3 | Out-Null
        } catch { }
        $deadline = (Get-Date).AddSeconds(6)
        while ((Get-Date) -lt $deadline -and (Test-PortOpen $Port)) {
            Start-Sleep -Milliseconds 200
        }
    }
    $pidFile = Join-Path $app 'agent.pid'
    if (-not (Test-Path -LiteralPath $pidFile)) { return }
    $agentPid = 0
    $text = (Get-Content -LiteralPath $pidFile -TotalCount 1).Trim()
    [void][int]::TryParse($text, [ref]$agentPid)
    if ($agentPid -le 0) { return }
    $cli = ''
    try {
        $cli = (Get-CimInstance Win32_Process -Filter "ProcessId=$agentPid" -ErrorAction Stop).CommandLine
    } catch { return }
    if ($cli -match 'wlt_main|wlt_agent|--agent|WinLinuxTunnel') {
        Stop-Process -Id $agentPid -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 400
    }
}

function Register-Autostart([string]$ExePath, [string]$Pythonw) {
    $startup = [Environment]::GetFolderPath('Startup')
    $legacy = Join-Path $startup 'wlt.vbs'
    if (Test-Path -LiteralPath $legacy) { Remove-Item -LiteralPath $legacy -Force }
    $lnk = Join-Path $startup 'WinLinuxTunnel.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($lnk)
    if ($ExePath) {
        $sc.TargetPath = $ExePath
        $sc.Arguments = '--agent'
        $sc.WorkingDirectory = Split-Path -Parent $ExePath
    } else {
        if (-not $Pythonw) { throw 'Python 3.8+ is required (pythonw.exe) to register autostart from source' }
        $entry = Join-Path $here 'wlt_main.py'
        $sc.TargetPath = $Pythonw
        $sc.Arguments = '"' + $entry + '" --agent'
        $sc.WorkingDirectory = $here
    }
    $sc.WindowStyle = 7
    $sc.Description = 'WinLinuxTunnel agent'
    $sc.Save()
    return $lnk
}

function Start-WltAgent([string]$ExePath, [string]$Pythonw) {
    if ($ExePath) {
        Start-Process -FilePath $ExePath -ArgumentList '--agent' `
            -WorkingDirectory (Split-Path -Parent $ExePath) -WindowStyle Hidden
        return
    }
    if (-not $Pythonw) { throw 'Python 3.8+ is required to start the agent from source' }
    $entry = Join-Path $here 'wlt_main.py'
    Start-Process -FilePath $Pythonw -ArgumentList ('"' + $entry + '" --agent') `
        -WorkingDirectory $here -WindowStyle Hidden
}

$ssh = Find-Exe 'ssh'
$scp = Find-Exe 'scp'
$keygen = Find-Exe 'ssh-keygen'
if (-not $ssh -or -not $scp -or -not $keygen) {
    throw 'Windows OpenSSH client (ssh/scp/ssh-keygen) is required (Win10 1809+ built-in)'
}

$oldToken = ''
$oldTokenPath = Join-Path $app 'agent.token'
if (Test-Path -LiteralPath $oldTokenPath) {
    $oldToken = (Get-Content -LiteralPath $oldTokenPath -Raw).Trim()
}

if (-not $LinuxHost) { $LinuxHost = Read-Host 'Linux host (the address Windows dials to)' }
if (-not $LinuxUser) { $LinuxUser = Read-Host 'Linux user (only ~ needed, no sudo)' }
if (-not $LinuxHost -or -not $LinuxUser) { throw 'Linux host and user are required' }
$target = "$LinuxUser@$LinuxHost"
if (-not $RemoteRoot) { $RemoteRoot = '~/wlt' }

Step 'Creating dedicated keys'
New-Item -ItemType Directory -Force -Path $keys | Out-Null
$tunnelKey = Join-Path $keys 'tunnel_ed25519'
$mgmtKey = Join-Path $keys 'mgmt_ed25519'
foreach ($spec in @(@{ p = $tunnelKey; c = 'wlt-tunnel' }, @{ p = $mgmtKey; c = 'wlt-mgmt' })) {
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

Step 'Uploading linux/docs to Linux'
$scpCommon = "-i `"$mgmtKey`" -P $SshPort -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes"
foreach ($item in @('linux', 'tools', 'docs', 'README.md', 'VERSION', 'LICENSE', 'NOTICE')) {
    $src = Join-Path $root $item
    if (-not (Test-Path -LiteralPath $src)) { Warn "skip (absent): $item"; continue }
    $a = "-r -p $scpCommon `"$src`" `"$target`:$RemoteRoot/`""
    $r = Run-Exe $scp $a
    if ($r.rc -ne 0) { Warn "upload failed: $item $($r.err)" } else { Ok "uploaded $item" }
}
# Make the CLI executable.
$null = Run-Exe $ssh "$scpCommon $target `"chmod +x $RemoteRoot/linux/wlt`""

Step 'Writing local config + token'
New-Item -ItemType Directory -Force -Path $app | Out-Null
$conf = @(
    "WLT_LINUX_HOST=$LinuxHost", "WLT_LINUX_USER=$LinuxUser", "WLT_SSH_PORT=$SshPort",
    "WLT_REMOTE_ROOT=$RemoteRoot", "WLT_AGENT_PORT=$AgentPort", "WLT_TUNNEL_PORT=$TunnelPort"
) -join "`r`n"
[IO.File]::WriteAllText((Join-Path $app 'wlt.conf'), $conf + "`r`n", (New-Object Text.UTF8Encoding($false)))
[IO.File]::WriteAllText((Join-Path $app 'agent.token'), $token, (New-Object Text.UTF8Encoding($false)))
# Restrict the token file to the current user only.
$null = Run-Exe 'icacls' "`"$app\agent.token`" /inheritance:r /grant:r `"$($env:USERNAME):(R,W)`""
[Environment]::SetEnvironmentVariable('WLT_AGENT_TOKEN', $token, 'User')
# This process's environment, so the agent we start below sees the new token
# (a user-env write does not update processes that are already running).
$env:WLT_AGENT_TOKEN = $token
Ok 'config + token written; user env var WLT_AGENT_TOKEN set'

$appExe = Find-AppExe
$pythonw = $null
if (-not $appExe) { $pythonw = Find-Pythonw }

Step 'Restarting the agent so it loads the new config'
Stop-WltAgent -Token $oldToken -Port $AgentPort

Step 'Registering autostart (current user)'
$lnk = Register-Autostart -ExePath $appExe -Pythonw $pythonw
if ($appExe) { Ok "autostart: $lnk -> $appExe --agent" }
else { Ok "autostart: $lnk -> pythonw wlt_main.py --agent (dev)" }

if (-not $NoStart) {
    Step 'Starting the agent'
    Start-WltAgent -ExePath $appExe -Pythonw $pythonw
    Start-Sleep -Seconds 1
    if (Test-PortOpen $AgentPort) { Ok 'agent is listening' }
    else { Warn 'agent port not open yet; the GUI will start it, or see %USERPROFILE%\.wlt\logs\' }
}

Write-Host ''
Write-Host 'Done. On Linux:' -ForegroundColor Cyan
Write-Host "  $RemoteRoot/linux/wlt status"
Write-Host "  $RemoteRoot/linux/wlt run `"ipconfig`""
Write-Host "  $RemoteRoot/linux/wlt tunnel add L 3080 127.0.0.1:3080"
Write-Host ''
Write-Host 'Open WinLinuxTunnel from the Start Menu. It talks to the agent on 127.0.0.1.' -ForegroundColor Yellow
