# ST MCP Connector — Setup & Update Guide

## Before you build for the first time

1. **Install Inno Setup 6** — this is a free program that packages the app into an installer.
   Download it at: https://jrsoftware.org/isdownload.php

2. **Make the GitHub repo public** — go to https://github.com/adamsbenjamin8-boop/st-mcp,
   click Settings → scroll to Danger Zone → Change visibility → Make public.
   The auto-update system won't work on a private repo. Your credentials are safe — they live
   in a .env file on each computer and are never uploaded to GitHub.

3. **Fill in the credentials** — open the file `assets\.env.template` in Notepad and paste in the
   real ServiceTitan values (Client ID, Client Secret, App Key, Tenant ID). Save it when done.
   Every computer you install on will get these credentials automatically.

---

## Building the installer

1. Open Command Prompt
2. Type `cd C:\ST\desktop_app` and press Enter
3. Type `build\build.bat` and press Enter
4. Wait — it will show progress. When it finishes you'll see:
   `build\Output\ST_MCP_Setup.exe`

That .exe file is the installer you copy to other computers.

---

## Installing on a new computer

1. Copy `ST_MCP_Setup.exe` to the computer (USB drive, email, shared folder — whatever works)
2. Run it and click through the install
3. That's it — the app starts automatically, puts an icon in the system tray, and runs every time the computer starts up

---

## What the app does on its own

Once installed, the app handles everything automatically:
- Keeps the ServiceTitan connection running in the background
- Restarts it automatically if it ever crashes
- Refreshes the local lookup data (technicians, job types, etc.) every morning
- Checks for updates 10 seconds after startup and every 4 hours after that
- Shows a green dot in the tray when everything is working, red if something is wrong

---

## Pushing an update to all computers

This is the day-to-day workflow when you want to add a new feature or fix something.

**Step 1** — Make your changes to the code (Claude does this part)

**Step 2** — Open the file `desktop_app\version.py` in Notepad and bump the version number.
For example, change `1.0.0` to `1.0.1`

**Step 3** — Open Command Prompt and run these commands one at a time:

```
cd C:\ST
git add -A
git commit -m "Describe what you changed here"
git tag scripts-v1.0.1
git push origin main
git push --tags
```

Replace `1.0.1` with whatever version number you set in Step 2.

That's it. GitHub picks it up automatically and within 4 hours every installed copy will show
an "Install Update" banner. One click installs it and restarts the connection.

---

## If something goes wrong on a computer

The app saves log files in its install folder (`C:\Program Files\ST_MCP\`):
- `cache_sync.log` — what happened during the last data refresh
- `app.log` — any errors or crashes

You can also open these from the **📋 Log** button inside the status window.

---

## File layout (for reference)

```
C:\ST\
  servicetitan_writer.py   The main ServiceTitan connection (updated automatically)
  st_cache_sync.py         The data refresh script (updated automatically)

desktop_app\
  launcher.py              The tray icon and status window
  updater.py               Handles downloading updates from GitHub
  version.py               Version number — bump this before every release
  assets\
    .env.template          Credentials — fill this in before building
  build\
    build.bat              Run this to build the installer
    installer.iss          Installer configuration
    st_mcp.spec            Build configuration
```
