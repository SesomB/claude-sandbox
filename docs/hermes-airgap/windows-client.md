# Hermes Desktop on an air-gapped Windows workstation

Hermes Desktop is the window you chat in. The agent itself, with its terminal, files, memory and sessions, runs on the **Hermes server**, under **your own account** there. Desktop reaches the server through an SSH tunnel that you open with `Start-HermesTunnel.ps1`.

**You need:**
- **Windows 11 22H2** (build 22621) or later, x64.
- The **OpenSSH Client** Windows feature. It is usually already installed; if not, an admin can add it from the OS image with `Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0`.
- From the bundle's `windows\` folder: `HermesBundled-*.msixbundle`, `Install-HermesDesktop.ps1`, `Start-HermesTunnel.ps1`, and `SHA256SUMS` (next to the folder or inside it).

If PowerShell blocks the scripts, run them with `powershell -ExecutionPolicy Bypass -File .\<script>.ps1 …`, or unblock them with `Unblock-File .\*.ps1`.

## 1. Create your SSH key and send it to the admin

```powershell
.\Start-HermesTunnel.ps1 -NewKey
```

This creates `%USERPROFILE%\.ssh\id_ed25519_hermes`; choose a passphrase. It then prints the **public** key: send that line to your Hermes admin. The admin runs `hermes-add-user` and sends back three things:

- the server name
- your port (9121, 9122, …)
- your **Desktop token**

## 2. Install Hermes Desktop

```powershell
.\Install-HermesDesktop.ps1 -Token '<your Desktop token>'
```

The script:
- Checks the package against `SHA256SUMS`, which takes a few minutes for a 4.3 GB file.
- Checks the Nous Research signature. If it fails only because the certificate revocation check can't run offline, use `-SkipSignatureCheck`; Windows verifies the signature again during installation.
- Installs with `Add-AppxPackage`.
- Saves the remote-gateway URL (`http://127.0.0.1:9119`) and your token as user environment variables, so Desktop opens straight in remote mode.

Without `-Token`, enter the URL and token yourself in Desktop under **Settings → Gateways → Remote gateway**.

Optional, from an **elevated** PowerShell:
```powershell
.\Install-HermesDesktop.ps1 -BlockInternet
```
This adds Windows Firewall rules that stop the app trying to reach the internet, for example its update check. The tunnel on 127.0.0.1 isn't affected. Re-run it after every Desktop upgrade.

## 3. Open the tunnel and start Desktop

```powershell
.\Start-HermesTunnel.ps1 -Server hermes01.corp.local -User alice -RemotePort 9121
```

- Leave this window open; it reconnects automatically if the connection drops.
- The first connection asks you to confirm the server's host key. Check the fingerprint with your admin.
- Then start **Hermes** from the Start menu.

To start the tunnel automatically at logon:
```powershell
.\Start-HermesTunnel.ps1 -Server hermes01.corp.local -User alice -RemotePort 9121 -RegisterLogonTask
Start-ScheduledTask -TaskName 'Hermes SSH tunnel'     # start it now
```
A logon task can't type a key passphrase. Either load the key into the Windows `ssh-agent` service (`Start-Service ssh-agent; ssh-add $env:USERPROFILE\.ssh\id_ed25519_hermes`), or ask your admin whether a key without a passphrase is acceptable. Remove the task with `-UnregisterLogonTask`.

## 4. Check it works

In Desktop, ask: *"Run `uname -a` and `pwd` with the terminal tool."* The answer should show **Linux (Debian)** and `/home/<you>/work`. That proves the agent runs on the server, not on your PC. Files the agent creates appear in `/home/<you>/work` on the server.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Local port 9119 is already in use` | A tunnel is already running, or Desktop started a local backend. Close it, or use `-LocalPort 9120` and change the URL in Desktop to `http://127.0.0.1:9120`. |
| `Permission denied (publickey)` | The admin doesn't have your current public key, or you used a different key. Run `-NewKey` again to print it. |
| `channel ... open failed: administratively prohibited` | `-RemotePort` is wrong: you can only tunnel to your own port. |
| Desktop says unauthorized / 401 | Wrong token. Ask the admin, or read `~/.hermes/desktop-connection.txt` on the server. If the token was rotated, update it in Settings → Gateways or re-run the installer with `-Token`. |
| Desktop can't connect at all | Is the tunnel window still open? Ask the admin to check `systemctl status hermes-serve@<you>`. |
| Update errors in Settings or `logs\update.log` | Expected offline. Upgrades come as a new MSIX from your admin. |
