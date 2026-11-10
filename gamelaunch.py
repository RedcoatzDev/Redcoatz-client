"""Find the NTW3 launcher and start it.

Everything here only reads: the Steam registry keys (HKCU Software/Valve/Steam
SteamPath, HKLM SOFTWARE/WOW6432Node/Valve/Steam InstallPath) and each
library's steamapps/libraryfolders.vdf to find the Steam libraries, the
game's data/NTW3/Launcher folder to find the newest "NTW3 v*.exe", and the
process list to see whether the game is already up. The only things written
are the launcher process itself, a Steam process when Steam was not running
(see ensure_steam), and, in make_desktop_shortcut(), a .lnk on the Desktop.

The client starts the game rather than the other way round because the NTW3
launcher belongs to the Lordz team, and a checkbox there would tie our
releases to theirs. It also means the ladder is running before the game and
can take its baseline of memory. The launcher is searched for rather than
hardcoded because Steam libraries live on other drives and the launcher's
filename carries the mod version; --game-path overrides the search.
"""

import glob
import os
import re
import subprocess
import sys

RELATIVE = os.path.join("steamapps", "common", "Napoleon Total War",
                        "data", "NTW3", "Launcher")


def _steam_roots():
    """Every Steam library on this machine, best guesses first."""
    roots = []

    # Where Steam itself says it is.
    try:
        import winreg
        for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                          (winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\WOW6432Node\Valve\Steam")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for name in ("SteamPath", "InstallPath"):
                        try:
                            roots.append(winreg.QueryValueEx(k, name)[0])
                        except OSError:
                            pass
            except OSError:
                pass
    except ImportError:
        pass                                    # not Windows

    roots += [r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"]

    # Libraries on other drives are listed in this file, one "path" per entry.
    extra = []
    for r in list(roots):
        vdf = os.path.join(r, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, "r", encoding="utf-8", errors="replace") as f:
                extra += re.findall(r'"path"\s*"([^"]+)"', f.read())
        except OSError:
            pass
    roots += [p.replace("\\\\", "\\") for p in extra]

    seen, out = set(), []
    for r in roots:
        r = os.path.normpath(r)
        if r.lower() not in seen and os.path.isdir(r):
            seen.add(r.lower())
            out.append(r)
    return out


def _version_key(path):
    """Sort key from the filename, so v9.10 beats v9.6."""
    m = re.search(r"v(\d+)\.(\d+)", os.path.basename(path))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def find_launcher(explicit=None):
    """Full path to the newest NTW3 launcher, or None."""
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    found = []
    for root in _steam_roots():
        found += glob.glob(os.path.join(root, RELATIVE, "NTW3 v*.exe"))
    if not found:
        return None
    return max(found, key=_version_key)


#: The client's old executable name, excluded from the running check below
#: because it matches the NTW3* filter.
OURS = ("ntw3ladder",)


def already_running():
    """True if the game or its launcher is up, so we do not start a second.

    The tasklist filter "NTW3*" also matches the client's old name
    ntw3ladder.exe, so that is excluded: a copy left running under the old
    name would otherwise make this one think the game was up.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from lobby_memory import find_game
        if find_game()[0]:
            return True
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["tasklist", "/fi", "imagename eq NTW3*", "/fo", "csv", "/nh"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        for line in out.splitlines():
            name = line.split('","')[0].strip('"').lower()
            if not name.endswith(".exe"):
                continue
            if any(name.startswith(o) for o in OURS):
                continue                     # that is us
            return True
    except Exception:
        pass
    return False


#: How long to wait for Steam to be signed in before launching anyway.
#: Steam registers its pid within a few seconds of starting and keeps
#: booting for many more; the overlay is injected by the signed-in client,
#: so the game must not start before that.
STEAM_START_TIMEOUT = 90


def _active_process():
    """Steam's own record of itself, the ActiveProcess key under its HKCU
    Software hive: (pid, active_user). Both 0 when Steam is down or signed
    out; Steam zeroes ActiveUser on sign-out and on exit, and the pid is
    checked against the process list because the key is left behind when
    Steam is killed.
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Valve\Steam\ActiveProcess") as k:
            pid = int(winreg.QueryValueEx(k, "pid")[0])
            try:
                user = int(winreg.QueryValueEx(k, "ActiveUser")[0])
            except OSError:
                user = 0
    except OSError:
        return 0, 0
    if pid <= 0:
        return 0, 0
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        h = k32.OpenProcess(0x1000, False, pid)      # QUERY_LIMITED_INFORMATION
        if not h:
            return 0, 0
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code)):
                return 0, 0
            if code.value != 259:                    # not STILL_ACTIVE
                return 0, 0
        finally:
            k32.CloseHandle(ctypes.c_void_p(h))
    except Exception:
        pass
    return pid, user


def _steam_pid():
    """Steam's running pid, or 0."""
    return _active_process()[0]


def steam_running():
    """True when a live Steam process is registered."""
    return _steam_pid() != 0


