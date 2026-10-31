"""Find the NTW3 launcher and start it.

Everything here only reads: the Steam registry keys (HKCU Software/Valve/Steam
SteamPath, HKLM SOFTWARE/WOW6432Node/Valve/Steam InstallPath) and each
library's steamapps/libraryfolders.vdf to find the Steam libraries, the
game's data/NTW3/Launcher folder to find the newest "NTW3 v*.exe", and the
process list to see whether the game is already up. The only things written
are the launcher process itself and, in make_desktop_shortcut(), a .lnk on
the Desktop.

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


def start_game(explicit=None):
    """Launch the NTW3 launcher. Returns (started, message)."""
    if already_running():
        return False, "NTW3 is already running"

    exe = find_launcher(explicit)
    if not exe:
        return False, ("could not find the NTW3 launcher; "
                       "pass --game-path to point at it")
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
