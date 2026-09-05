import subprocess
import os
import threading
import time

_VOL_TYPE = """
using System.Runtime.InteropServices;
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IAudioEndpointVolume {
    void _a();void _b();void _c();void _d();
    int SetMasterVolumeLevelScalar(float f, System.Guid g);
    void _f();
    int GetMasterVolumeLevelScalar(out float f);
}
[Guid("D666063F-1587-4E43-81F1-B948E807363F"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IMMDevice {
    int Activate(ref System.Guid id,int c,System.IntPtr p,[MarshalAs(UnmanagedType.IUnknown)]out object o);
}
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IMMDeviceEnumerator {
    void _a();
    int GetDefaultAudioEndpoint(int flow,int role,out IMMDevice dev);
}
[ComImport,Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
public class MMDeviceEnumerator {}
"""


def _run_vol_ps(ps: str) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-command", ps], capture_output=True, text=True)


def set_volume(level: int) -> str:
    """Set system volume (0-100) via Windows Audio API."""
    level = max(0, min(100, level))
    scalar = level / 100.0
    ps = (
        f"Add-Type -TypeDefinition @'\n{_VOL_TYPE}\n'@ -Language CSharp 2>$null;"
        "$e = [MMDeviceEnumerator] -as [IMMDeviceEnumerator];"
        "$d = $null; $e.GetDefaultAudioEndpoint(0,1,[ref]$d);"
        "$g = [System.Guid]'5CDF2C82-841E-4546-9722-0CF74078229A';"
        "$v = $null; $d.Activate([ref]$g,1,[IntPtr]::Zero,[ref]$v);"
        f"$v.SetMasterVolumeLevelScalar({scalar},[System.Guid]::Empty)"
    )
    _run_vol_ps(ps)
    return f"Volume set to {level}%."


def get_volume() -> str:
    """Get current system volume (0-100)."""
    ps = (
        f"Add-Type -TypeDefinition @'\n{_VOL_TYPE}\n'@ -Language CSharp 2>$null;"
        "$e = [MMDeviceEnumerator] -as [IMMDeviceEnumerator];"
        "$d = $null; $e.GetDefaultAudioEndpoint(0,1,[ref]$d);"
        "$g = [System.Guid]'5CDF2C82-841E-4546-9722-0CF74078229A';"
        "$v = $null; $d.Activate([ref]$g,1,[IntPtr]::Zero,[ref]$v);"
        "$f = 0.0; $v.GetMasterVolumeLevelScalar([ref]$f); [int]($f*100)"
    )
    result = _run_vol_ps(ps)
    vol = result.stdout.strip()
    return f"Volume is at {vol}%." if vol.isdigit() else "Could not read volume."


def get_clipboard() -> str:
    """Return current clipboard text."""
    result = subprocess.run(
        ["powershell", "-command", "Get-Clipboard"],
        capture_output=True, text=True,
    )
    return result.stdout.strip()


def set_clipboard(text: str) -> str:
    """Set clipboard content."""
    safe = text.replace("'", "''")
    subprocess.run(
        ["powershell", "-command", f"Set-Clipboard -Value '{safe}'"],
        capture_output=True,
    )
    return "Clipboard updated."


def get_system_info() -> str:
    """Get CPU, RAM, and disk usage."""
    ps = (
        "$cpu = (Get-CimInstance Win32_Processor).LoadPercentage;"
        "$os = Get-CimInstance Win32_OperatingSystem;"
        "$ram_total = [math]::Round($os.TotalVisibleMemorySize/1MB,1);"
        "$ram_free = [math]::Round($os.FreePhysicalMemory/1MB,1);"
        "$ram_used = $ram_total - $ram_free;"
        "$disk = Get-CimInstance Win32_LogicalDisk -Filter 'DeviceID=\"C:\"';"
        "$disk_total = [math]::Round($disk.Size/1GB,1);"
        "$disk_free = [math]::Round($disk.FreeSpace/1GB,1);"
        "Write-Output \"CPU: ${cpu}% | RAM: ${ram_used}/${ram_total} GB | "
        "Disk C: ${disk_free}/${disk_total} GB free\""
    )
    result = subprocess.run(["powershell", "-command", ps], capture_output=True, text=True)
    return result.stdout.strip() or "Could not read system info."


def set_brightness(level: int) -> str:
    """Set screen brightness (0-100). Only works on laptops."""
    level = max(0, min(100, level))
    ps = f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1,{level})"
    result = subprocess.run(["powershell", "-command", ps], capture_output=True, text=True)
    if result.returncode == 0:
        return f"Brightness set to {level}%."
    return "Brightness control not available (desktop monitors don't support WMI brightness)."


def lock_screen() -> str:
    """Lock the workstation."""
    subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"])
    return "Screen locked."


def power_command(action: str) -> str:
    """Shutdown, restart, or sleep the PC."""
    action = action.lower().strip()
    if action == "shutdown":
        subprocess.Popen(["shutdown", "/s", "/t", "5"])
        return "Shutting down in 5 seconds."
    elif action == "restart":
        subprocess.Popen(["shutdown", "/r", "/t", "5"])
        return "Restarting in 5 seconds."
    elif action == "sleep":
        subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0", "1", "0"])
        return "Going to sleep."
    return f"Unknown power action: {action}. Use shutdown/restart/sleep."


def show_notification(title: str, message: str) -> str:
    """Show a Windows toast notification."""
    safe_title = title.replace("'", "''")
    safe_msg = message.replace("'", "''")
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime] > $null;"
        "$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(1);"
        "$text = $xml.GetElementsByTagName('text');"
        f"$text[0].AppendChild($xml.CreateTextNode('{safe_title}')) > $null;"
        f"$text[1].AppendChild($xml.CreateTextNode('{safe_msg}')) > $null;"
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Jarvis').Show($toast)"
    )
    result = subprocess.run(["powershell", "-command", ps], capture_output=True, text=True)
    if result.returncode == 0:
        return f"Notification shown: {title}"
    return f"Notification failed: {result.stderr.strip()}"


# --- Timer / Reminder ---
_timers: list = []


def set_timer(seconds: int, message: str = "Timer done!") -> str:
    """Set a timer that shows a notification after N seconds."""
    def _timer_cb():
        show_notification("Jarvis Timer", message)

    t = threading.Timer(seconds, _timer_cb)
    t.daemon = True
    t.start()
    _timers.append(t)

    if seconds >= 3600:
        time_str = f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    elif seconds >= 60:
        time_str = f"{seconds // 60}m {seconds % 60}s"
    else:
        time_str = f"{seconds}s"
    return f"Timer set for {time_str}: {message}"
