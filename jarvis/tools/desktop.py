import subprocess
import sys
import time
import os
import tempfile
import json
import pyautogui

pyautogui.FAILSAFE = False

_IS_MAC = sys.platform == "darwin"

# Windows-style modifier names -> macOS equivalents (pyautogui already maps 'alt' to Option)
_MAC_KEY_ALIASES = {
    "win": "command", "winleft": "command", "winright": "command", "super": "command",
    "cmd": "command",
    "altleft": "option", "altright": "option",
}


# ──────────────── Typing & Keys ────────────────

def type_text(text: str) -> str:
    """Type text at the current cursor position."""
    time.sleep(0.15)
    pyautogui.write(text, interval=0.02)
    return f"Typed: {text[:80]}"


def press_key(keys: str) -> str:
    """Press a key or hotkey combo. Example: 'ctrl+shift+esc', 'alt+tab', 'enter'."""
    parts = [k.strip() for k in keys.split("+")]
    if _IS_MAC:
        parts = [_MAC_KEY_ALIASES.get(k.lower(), k) for k in parts]
    pyautogui.hotkey(*parts)
    return f"Pressed: {keys}"


# ──────────────── Mouse Control ────────────────

def click_at(x: int, y: int, button: str = "left") -> str:
    """Click at screen coordinates (x, y). Button: left, right, double."""
    if button == "double":
        pyautogui.doubleClick(x, y)
    elif button == "right":
        pyautogui.rightClick(x, y)
    else:
        pyautogui.click(x, y)
    return f"Clicked ({button}) at ({x}, {y})."


def scroll_screen(direction: str, amount: int = 3) -> str:
    """Scroll up or down. Amount = number of scroll steps."""
    if direction.lower() == "up":
        pyautogui.scroll(amount)
    elif direction.lower() == "down":
        pyautogui.scroll(-amount)
    else:
        return f"Unknown direction: {direction}. Use up/down."
    return f"Scrolled {direction} by {amount}."


def move_mouse(x: int, y: int) -> str:
    """Move mouse cursor to (x, y) without clicking."""
    pyautogui.moveTo(x, y)
    return f"Moved mouse to ({x}, {y})."


# ──────────────── Screen Reading (OCR) ────────────────

def read_screen() -> str:
    """Take a screenshot and OCR it. Returns all visible text with approximate positions.
    Format: each line is 'text | x,y' where x,y is the center of the text bounding box."""
    tmp = os.path.join(tempfile.gettempdir(), "jarvis_screen.png")
    img = pyautogui.screenshot()
    img.save(tmp)
    if _IS_MAC:
        # Retina: screenshot is in physical pixels, mouse works in logical points
        screen_w, _ = pyautogui.size()
        return _ocr_image_mac(tmp, img.width / screen_w if screen_w else 1.0)
    return _ocr_image(tmp)


def _ocr_image_mac(image_path: str, scale: float = 1.0) -> str:
    """Run Apple Vision OCR on an image file, return text with positions (logical screen coords)."""
    try:
        import Vision
        from Foundation import NSURL
    except ImportError:
        return "OCR unavailable: pyobjc-framework-Vision is not installed (pip install -r requirements.txt)."
    from PIL import Image

    with Image.open(image_path) as im:
        width, height = im.size
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(
        NSURL.fileURLWithPath_(image_path), None
    )
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    ok, err = handler.performRequests_error_([request], None)
    if not ok:
        return f"OCR failed: {err}"

    rows = []
    for obs in request.results() or []:
        candidates = obs.topCandidates_(1)
        if not candidates:
            continue
        box = obs.boundingBox()  # normalized, origin bottom-left
        cx = (box.origin.x + box.size.width / 2) * width / scale
        cy = (1 - (box.origin.y + box.size.height / 2)) * height / scale
        rows.append((cy, cx, candidates[0].string()))
    if not rows:
        return "No text found on screen."
    rows.sort()
    return "\n".join(f"{text} | {int(cx)},{int(cy)}" for cy, cx, text in rows)


