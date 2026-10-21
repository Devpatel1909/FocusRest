# FocusRest

FocusRest is a Windows desktop helper that reminds you to take breaks using the 20-20-20 rule, tracks app and browser usage, and shows a privacy-friendly local dashboard.

It runs in the background as a tray app, starts hidden, and keeps all usage data on your PC in `%APPDATA%\RestReminder`.

## Features

- Every 20 minutes: reminder + short rest timer
- 20-second eye-rest window
- Optional beep sound toggle
- Tracks app usage and browser website usage
- Local dashboard with charts and stats
- Runs in the system tray near the clock
- Optional start-with-Windows autostart
- Exports CSV for your own analysis

## Requirements

- Windows 10/11
- Python 3.9+
- Python from python.org is recommended

Install runtime dependencies:

```powershell
python -m pip install pystray pillow
```

Optional for exact website names:

```powershell
python -m pip install uiautomation
```

## Run it

Double-click this file:

```powershell
rest_reminder.pyw
```

Or from command line:

```powershell
python rest_reminder.pyw
```

Use:

```powershell
python rest_reminder.pyw --show
```

This opens the dashboard immediately instead of starting hidden in the tray.

## Project files

- `rest_reminder.pyw` — main app
- `patch_tray.py` — helper script to patch a target `rest_reminder.pyw` file with tray support
- `requirements.txt` — package dependencies

## Privacy

FocusRest stores activity data locally on your computer only. No account, login, or cloud syncing is required.

## Notes

This project is designed for personal use and local desktop productivity tracking. It does not send data anywhere else.

## License

MIT License.
