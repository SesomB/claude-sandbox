<#
.SYNOPSIS
  Opens the SSH tunnel that connects Hermes Desktop to your Hermes backend
  on the air-gapped Debian server.

.DESCRIPTION
  Hermes Desktop is set to "Remote gateway" = http://127.0.0.1:9119.
  This script forwards that local port, over SSH, to your personal backend
  port on the server (127.0.0.1:<RemotePort> on the server side). The
  agent, its terminal and its files all live on the server; nothing runs
  on this workstation except the Desktop UI and ssh.exe.

  Uses the OpenSSH client that ships with Windows 10/11 (C:\Windows\System32\OpenSSH\ssh.exe).

.PARAMETER Server
  Server host name or IP address.
.PARAMETER User
  Your Linux account on the server.
.PARAMETER RemotePort
  Your backend port on the server, as printed by add-user.sh (9121, 9122, ...).
.PARAMETER LocalPort
  Local port Hermes Desktop connects to. Default 9119.
.PARAMETER NewKey
  Generate an ed25519 key (if none exists) and print the public key to send
  to the server administrator, then exit.
.PARAMETER RegisterLogonTask
  Register a per-user scheduled task that opens the tunnel at every logon.
.PARAMETER UnregisterLogonTask
  Remove that scheduled task.

.EXAMPLE
  .\Start-HermesTunnel.ps1 -NewKey
.EXAMPLE
  .\Start-HermesTunnel.ps1 -Server hermes01.corp.local -User alice -RemotePort 9121
.EXAMPLE
  .\Start-HermesTunnel.ps1 -Server hermes01.corp.local -User alice -RemotePort 9121 -RegisterLogonTask
#>
[CmdletBinding(DefaultParameterSetName = 'Tunnel')]
param(
    [Parameter(ParameterSetName = 'Tunnel', Mandatory)]
    [Parameter(ParameterSetName = 'Register', Mandatory)]
    [string]$Server,

    [Parameter(ParameterSetName = 'Tunnel', Mandatory)]
    [Parameter(ParameterSetName = 'Register', Mandatory)]
    [string]$User,

    [Parameter(ParameterSetName = 'Tunnel', Mandatory)]
    [Parameter(ParameterSetName = 'Register', Mandatory)]
    [ValidateRange(1024, 65535)]
    [int]$RemotePort,

    [Parameter(ParameterSetName = 'Tunnel')]
    [Parameter(ParameterSetName = 'Register')]
    [ValidateRange(1024, 65535)]
    [int]$LocalPort = 9119,

    [Parameter(ParameterSetName = 'Key', Mandatory)]
    [switch]$NewKey,

    [Parameter(ParameterSetName = 'Register', Mandatory)]
    [switch]$RegisterLogonTask,

    [Parameter(ParameterSetName = 'Unregister', Mandatory)]
    [switch]$UnregisterLogonTask
)

$ErrorActionPreference = 'Stop'
$TaskName = 'Hermes SSH tunnel'
$SshDir = Join-Path $env:USERPROFILE '.ssh'
$KeyPath = Join-Path $SshDir 'id_ed25519_hermes'

function Get-OpenSsh([string]$Name) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $builtin = Join-Path $env:WINDIR "System32\OpenSSH\$Name.exe"
    if (Test-Path $builtin) { return $builtin }
    throw "$Name.exe not found. Enable the Windows optional feature 'OpenSSH Client' (it installs offline from the OS image): Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0"
}

switch ($PSCmdlet.ParameterSetName) {
    'Key' {
        if (-not (Test-Path $SshDir)) { New-Item -ItemType Directory -Path $SshDir | Out-Null }
        if (-not (Test-Path $KeyPath)) {
            $keygen = Get-OpenSsh 'ssh-keygen'
            Write-Host "Generating $KeyPath. Choose a passphrase when prompted."
            & $keygen -t ed25519 -f $KeyPath -C "$env:USERNAME@$env:COMPUTERNAME hermes"
            if ($LASTEXITCODE -ne 0) { throw 'ssh-keygen failed.' }
        }
        Write-Host ''
        Write-Host 'Send this public key to the Hermes server administrator:' -ForegroundColor Cyan
        Get-Content "$KeyPath.pub"
        return
    }

    'Unregister' {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "Removed scheduled task '$TaskName' (if it existed)."
        return
    }

    'Register' {
        $self = $MyInvocation.MyCommand.Path
        $argList = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$self`" " +
                   "-Server $Server -User $User -RemotePort $RemotePort -LocalPort $LocalPort"
        $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argList
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
            -Description 'Forwards Hermes Desktop to the Hermes backend over SSH.' -Force | Out-Null
        Write-Host "Registered '$TaskName'. It starts at your next logon; start it now with:"
        Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
        Write-Host 'Note: a key with a passphrase needs ssh-agent (Get-Service ssh-agent) for unattended use.'
        return
    }

    'Tunnel' {
        $ssh = Get-OpenSsh 'ssh'
        $inUse = Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue
        if ($inUse) {
            throw "Local port $LocalPort is already in use (PID $($inUse[0].OwningProcess)). Is a tunnel already open, or is a local Hermes backend running? Use -LocalPort and change Desktop's Remote gateway URL to match."
        }
        $sshArgs = @(
            '-N',                                   # no remote command, forwarding only
            '-o', 'ExitOnForwardFailure=yes',       # fail loudly if the server refuses the forward
            '-o', 'ServerAliveInterval=30',
            '-o', 'ServerAliveCountMax=3',
            '-L', "127.0.0.1:${LocalPort}:127.0.0.1:${RemotePort}"
        )
        if (Test-Path $KeyPath) { $sshArgs += @('-i', $KeyPath, '-o', 'IdentitiesOnly=yes') }
        $sshArgs += "$User@$Server"

        Write-Host "Tunnel: http://127.0.0.1:$LocalPort  ->  ${Server}:127.0.0.1:$RemotePort (as $User)"
        Write-Host 'Leave this window open while you use Hermes Desktop. Ctrl+C closes the tunnel.'
        # Reconnect automatically if the connection drops (network blip, server reboot).
        while ($true) {
            & $ssh @sshArgs
            $code = $LASTEXITCODE
            if ($code -eq 255) {
                Write-Warning "ssh exited with code 255 (connection lost or refused). Retrying in 10 s..."
                Start-Sleep -Seconds 10
            } else {
                Write-Host "ssh exited with code $code."
                break
            }
        }
    }
}
