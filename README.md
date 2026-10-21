<div align="center">

# FocusRest

**A gentle 20-20-20 eye-rest reminder and private screen-time tracker for Windows.**
It lives in your system tray, reminds you to rest, and shows where your day actually goes.

![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0078D4)
![Python](https://img.shields.io/badge/python-3.9%2B-3776AB)
![Dependencies](https://img.shields.io/badge/dependencies-optional-8cc63f)
![Privacy](https://img.shields.io/badge/data-100%25%20local-111111)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

<img src="docs/dashboard.png" alt="FocusRest dashboard" width="900">

</div>

---

## Why FocusRest?

Long hours at a screen strain your eyes. The **20-20-20 rule** is simple: every **20 minutes**, look at something **20 feet** away for **20 seconds**. FocusRest does the reminding for you, and adds usage insights so you can see how much time goes to work, chat and entertainment.

Everything runs on your PC. No account, no cloud, no telemetry.

## Features

### Rest reminders
- Notification every 20 minutes (configurable) followed by a rest timer (default 20 seconds).
- Always-on-top rest window with a countdown ring and a rotating eye-care tip.
- "Rest over" notification, with an optional alarm sound (**Beep sound**, off by default).
- Pause, resume, reset or start a rest immediately, from the app or the tray menu.

### Usage insights
- Time per **application**, and per **website** inside browsers (Chrome, Edge, Firefox, Brave, Opera, Vivaldi).
- Idle time (no keyboard or mouse for 2 minutes) is never counted.
- Dashboard with:
  - screen-time chart by hour (24h) or by day (7d), with hover tooltips
  - total screen time compared with the previous day
  - rests taken, longest continuous session and a daily rest goal
  - **focus score** based on the share of time in work apps and sites
  - time by category: Work, Communication, Entertainment, Other
  - top websites, top apps and a recent app-usage list
- Searchable **Applications** and **Websites** pages with expandable rows.
- Pick any past day (90 days are kept) and **export it to CSV**.
- Private browsing windows are detected and **not recorded**.

### Runs quietly in the background
- Starts hidden in the **system tray**; click the icon to open the dashboard.
- The window's **X** button hides it, and **Quit** is in the tray menu.
- Tray tooltip shows the time left until your next rest.
- Optional **Start with Windows** toggle.
- Only one copy runs at a time.

## Quick start

1. Install **Python 3.9+** from [python.org](https://www.python.org/downloads/) (tick *Add Python to PATH*).
2. Download `rest_reminder.pyw`.
3. Double-click it.

The first launch installs two small packages (`pystray`, `pillow`) automatically for the tray icon. If that fails, the app still works and simply opens as a normal window.

Look near the clock for the FocusRest icon. On Windows 11 it may be under the **^** arrow; drag it out to keep it always visible.

```text
python rest_reminder.pyw --show    # open the window at start-up instead of hiding it
```

### Optional: exact website names

Without extra setup, websites are guessed from the browser window title. For exact domains read from the address bar:

```text
python -m pip install uiautomation
```

## How to use

### 1. First launch
1. Double-click `rest_reminder.pyw`. A "FocusRest is running" notification appears and the icon shows near the clock (system tray).
2. Click the tray icon to open the dashboard. Close it with **X** and it keeps running in the background.

### 2. Get your rest reminders
1. Leave the app running. After 20 minutes you get a notification and a **Rest your eyes** window with a 20-second countdown.
2. Look at something about 20 feet (6 metres) away until the timer ends.
3. A "Rest over" notification tells you to get back to work, and the next 20-minute countdown starts by itself.

Want to rest early? Click **Rest now** in the app, the sidebar or the tray menu. To stop the countdown for a while, click **Pause**, then **Resume** later.

### 3. Change the settings
Open **Rest Timer** from the sidebar (or the gear icon at the top):
- **Remind me every (minutes):** how often to rest.
- **Rest for (seconds):** how long each rest lasts.
- **Track app & website usage:** switch tracking off to stop recording.
- **Beep sound:** play an alarm when the rest ends (off by default).
- **Start with Windows:** launch FocusRest automatically when you sign in.

### 4. See where your time goes
- **Dashboard:** today at a glance. Use **24h / 7d** to switch the chart, and hover a bar for details.
- **Applications:** time per app. Click a browser row to expand the websites inside it.
- **Websites:** time per site. Filter by browser using the dropdown.
- **Quick search:** type an app or site name in the top bar and press **Enter**.
- **Date picker:** choose an earlier day to review it.
- **Export CSV:** save the selected day's data to a spreadsheet file.

### 5. Control it from the tray
Right-click the tray icon for **Open**, **Pause / Resume**, **Rest now**, **Beep sound**, **Start with Windows** and **Quit FocusRest**. Hover the icon to see the time left until the next rest.

### 6. Quit completely
The **X** button only hides the window. To fully close the app, right-click the tray icon and choose **Quit FocusRest**.

## Reference

| Where | What you can do |
| --- | --- |
| **Dashboard** | Overview of the day, quick timer actions, top apps and websites |
| **Applications** | Time per app; click a browser row to expand its websites |
| **Websites** | Time per site, filterable by browser |
| **Rest Timer** | Interval, rest length, tracking, beep and start-with-Windows settings, plus rest statistics |
| **Tray icon** | Open, Pause or Resume, Rest now, Beep sound, Start with Windows, Quit |
| **Top bar** | Quick search, date picker, sound toggle, settings, CSV export |

## Privacy

- All data is stored locally in `%APPDATA%\RestReminder\usage.json`.
- Nothing is uploaded and no network connection is needed to track usage.
- Window titles are read only to work out the app or website name; the recorded data is time per app and per site, not page content.
- Delete the `RestReminder` folder at any time to erase all history.

## Customising

Open `rest_reminder.pyw` in any editor:

| Setting | Where |
| --- | --- |
| Default interval and rest length | `DEFAULT_INTERVAL_MIN`, `DEFAULT_REST_SEC` |
| Idle threshold | `IDLE_LIMIT_SEC` (seconds) |
| How many days of history to keep | `KEEP_DAYS` |
| Which apps and sites count as Work, Communication or Entertainment | `CAT_RULES_APPS`, `CAT_RULES_SITES` |
| Friendly app names | `FRIENDLY` |
| Colours | constants in the *UI toolkit* section (`LIME`, `GREEN`, `BG` ...) |

## How it works

- A background thread samples the foreground window once per second (Windows API via `ctypes`) and adds the time to the current app, and to the current website for browsers.
- The interface is plain **Tkinter** with custom-drawn canvas widgets, so there is no heavy UI framework.
- Notifications use native Windows toasts through PowerShell, with no extra package.
- The tray icon uses `pystray` and `pillow`.

## Troubleshooting

**No window appears when I double-click.**
That is expected: it starts in the tray. Open it from the tray icon, or run with `--show`.

**I can't see the tray icon.**
Click the **^** arrow beside the clock and drag the FocusRest icon onto the taskbar.

**Websites show as page titles instead of domains.**
Install the optional package: `python -m pip install uiautomation`.

**Double-clicking does nothing the second time.**
FocusRest is already running. Use the tray icon.

**No notification sounds.**
Turn on **Beep sound** in the sidebar, the top bar or Rest Timer settings.

**Do Focus Assist or Do Not Disturb hide the reminders?**
Windows may silence the toast. The rest window still appears on top.

## Roadmap

- [ ] Settings that persist between launches
- [ ] Snooze button on the rest window
- [ ] Count long idle periods as a rest
- [ ] Skip reminders during calls or full-screen video
- [ ] Daily and weekly usage goals
- [ ] Custom categories edited in the app
- [ ] Daily summary notification

Ideas and pull requests are welcome.

## Contributing

1. Fork the repository and create a branch: `git checkout -b feature/my-idea`
2. Make your change and test it on Windows.
3. Open a pull request describing what changed and why.

Please keep it dependency-light and privacy-first.

## License

Released under the [MIT License](LICENSE).

---

<div align="center">
Made to give your eyes a break. 👀
</div>
