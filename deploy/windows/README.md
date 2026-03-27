# Windows Installer / Service Guide

This folder turns `print_agent.py` into a real installable Windows app experience.

## What it does

- installs/runs `print_agent.py` as a Windows Service (`OdooPrintAgent`)
- sets service startup to Automatic
- restarts service on crash
- installs Python dependencies from `requirements.txt`
- creates desktop shortcuts:
  - `Odoo Print Agent Logs`
  - `Odoo Print Agent Restart`
  - `Odoo Print Agent Manager`

## Prerequisites

- `nssm.exe` must exist at:
  - `deploy/windows/tools/nssm.exe`
  - or available in system `PATH`
- Optional for one-click installer build: Inno Setup 6

## Quick install (script only)

Run PowerShell as Administrator in project root:

```powershell
.\deploy\windows\install_service.ps1 -AutoInstallPython
```

## Uninstall service

```powershell
.\deploy\windows\uninstall_service.ps1 -RemoveShortcuts
```

## Build a single installer EXE

1. Put `nssm.exe` in `deploy/windows/tools/nssm.exe`
2. Install Inno Setup 6
3. Run:

```powershell
.\deploy\windows\installer\build_installer.ps1
```

4. Output:
   - `deploy/windows/installer/OdooPrintAgentSetup.exe`

## Notes

- Configure Odoo URL/DB/user/password and printer routes from **Odoo Print Agent Manager** desktop app.
- Bootstrap values in `print_agent.py` are fallback only.
