import subprocess
import os
import sys
import webbrowser

_IS_MAC = sys.platform == "darwin"
_USER = os.environ.get("USERNAME", "User")

# macOS: friendly name -> application name for `open -a` / AppleScript
MAC_APP_MAP = {
    # Browsers
    "safari": "Safari",
    "chrome": "Google Chrome",
    "google chrome": "Google Chrome",
    "firefox": "Firefox",
    "edge": "Microsoft Edge",
    "brave": "Brave Browser",
    # Communication
    "discord": "Discord",
    "telegram": "Telegram",
    "slack": "Slack",
    "teams": "Microsoft Teams",
    "zoom": "zoom.us",
    "whatsapp": "WhatsApp",
    "messages": "Messages",
    "nachrichten": "Messages",
    "facetime": "FaceTime",
    "mail": "Mail",
    # Dev tools
    "vscode": "Visual Studio Code",
    "vs code": "Visual Studio Code",
    "terminal": "Terminal",
    "iterm": "iTerm",
    "xcode": "Xcode",
    # Media
    "spotify": "Spotify",
    "vlc": "VLC",
    "music": "Music",
    "musik": "Music",
    "photos": "Photos",
    "fotos": "Photos",
    # Gaming
    "steam": "Steam",
    "epic games": "Epic Games Launcher",
    # Productivity
    "notepad": "TextEdit",
    "textedit": "TextEdit",
    "notes": "Notes",
    "notizen": "Notes",
    "word": "Microsoft Word",
    "excel": "Microsoft Excel",
    "powerpoint": "Microsoft PowerPoint",
    "pages": "Pages",
    "numbers": "Numbers",
    "keynote": "Keynote",
    "calendar": "Calendar",
    "kalender": "Calendar",
    "reminders": "Reminders",
    "erinnerungen": "Reminders",
    "maps": "Maps",
    "karten": "Maps",
    "preview": "Preview",
    "vorschau": "Preview",
    "app store": "App Store",
    # System
    "finder": "Finder",
    "explorer": "Finder",
    "file explorer": "Finder",
    "task manager": "Activity Monitor",
    "activity monitor": "Activity Monitor",
    "aktivitätsanzeige": "Activity Monitor",
    "calculator": "Calculator",
    "rechner": "Calculator",
    "settings": "System Settings",
    "system settings": "System Settings",
    "system preferences": "System Settings",
    "systemeinstellungen": "System Settings",
    "control panel": "System Settings",
}

# macOS < 13 still calls it System Preferences
_MAC_FALLBACKS = {
    "System Settings": "System Preferences",
}

APP_MAP = {
    # Browsers
    "chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "google chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "firefox": r"C:\Program Files\Mozilla Firefox\firefox.exe",
    "edge": r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "brave": r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    # Communication
    "discord": rf"C:\Users\{_USER}\AppData\Local\Discord\Update.exe",
    "telegram": rf"C:\Users\{_USER}\AppData\Roaming\Telegram Desktop\Telegram.exe",
    "slack": rf"C:\Users\{_USER}\AppData\Local\slack\slack.exe",
    "teams": rf"C:\Users\{_USER}\AppData\Local\Microsoft\Teams\current\Teams.exe",
    "zoom": rf"C:\Users\{_USER}\AppData\Roaming\Zoom\bin\Zoom.exe",
    # Dev tools
    "vscode": "code",
    "vs code": "code",
    "terminal": "wt.exe",
    "cmd": "cmd.exe",
    "powershell": "powershell.exe",
    "git bash": rf"C:\Program Files\Git\git-bash.exe",
    # Media
    "spotify": rf"C:\Users\{_USER}\AppData\Roaming\Spotify\Spotify.exe",
    "vlc": r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    # Gaming
    "steam": r"C:\Program Files (x86)\Steam\steam.exe",
    "epic games": r"C:\Program Files (x86)\Epic Games\Launcher\Portal\Binaries\Win64\EpicGamesLauncher.exe",
    # Productivity
    "notepad": "notepad.exe",
    "notepad++": r"C:\Program Files\Notepad++\notepad++.exe",
    "word": r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE",
    "excel": r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE",
    "powerpoint": r"C:\Program Files\Microsoft Office\root\Office16\POWERPNT.EXE",
    "paint": "mspaint.exe",
    "snipping tool": "SnippingTool.exe",
    # System
    "explorer": "explorer.exe",
    "file explorer": "explorer.exe",
    "task manager": "taskmgr.exe",
    "calculator": "calc.exe",
    "settings": "ms-settings:",
    "control panel": "control.exe",
}

_FALLBACKS = {
    "chrome": r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "google chrome": r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "edge": r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
}

# Discord needs --processStart flag
_ARGS = {
    "discord": ["--processStart", "Discord.exe"],
}


def open_app(name: str) -> str:
    """Open an application by friendly name."""
    key = name.lower().strip()
    if _IS_MAC:
        app = MAC_APP_MAP.get(key, name.strip())
        for candidate in (app, _MAC_FALLBACKS.get(app)):
            if not candidate:
                continue
            result = subprocess.run(["open", "-a", candidate], capture_output=True, text=True)
            if result.returncode == 0:
                return f"Opening {name}."
        return f"Could not open {name}: {result.stderr.strip() or 'app not found'}"
    exe = APP_MAP.get(key, key)

    # Build arg list — no shell=True
    args = [exe] + _ARGS.get(key, [])
    try:
        subprocess.Popen(args)
        return f"Opening {name}."
    except Exception as e:
        fallback = _FALLBACKS.get(key)
        if fallback:
            try:
                subprocess.Popen([fallback] + _ARGS.get(key, []))
                return f"Opening {name}."
            except Exception:
                pass
        return f"Could not open {name}: {e}"


def open_url(url: str) -> str:
    """Open a URL in the default browser."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    webbrowser.open(url)
    return f"Opened {url}."


def kill_process(name: str) -> str:
    """Kill a process by name (e.g. 'spotify', 'chrome')."""
    # Sanitize: only allow alphanumeric, dots, spaces, hyphens
    safe = "".join(c for c in name if c.isalnum() or c in ".-_ ")
    if _IS_MAC:
        app = MAC_APP_MAP.get(name.lower().strip(), safe)
        # pgrep first: `tell application ... to quit` would LAUNCH a non-running app
        if subprocess.run(["pgrep", "-i", "-f", app], capture_output=True).returncode != 0:
            return f"{name} is not running."
        try:  # graceful quit first (AppleScript), then force kill
            script = 'tell application "%s" to quit' % app.replace("\\", "\\\\").replace('"', '\\"')
            r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=8)
            if r.returncode == 0:
                return f"Closed {name}."
        except subprocess.TimeoutExpired:
            pass
        r = subprocess.run(["pkill", "-i", "-f", app], capture_output=True, text=True)
        if r.returncode == 0:
            return f"Killed {name}."
        return f"Could not kill {name}: {r.stderr.strip() or 'no such process'}"
    result = subprocess.run(
        ["taskkill", "/IM", f"{safe}.exe", "/F"],
        capture_output=True, text=True,
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        return f"Killed {name}."
    return f"Could not kill {name}: {output}"