def _ocr_image(image_path: str) -> str:
    """Run Windows OCR on an image file, return text with positions."""
    ps = f"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation,ContentType=WindowsRuntime]

function Await($WinRtTask, $ResultType) {{
    $asTask = [System.WindowsRuntimeSystemExtensions].GetMethod('AsTask', [Type[]]@($WinRtTask.GetType()))
    if (-not $asTask) {{ $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {{ $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' }} | Select-Object -First 1 }}
    $netTask = $asTask.MakeGenericMethod($ResultType).Invoke($null, @($WinRtTask))
    $netTask.Wait()
    $netTask.Result
}}

$path = '{image_path.replace(chr(92), chr(92)+chr(92))}'
$stream = [System.IO.File]::OpenRead($path)
$randomAccess = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($stream)
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($randomAccess)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$stream.Dispose()

$output = @()
foreach ($line in $result.Lines) {{
    $words = $line.Words
    if ($words.Count -gt 0) {{
        $x1 = ($words | ForEach-Object {{ $_.BoundingRect.X }}) | Measure-Object -Minimum | Select-Object -Expand Minimum
        $y1 = ($words | ForEach-Object {{ $_.BoundingRect.Y }}) | Measure-Object -Minimum | Select-Object -Expand Minimum
        $x2 = ($words | ForEach-Object {{ $_.BoundingRect.X + $_.BoundingRect.Width }}) | Measure-Object -Maximum | Select-Object -Expand Maximum
        $y2 = ($words | ForEach-Object {{ $_.BoundingRect.Y + $_.BoundingRect.Height }}) | Measure-Object -Maximum | Select-Object -Expand Maximum
        $cx = [int](($x1 + $x2) / 2)
        $cy = [int](($y1 + $y2) / 2)
        $output += "$($line.Text) | $cx,$cy"
    }}
}}
$output -join "`n"
"""
    result = subprocess.run(
        ["powershell", "-command", ps],
        capture_output=True, text=True, timeout=15,
    )
    text = result.stdout.strip()
    if not text:
        return "No text found on screen."
    return text


def find_on_screen(text: str) -> str:
    """Find text on screen using OCR. Returns coordinates of the match, or 'not found'.
    Use click_at() with the returned coordinates to click on it."""
    screen_text = read_screen()
    if screen_text == "No text found on screen.":
        return "No text found on screen."

    target = text.lower()
    best_match = None
    best_score = 0

    for line in screen_text.split("\n"):
        if " | " not in line:
            continue
        content, coords = line.rsplit(" | ", 1)
        content_lower = content.lower()

        # Exact substring match
        if target in content_lower:
            x, y = coords.split(",")
            return f"Found '{text}' at ({x.strip()}, {y.strip()}). Use click_at to click it."

        # Partial word match — score by overlap
        words_target = set(target.split())
        words_content = set(content_lower.split())
        overlap = len(words_target & words_content)
        if overlap > best_score:
            best_score = overlap
            best_match = (content, coords)

    if best_match and best_score > 0:
        content, coords = best_match
        x, y = coords.split(",")
        return f"Closest match: '{content}' at ({x.strip()}, {y.strip()}). Use click_at to click it."

    # Not found — try scrolling down and searching again
    pyautogui.scroll(-3)  # scroll down
    time.sleep(0.5)
    screen_text_2 = read_screen()
    for line in screen_text_2.split("\n"):
        if " | " not in line:
            continue
        content, coords = line.rsplit(" | ", 1)
        if target in content.lower():
            x, y = coords.split(",")
            return f"Found '{text}' at ({x.strip()}, {y.strip()}) after scrolling. Use click_at to click it."

    return f"'{text}' not found on screen (tried scrolling)."


# ──────────────── Window Management ────────────────

def get_open_windows() -> str:
    """Return titles of all open windows."""
    if _IS_MAC:
        import Quartz
        opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        titles = []
        for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
            if w.get("kCGWindowLayer", 0) != 0:
                continue
            owner = w.get("kCGWindowOwnerName", "")
            title = w.get("kCGWindowName", "")  # empty without Screen Recording permission
            titles.append(f"{owner}: {title}" if title else owner)
        return "\n".join(dict.fromkeys(titles)) or "No open windows found."
    result = subprocess.run(
        ["powershell", "-command",
         "Get-Process | Where-Object {$_.MainWindowTitle} | "
         "Select-Object -ExpandProperty MainWindowTitle"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() or "No open windows found."


def focus_window(title: str) -> str:
    """Bring a window to the foreground by partial title match."""
    if _IS_MAC:
        # Title passed as argv -> no escaping needed. Needs Accessibility permission.
        script = """
on run argv
  set q to item 1 of argv
  tell application "System Events"
    repeat with p in (every process whose visible is true)
      if (name of p) contains q then
        set frontmost of p to true
        return "ok"
      end if
      repeat with w in (every window of p)
        if (name of w) contains q then
          set frontmost of p to true
          perform action "AXRaise" of w
          return "ok"
        end if
      end repeat
    end repeat
  end tell
  return "not found"
end run
"""
        result = subprocess.run(["osascript", "-e", script, title], capture_output=True, text=True)
        if result.returncode != 0:
            return f"Could not focus '{title}': {result.stderr.strip()}"
        if "not found" in result.stdout:
            return f"No window matching '{title}' found."
        return f"Focused: {title}"
    safe = title.replace("'", "''")
    ps = (
        f"$p = Get-Process | Where-Object {{$_.MainWindowTitle -like '*{safe}*'}} "
        "| Select-Object -First 1; "
        "if ($p) { "
        "Add-Type -TypeDefinition '"
        "using System; using System.Runtime.InteropServices; "
        "public class W { "
        '[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h); '
        '[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n); '
        "}'; "
        "[W]::ShowWindow($p.MainWindowHandle, 9); "
        "[W]::SetForegroundWindow($p.MainWindowHandle) "
        "} else { Write-Output 'not found' }"
    )
    result = subprocess.run(["powershell", "-command", ps], capture_output=True, text=True)
    if "not found" in result.stdout:
        return f"No window matching '{title}' found."
    return f"Focused: {title}"


# ──────────────── Media Control ────────────────

def media_control(action: str) -> str:
    """Control media playback: play, pause, next, previous, mute."""
    key_map = {
        "play": "playpause",
        "pause": "playpause",
        "playpause": "playpause",
        "next": "nexttrack",
        "skip": "nexttrack",
        "previous": "prevtrack",
        "prev": "prevtrack",
        "mute": "volumemute",
    }
    key = key_map.get(action.lower().strip())
    if not key:
        return f"Unknown media action: {action}. Use play/pause/next/previous/mute."
    if _IS_MAC and key in _MAC_MEDIA_KEYS:
        _mac_media_key(_MAC_MEDIA_KEYS[key])
    else:
        pyautogui.press(key)
    return f"Media: {action}"


# NX_KEYTYPE_* codes — pyautogui has no media keys on macOS
_MAC_MEDIA_KEYS = {"playpause": 16, "nexttrack": 17, "prevtrack": 18}


def _mac_media_key(code: int) -> None:
    """Post a system-defined media key press (down + up) on macOS."""
    import Quartz
    from AppKit import NSEvent

    for down in (True, False):
        flags = 0xA00 if down else 0xB00
        data1 = (code << 16) | ((0xA if down else 0xB) << 8)
        ev = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            14, (0, 0), flags, 0, 0, None, 8, data1, -1  # 14 = NSEventTypeSystemDefined
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev.CGEvent())


def screenshot(filename: str = "") -> str:
    """Take a screenshot and save to Desktop."""
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    if not filename:
        filename = f"screenshot_{int(time.time())}.png"
    if not filename.endswith(".png"):
        filename += ".png"
    path = os.path.join(desktop, filename)
    img = pyautogui.screenshot()
    img.save(path)
    return f"Screenshot saved: {path}"
