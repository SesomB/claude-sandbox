<#
.SYNOPSIS
  Installs Hermes Desktop offline on an air-gapped Windows workstation and
  points it at the Hermes backend on the Debian server.

.DESCRIPTION
  * Verifies the .msixbundle against SHA256SUMS from the bundle folder.
  * Checks its Authenticode signature (Nous Research Inc., Azure Trusted Signing).
  * Sideloads it with Add-AppxPackage -Path. This registers NO App Installer
    update source, so the app's update check just reports
    "no-app-installer-source" instead of reaching out to the internet.
  * Optionally stores the remote-gateway URL + token as user environment
    variables (HERMES_DESKTOP_REMOTE_URL / HERMES_DESKTOP_REMOTE_TOKEN), so
    Desktop starts directly in remote mode. Otherwise enter them in
    Settings -> Gateways -> Remote gateway.
  * Optionally (-BlockInternet, admin) adds Windows Firewall rules that block
    outbound traffic from the app's executables. Loopback (the SSH tunnel at
    127.0.0.1) is not affected by Windows Firewall.

  Requirements: Windows 11 22H2 (build 22621) or later, x64.
  The local agent runtime bundled in the MSIX is never started in remote mode.

.PARAMETER Path
  Path to the HermesBundled-*.msixbundle (default: the one next to this script).
.PARAMETER Token
  Your Desktop remote token (from hermes-add-user / ~/.hermes/desktop-connection.txt).
  If given, Desktop is configured for remote mode via user environment variables.
.PARAMETER LocalPort
  Local tunnel port (default 9119).
.PARAMETER BlockInternet
  Add outbound-block firewall rules for the installed app (run as Administrator).
.PARAMETER SkipSignatureCheck
  Skip the Authenticode check (e.g. when revocation info cannot be retrieved offline).

.EXAMPLE
  .\Install-HermesDesktop.ps1 -Token 'WwlRUiafsmsC...'
.EXAMPLE
  # elevated PowerShell
  .\Install-HermesDesktop.ps1 -BlockInternet
#>
[CmdletBinding()]
param(
    [string]$Path,
    [string]$Token,
    [ValidateRange(1024, 65535)][int]$LocalPort = 9119,
    [switch]$BlockInternet,
    [switch]$SkipSignatureCheck
)

$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ExpectedPublisher = 'CN=Nous Research Inc., O=Nous Research Inc., L=Austin, S=Texas, C=US'

# --- platform -------------------------------------------------------------
$build = [Environment]::OSVersion.Version.Build
if ($build -lt 22621) { throw "Hermes Desktop needs Windows 11 22H2 (build 22621) or later; this is build $build." }
if (-not [Environment]::Is64BitOperatingSystem) { throw 'Hermes Desktop needs 64-bit Windows.' }

# --- locate + verify the package --------------------------------------------
if (-not $Path) {
    $Path = Get-ChildItem -Path $Here -Filter 'HermesBundled-*.msix*' | Sort-Object LastWriteTime -Descending |
        Select-Object -First 1 -ExpandProperty FullName
    if (-not $Path) { throw "No HermesBundled-*.msixbundle found in $Here. Use -Path." }
}
$Path = (Resolve-Path $Path).Path
$name = Split-Path -Leaf $Path
Write-Host "Package: $Path"

$sums = @((Join-Path $Here 'SHA256SUMS'), (Join-Path (Split-Path $Here -Parent) 'SHA256SUMS')) |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if ($sums) {
    $line = Get-Content $sums | Where-Object { $_ -match "^\s*([0-9a-fA-F]{64})\s+\*?(\S*/)?$([regex]::Escape($name))\s*$" } | Select-Object -First 1
    if (-not $line) { throw "$name is not listed in $sums" }
    $expected = ($line -split '\s+')[0].ToLower()
    Write-Host 'Verifying SHA-256 (this takes a while for a multi-GB file)...'
    $actual = (Get-FileHash -Algorithm SHA256 -Path $Path).Hash.ToLower()
    if ($actual -ne $expected) { throw "SHA-256 mismatch for $name`n expected $expected`n actual   $actual" }
    Write-Host 'SHA-256 OK' -ForegroundColor Green
} else {
    Write-Warning 'No SHA256SUMS found next to the package; skipping hash verification.'
}

if (-not $SkipSignatureCheck) {
    $sig = Get-AuthenticodeSignature -FilePath $Path
    if ($sig.Status -ne 'Valid') {
        throw "Signature status is '$($sig.Status)': $($sig.StatusMessage)`nIf this is only an offline revocation-check failure, re-run with -SkipSignatureCheck (Add-AppxPackage verifies the signature again)."
    }
    if ($sig.SignerCertificate.Subject -ne $ExpectedPublisher) {
        throw "Unexpected signer: $($sig.SignerCertificate.Subject)"
    }
    Write-Host "Signature OK: $($sig.SignerCertificate.Subject)" -ForegroundColor Green
}

# --- install ----------------------------------------------------------------
$existing = Get-AppxPackage -Name 'NousResearch.HermesBundled' -ErrorAction SilentlyContinue
if ($existing) { Write-Host "Upgrading existing Hermes Desktop $($existing.Version)" }
Write-Host 'Installing (Add-AppxPackage)...'
Add-AppxPackage -Path $Path -ForceUpdateFromAnyVersion
$pkg = Get-AppxPackage -Name 'NousResearch.HermesBundled'
if (-not $pkg) { throw 'Installation finished but the package is not registered.' }
Write-Host "Installed Hermes Desktop $($pkg.Version)" -ForegroundColor Green

# --- remote gateway ---------------------------------------------------------
$url = "http://127.0.0.1:$LocalPort"
if ($Token) {
    # Desktop reads both together; URL without token is an error.
    [Environment]::SetEnvironmentVariable('HERMES_DESKTOP_REMOTE_URL', $url, 'User')
    [Environment]::SetEnvironmentVariable('HERMES_DESKTOP_REMOTE_TOKEN', $Token, 'User')
    Write-Host "Configured remote gateway $url via user environment variables (sign out/in or restart Explorer if Desktop doesn't pick them up)."
} else {
    Write-Host "In Hermes Desktop: Settings -> Gateways -> Remote gateway -> URL $url, paste your token."
}

# --- optional outbound block ------------------------------------------------
if ($BlockInternet) {
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) { throw '-BlockInternet needs an elevated (Administrator) PowerShell.' }
    Get-NetFirewallRule -Group 'Hermes Desktop (air-gap)' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    $exes = Get-ChildItem -Path $pkg.InstallLocation -Recurse -Filter '*.exe' -ErrorAction SilentlyContinue
    foreach ($exe in $exes) {
        New-NetFirewallRule -DisplayName "Hermes Desktop block: $($exe.Name)" -Group 'Hermes Desktop (air-gap)' `
            -Direction Outbound -Action Block -Program $exe.FullName -Profile Any | Out-Null
    }
    Write-Host "Added $($exes.Count) outbound block rules (group 'Hermes Desktop (air-gap)'). Re-run after upgrading Desktop." -ForegroundColor Green
}

Write-Host ''
Write-Host 'Next: open the SSH tunnel, then start Hermes Desktop:' -ForegroundColor Cyan
Write-Host "  .\Start-HermesTunnel.ps1 -Server <server> -User <you> -RemotePort <your port> -LocalPort $LocalPort"