def steam_state():
    """One of "down", "starting", "ready".

    "starting" is a live Steam process that has not signed anybody in yet:
    ActiveUser is 0 until the client has finished logging in, which is also
    when its overlay is ready to be injected. "ready" is a signed-in Steam.
    """
    pid, user = _active_process()
    if not pid:
        return "down"
    return "ready" if user else "starting"


def _steam_exe():
    """steam.exe, from the registry, or None."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            for name in ("SteamExe", "SteamPath"):
                try:
                    v = winreg.QueryValueEx(k, name)[0]
                except OSError:
                    continue
                p = v if v.lower().endswith(".exe") else os.path.join(v, "steam.exe")
                if os.path.isfile(p):
                    return os.path.normpath(p)
    except OSError:
        pass
    for root in _steam_roots():
        p = os.path.join(root, "steam.exe")
        if os.path.isfile(p):
            return p
    return None


def ensure_steam(log=print, status=None):
    """Start Steam if it is not signed in, and wait until it is, before the game.

    Why: when Steam is down, the NTW3 launcher's steam:// link starts Steam
    and the game together, and Steam injects its overlay into a game that
    is already seconds into loading, with threads running and heaps busy.
    Measured over two weeks on one machine (1 November 2026): about one
    launch in three crashed with heap corruption inside fifteen seconds
    when Steam was cold-started by the launch, against about one in sixty
    when Steam was already up. Starting Steam first and waiting for it to
    sign in puts the overlay in at the game's first instruction, as on
    every warm launch.

    Waits on Steam's own signal, not a timer: ActiveUser in the registry
    turns non-zero when the client has signed in. `status(text)` is called
    with each change of state, for a window to show; `log` gets the same
    lines. Never raises: a Steam that will not start just means the game
    is launched as before.
    """
    import time

    def say(text):
        log(text)
        if status:
            status(text)

    state = steam_state()
    if state == "ready":
        return "Steam is signed in"
    if state == "down":
        exe = _steam_exe()
        if not exe:
            return "Steam is not running and steam.exe was not found; launching anyway"
        say("Steam is not running; starting it")
        try:
            subprocess.Popen([exe, "-silent"], cwd=os.path.dirname(exe),
                             close_fds=True)
        except OSError as e:
            return f"could not start Steam ({e}); launching anyway"
    else:
        say("Steam is starting; waiting for it to sign in")
    began = time.monotonic()
    last = state
    while time.monotonic() - began < STEAM_START_TIMEOUT:
        state = steam_state()
        if state == "ready":
            return "Steam signed in after %.0f s" % (time.monotonic() - began)
        if state != last:
            last = state
            say("Steam is up (pid %d); waiting for it to sign in" % _steam_pid()
                if state == "starting" else "Steam is not running yet")
        time.sleep(0.5)
    return f"Steam did not sign in within {STEAM_START_TIMEOUT} s; launching anyway"


#: The message start_game returns for a game already up, compared by the
#: caller, and how long a caller may keep watching such a game in case it
#: was on its way out. Napoleon takes several seconds to leave the process
#: list after its window has gone.
ALREADY_RUNNING = "NTW3 is already running"
EXIT_GRACE = 20


def start_game(explicit=None, log=print, status=None):
    """Launch the NTW3 launcher, with Steam signed in first. Returns (started, message).

    `status` is passed to ensure_steam for a window to show the wait.
    """
    if already_running():
        return False, ALREADY_RUNNING

    exe = find_launcher(explicit)
    if not exe:
        return False, ("could not find the NTW3 launcher; "
                       "pass --game-path to point at it")
    log(f"steam: {ensure_steam(log, status)}")
    try:
        # Started from its own folder: the launcher reads prefs.ini and rsc/
        # relative to itself.
        subprocess.Popen([exe], cwd=os.path.dirname(exe),
                         close_fds=True)
        return True, f"started {os.path.basename(exe)}"
    except OSError as e:
        return False, f"could not start {os.path.basename(exe)}: {e}"


def make_desktop_shortcut(target=None, name="NTW3 Ladder"):
    """Put a shortcut to this client on the Desktop. Returns (made, message).

    Built through PowerShell's WScript.Shell rather than pywin32, to avoid a
    dependency.
    """
    target = target or (sys.executable if getattr(sys, "frozen", False)
                        else os.path.abspath(sys.argv[0]))
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    if not os.path.isdir(desktop):
        return False, "could not find the Desktop folder"
    link = os.path.join(desktop, f"{name}.lnk")

    workdir = os.path.dirname(target)
    args = ""
    if not getattr(sys, "frozen", False):
        # Running from source: the shortcut has to invoke python on the script.
        args = f'"{target}"'
        target = sys.executable

    ps = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
        f"'{link}'); "
        f"$s.TargetPath = '{target}'; "
        + (f"$s.Arguments = '{args}'; " if args else "")
        + f"$s.WorkingDirectory = '{workdir}'; "
        "$s.Description = 'NTW3 Ladder - starts the ladder and the game'; "
        "$s.Save()"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-Command", ps],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return False, (r.stderr or "powershell failed").strip()[:200]
        return True, link
    except Exception as e:
        return False, str(e)
