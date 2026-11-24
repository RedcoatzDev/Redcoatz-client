"""Set up NTW3 from nothing: find what is installed, fetch and run what is not.

The client's setup screen sits on top of this; nothing here draws.

What to install comes from install_manifest.json in the public repository
https://github.com/RedcoatzDev/Redcoatz-client: four official Lordz files in
order (the 9.4 core, the 9.5 and 9.6 patches, each a
WinRAR self-extractor, then the 9.6.1 texture tool), each with its size and
SHA-256, how to run it, every file it puts down, and the few files that prove
it is installed. This module only knows how to look, download, check and run;
a new patch is a server change.

Steps before those four are not ours to do and are only detected: Steam, and
Napoleon: Total War itself (Steam app 34030), which the player installs from
Steam.

What it touches. Reads the registry and Steam's own files to find the game,
and the game folder to see what is installed. Downloads into
<game>\\data\\NTW3\\redcoatz_install (never %TEMP%), resuming a part file, and
refuses to run anything whose SHA-256 is not the one the manifest gives. Runs
each official file unchanged, with its own silent switches, and checks every
file it should have put down afterwards. Deletes a download once its step is
checked. Nothing is written outside the game folder.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zlib

from version import USER_AGENT

STEAM_APP = 34030
STAGING_REL = os.path.join("data", "NTW3", "redcoatz_install")

# Step states.
INSTALLED = "installed"   # every detect file present at its size
MISSING = "missing"       # none of them present
PARTIAL = "partial"       # some present or wrong size: interrupted, or a file
                          # removed by hand; running the step again repairs it
BLOCKED = "blocked"       # an earlier step is not installed
UNKNOWN = "unknown"       # 9.6.1 only: the packs are neither its inputs nor
                          # its outputs, so it would refuse

#: The tool exits like this; see the manifest's run.exit.
TOOL_DONE, TOOL_REFUSED, TOOL_FAILED, TOOL_USAGE = 0, 1, 2, 3


#: The pause between download retries; replaceable in tests.
_sleep = time.sleep


class SetupError(Exception):
    """A step could not be done. The message is for the player."""


# --------------------------------------------------------------------------
# The manifest

#: The public repository the install list is read from, and the list itself.
#: The checksums a player's machine trusts come from here, not from the
#: ladder's server or the bucket the files are served from: a takeover of
#: either can then only serve files this module refuses to run.
REPO_URL = "https://github.com/RedcoatzDev/Redcoatz-client"
MANIFEST_URL = ("https://raw.githubusercontent.com/RedcoatzDev/Redcoatz-client/"
                "main/install_manifest.json")


def with_urls(doc):
    """The manifest with each download's url filled in from its base."""
    from urllib.parse import quote
    base = str(doc.get("base") or "").rstrip("/")
    for step in doc["steps"]:
        d = step["download"]
        if not d.get("url"):
            if not base:
                raise SetupError("The install list names no place to download from.")
            d["url"] = base + "/" + "/".join(quote(p) for p in d["path"].split("/"))
    return doc


def fetch_manifest(url=MANIFEST_URL, timeout=15):
    """The install list from the public repository. Raises on any failure:
    without it there is nothing to do, and guessing would be worse."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        doc = json.loads(r.read().decode("utf-8"))
    if doc.get("schema") != 1 or not doc.get("steps"):
        raise SetupError("The install list on GitHub is in a form this client cannot read. "
                         "Update Redcoatz.")
    return with_urls(doc)


def confirm_text(steps, doc):
    """What the player is asked before the first download: every file, its
    size, where it comes from and where its checksum comes from."""
    lines = ["  %s  (%.1f GB)" % (s["download"]["name"], s["download"]["bytes"] / 1e9)
             if s["download"]["bytes"] >= 1e8 else
             "  %s  (%d MB)" % (s["download"]["name"], max(1, s["download"]["bytes"] // 1_000_000))
             for s in steps]
    return ("Redcoatz will download these official NTW3 files and run them on your "
            "computer, installing into your Napoleon: Total War folder:\n\n%s\n\n"
            "They are served from %s. Before it runs, each one is checked against the "
            "MD5 the Lordz publish for it, and is deleted unrun if it differs. The expected "
            "values are in the open source at\n%s\n\nAre you sure you want to proceed?"
            % ("\n".join(lines), str(doc.get("base") or "the Redcoatz file host"), REPO_URL))


# --------------------------------------------------------------------------
# Steam and the game

def steam_roots():
    """Every Steam library on this machine. Empty off Windows."""
    try:
        import gamelaunch
        return gamelaunch._steam_roots()
    except Exception:                          # noqa: BLE001
        return []


def napoleon(roots=None):
    """(state, game folder or None) for Steam app 34030.

    state is "installed" when Steam's own manifest says the app is fully
    installed (StateFlags 4) and the folder exists, "updating" when the
    manifest exists in any other state, "missing" when no library has it.
    """
    for root in roots if roots is not None else steam_roots():
        acf = os.path.join(root, "steamapps", f"appmanifest_{STEAM_APP}.acf")
        try:
            with open(acf, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        m = re.search(r'"installdir"\s*"([^"]+)"', text)
        folder = os.path.join(root, "steamapps", "common",
                              m.group(1) if m else "Napoleon Total War")
        flags = re.search(r'"StateFlags"\s*"(\d+)"', text)
        if flags and int(flags.group(1)) == 4 and os.path.isdir(folder):
            return "installed", folder
        return "updating", folder
    return "missing", None


def steam():
    """"ready", "starting" or "down", from gamelaunch; "unknown" off Windows."""
    try:
        import gamelaunch
        return gamelaunch.steam_state()
    except Exception:                          # noqa: BLE001
        return "unknown"


# --------------------------------------------------------------------------
# What is installed

def _crc32(path, chunk=8 << 20):
    v = 0
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            v = zlib.crc32(block, v)
    return "%08x" % v


def _file_ok(game, entry, check_crc=True):
    """Does game/<entry.path> exist at entry.bytes (and entry.crc32 if given)?"""
    p = os.path.join(game, *entry["path"].split("/"))
    try:
        if os.path.getsize(p) != entry["bytes"]:
            return False
    except OSError:
        return None                           # absent, as distinct from wrong
    if check_crc and entry.get("crc32"):
        try:
            return _crc32(p) == entry["crc32"]
        except OSError:
            return False
    return True


def detect_step(step, game, check_crc=True):
    """The state of one step in this game folder, ignoring the steps before it."""
    results = [_file_ok(game, d, check_crc) for d in step["detect"]]
    if all(r is True for r in results):
        return INSTALLED
    if step["run"]["kind"] == "tool":
        # Not installed: would the tool accept what is there? It refuses
        # anything but the packs the 9.6 chain leaves (or ours, 23e-repaired).
        for inp in step.get("inputs", []):
            p = os.path.join(game, *inp["path"].split("/"))
            try:
                size = os.path.getsize(p)
            except OSError:
                return MISSING
            if not any(a["bytes"] == size for a in inp["accepts"]):
                outs = {o["path"]: o["bytes"] for o in step.get("outputs", [])}
                if outs.get(inp["path"]) != size:
                    return UNKNOWN
        return MISSING
    if all(r is None for r in results):
        return MISSING
    return PARTIAL


def detect(manifest, game, check_crc=True):
    """[(step, state)] in order. A step after one that is not installed is
    BLOCKED whatever its own files say: the patches only stack in order."""
    out, ok = [], True
    for step in manifest["steps"]:
        state = detect_step(step, game, check_crc) if game else MISSING
        if not ok and state != INSTALLED:
            state = BLOCKED
        out.append((step, state))
        ok = ok and state == INSTALLED
    return out


#: One file from each step, for a look at start-up that needs no install
#: list and no network. Presence only: the setup screen does the real check.
MARKERS = (
    "data/NTW3/Version94/Maps/maps_custom.pack",
    "data/NTW3/Version94/Data/txt.9401.pack",
    "data/NTW3/Version95/Maps/maps_defs.9501.pack",
    "data/NTW3/Version95/Data/db.9501.pack",
    "data/NTW3/Version96/Maps/maps_defs.9601.pack",
    "data/NTW3/Version96/Data/db.9601.pack",
    "data/NTW3/Launcher/NTW3 v9.6.exe",
)


def missing_markers(game=None):
    """The MARKERS this install lacks. [] when all are there, and also when
    the game itself cannot be found, which is the caller's other question."""
    if game is None:
        _state, game = napoleon()
    if not game:
        return []
    return [m for m in MARKERS if not os.path.isfile(os.path.join(game, *m.split("/")))]


def todo(states, chosen=None):
    """The steps to run, in order: every one not installed, or only the chosen
    ids for a custom install. A chosen step that needs an earlier, unchosen
    and uninstalled step pulls that step in, because it cannot run without it."""
    ids = [s["id"] for s, _ in states]
    want = set(ids if chosen is None else chosen)
    if chosen is not None and want:
        last = max(ids.index(i) for i in want if i in ids)
        want |= {s["id"] for s, st in states[:last] if st != INSTALLED}
    return [s for s, st in states if s["id"] in want and st != INSTALLED]


# --------------------------------------------------------------------------
# Room and rights

def bytes_needed(step):
    """Free space a step needs while it runs: the download, plus what it puts
    down (an extractor) or the largest pack held twice (the 9.6.1 tool)."""
    return step["download"]["bytes"] + max(step.get("unpacked_bytes", 0),
                                           step.get("work_bytes", 0))


def free_bytes(path):
    """Free space on path's drive, walking up to a folder that exists."""
    p = os.path.abspath(path)
    while not os.path.isdir(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return shutil.disk_usage(p).free


def writable(game):
    """Can this account create and delete a file under the game's data folder?"""
    target = os.path.join(game, "data")
    try:
        os.makedirs(target, exist_ok=True)
        probe = os.path.join(target, ".redcoatz_write_test")
        with open(probe, "wb") as f:
            f.write(b"x")
        os.remove(probe)
        return True
    except OSError:
        return False


def check_room(steps, game):
    """None when there is room and the folder is writable, else a sentence
    for the player. Checked before the first byte is downloaded."""
    if not writable(game):
        return ("Redcoatz cannot write to the game folder:\n\n%s\n\n"
                "Close Redcoatz, right-click it, choose Run as administrator, "
                "and start the setup again." % game)
    need = sum(bytes_needed(s) for s in steps)
    have = free_bytes(game)
    if have < need:
        return ("The setup needs %.1f GB free on the game's drive and there is "
                "%.1f GB. Free some space, then start the setup again."
                % (need / 1e9, have / 1e9))
    return None


# --------------------------------------------------------------------------
# Download

def staging(game):
    """Where downloads go: inside the game folder, never %TEMP%."""
    p = os.path.join(game, STAGING_REL)
    os.makedirs(p, exist_ok=True)
    return p


def digests(path, chunk=16 << 20, progress=None):
    """(md5, sha256) of a file, in one read."""
    md5, sha = hashlib.md5(), hashlib.sha256()
    done = 0
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            md5.update(block)
            sha.update(block)
            done += len(block)
            if progress:
                progress(done)
    return md5.hexdigest(), sha.hexdigest()


def sha256_of(path, chunk=16 << 20, progress=None):
    return digests(path, chunk, progress)[1]


def mismatch(d, path):
    """Why a file is not the official one, or None when it is.

    The MD5 is the Lordz' own: the value they publish beside the file (see
    md5_source in the install list). A file swapped on the host we serve
    from cannot pass, and anyone can compare the expected value in this open
    source with theirs. MD5's known weakness is making two new files that
    collide; it cannot be used to match a hash someone else already
    published. The SHA-256 is checked too, from the same read.
    """
    md5, sha = digests(path)
    if d.get("md5") and md5 != d["md5"]:
        return "its MD5 is %s, not the %s published for it" % (md5, d["md5"])
    if d.get("sha256") and sha != d["sha256"]:
        return "its SHA-256 differs from the install list's"
    return None


def download(step, folder, progress=None, stop=None, opener=urllib.request.urlopen,
             chunk=1 << 20, timeout=60):
    """Fetch step's file into folder, resuming a .part left by an earlier try,
    and return its path once its SHA-256 matches. progress(done, total) is
    called as bytes arrive; stop() returning True abandons the download (the
    part file stays, so the next try resumes)."""
    d = step["download"]
    final = os.path.join(folder, d["name"])
    total = d["bytes"]
    if os.path.isfile(final) and os.path.getsize(final) == total:
        if mismatch(d, final) is None:
            return final
        os.remove(final)
    part = final + ".part"
    have = os.path.getsize(part) if os.path.isfile(part) else 0
    if have > total:
        os.remove(part)
        have = 0
    tries = 0
    while have < total:
        if stop and stop():
            raise SetupError("Stopped. The download resumes from here next time.")
        req = urllib.request.Request(d["url"], headers={"User-Agent": USER_AGENT,
                                                        "Range": f"bytes={have}-"})
        try:
            with opener(req, timeout=timeout) as r:
                status = getattr(r, "status", None) or r.getcode()
                if have and status != 206:
                    # The server ignored the range: start over rather than
                    # append the whole file to a partial one.
                    have = 0
                with open(part, "ab" if have else "wb") as f:
                    while True:
                        if stop and stop():
                            raise SetupError("Stopped. The download resumes from here next time.")
                        block = r.read(chunk)
                        if not block:
                            break
                        f.write(block)
                        have += len(block)
                        if progress:
                            progress(have, total)
            tries = 0
        except SetupError:
            raise
        except urllib.error.HTTPError as e:
            # The host answered and said no: retrying will not change that.
            # Only a timeout or rate limit is worth waiting out.
            if e.code not in (408, 429) and e.code < 500:
                raise SetupError("The file host does not have %s (HTTP %d). Try again later; "
                                 "if it keeps happening, tell the Redcoatz team."
                                 % (d["name"], e.code)) from e
            tries += 1
            if tries > 8:
                raise SetupError("The file host keeps refusing %s (HTTP %d). Try again later."
                                 % (d["name"], e.code)) from e
            _sleep(min(30, 2 ** tries))
            have = os.path.getsize(part) if os.path.isfile(part) else 0
        except Exception as e:                 # noqa: BLE001 - network
            tries += 1
            if tries > 8:
                raise SetupError("The download of %s keeps failing (%s). Check the "
                                 "connection and start the setup again; it resumes "
                                 "where it stopped." % (d["name"], e)) from e
            _sleep(min(30, 2 ** tries))
            have = os.path.getsize(part) if os.path.isfile(part) else 0
    if os.path.getsize(part) != total:
        os.remove(part)
        raise SetupError("%s arrived at the wrong size; it will be downloaded again." % d["name"])
    why = mismatch(d, part)
    if why:
        os.remove(part)
        raise SetupError("%s is not the official file (%s), so it was deleted and not run. "
                         "Start the setup again to fetch it afresh; if it happens again, "
                         "tell the Redcoatz team." % (d["name"], why))
    os.replace(part, final)
    return final


# --------------------------------------------------------------------------
# Run and check

def command_line(step, exe, game, log):
    """The command line for a step: the exe, quoted, then the manifest's args
    with {game} and {log} filled in. A string, not a list: the WinRAR
    self-extractor reads -d"<folder>" itself and expects that exact quoting."""
    args = step["run"]["args"].replace("{game}", game.rstrip("\\/")).replace("{log}", log)
    return f'"{exe}" {args}'


#: Windows' "the requested operation requires elevation".
ELEVATION_REQUIRED = 740


class _Elevated:
    """A program started through Windows' own "run as administrator"
    question, with the one method run() needs. Some of the official files ask
    for administrator rights, and an ordinary start of those is refused."""

    def __init__(self, exe, args, cwd):
        import ctypes
        from ctypes import wintypes

        class SHELLEXECUTEINFOW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
                        ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                        ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                        ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                        ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                        ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                        ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE),
                        ("hProcess", wintypes.HANDLE)]

        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self._k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._dword = wintypes.DWORD
        self._byref = ctypes.byref
        info = SHELLEXECUTEINFOW()
        info.cbSize = ctypes.sizeof(info)
        info.fMask = 0x40                     # SEE_MASK_NOCLOSEPROCESS
        info.lpVerb = "runas"
        info.lpFile = exe
        info.lpParameters = args
        info.lpDirectory = cwd
        info.nShow = 1
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
        if not shell32.ShellExecuteExW(ctypes.byref(info)) or not info.hProcess:
            err = ctypes.get_last_error()
            if err == 1223:                   # the player said no to the question
                raise SetupError("Windows asked for administrator rights to run %s and the "
                                 "answer was no. Nothing was changed. Click Install to try "
                                 "again." % os.path.basename(exe))
            raise SetupError("Windows would not start %s as administrator (error %s)."
                             % (os.path.basename(exe), err))
        self._h = info.hProcess
        self._code = None

    def poll(self):
        if self._code is None and self._k32.WaitForSingleObject(self._h, 0) == 0:
            code = self._dword()
            self._k32.GetExitCodeProcess(self._h, self._byref(code))
            self._code = code.value
            self._k32.CloseHandle(self._h)
        return self._code


def start(step, exe, game, log, popen=subprocess.Popen):
    """Start a step's program: plainly, and through Windows' administrator
    question when Windows says the program needs that."""
    try:
        return popen(command_line(step, exe, game, log), cwd=os.path.dirname(exe))
    except OSError as e:
        if getattr(e, "winerror", None) != ELEVATION_REQUIRED:
            raise
    args = step["run"]["args"].replace("{game}", game.rstrip("\\/")).replace("{log}", log)
    return _Elevated(exe, args, os.path.dirname(exe))


def run(step, exe, game, log, popen=subprocess.Popen, progress=None, poll=1.0):
    """Run one official file and wait. Raises SetupError on a refusal or a
    failure. progress(done, total) follows bytes landing in the game folder,
    since neither installer reports its own."""
    targets = {os.path.join(game, *f["path"].split("/")): f["bytes"] for f in step.get("files", [])}
    total = step.get("unpacked_bytes", 0)

    def size(p):
        try:
            return os.path.getsize(p)
        except OSError:
            return -1

    # A file counts once it has been seen to change: a patch overwrites files
    # that are already there at the same size, and the extractor stamps each
    # one with the archive's own date, so neither size nor date alone says
    # what has been written. Fresh files are seen appearing.
    last = {p: size(p) for p in targets}
    moved = set()
    if progress:
        # Said once up front, so a step that reports nothing (the 9.6.1
        # tool writes in place) still shows that it is running.
        progress(0, total)
    proc = start(step, exe, game, log, popen=popen)
    while True:
        code = proc.poll()
        if code is not None:
            break
        if progress and targets and total:
            for p in targets:
                s = size(p)
                if s != last[p]:
                    moved.add(p)
                    last[p] = s
            progress(min(total, sum(min(max(last[p], 0), targets[p]) for p in moved)), total)
        time.sleep(poll)
    if progress and total:
        progress(total, total)
    if step["run"]["kind"] == "tool":
        if code == TOOL_REFUSED:
            raise SetupError("The 9.6.1 tool refused and changed nothing. Its log says why:\n\n%s" % log)
        if code != TOOL_DONE:
            raise SetupError("The 9.6.1 tool stopped partway (exit %s). Its log says what state "
                             "the packs are in:\n\n%s" % (code, log))
    elif code != 0:
        raise SetupError("%s stopped with exit code %s." % (step["title"], code))
    return code


def check(step, game):
    """[paths] the step should have put down and did not, at the right size.
    Empty when the step is complete."""
    entries = step.get("files") or step.get("outputs") or step["detect"]
    return [e["path"] for e in entries if _file_ok(game, e, check_crc=False) is not True]


def explain_missing(step, missing):
    """A sentence for the player about files a step did not put down."""
    exes = [m for m in missing if m.lower().endswith(".exe")]
    msg = "%s did not finish: %d file(s) are missing or the wrong size, for example\n\n%s" % (
        step["title"], len(missing), "\n".join(missing[:5]))
    if exes:
        msg += ("\n\nMissing programs are usually removed by antivirus. Restore %s from "
                "quarantine and start the setup again." % os.path.basename(exes[0]))
    return msg


def do_step(step, game, progress=None, stop=None, log=print, opener=urllib.request.urlopen,
            popen=subprocess.Popen):
    """Download, check, run and verify one step. progress(phase, done, total)
    with phase "download", "verify" or "install". Raises SetupError."""
    folder = staging(game)
    log(f"setup: {step['id']} download")
    exe = download(step, folder, progress=(lambda d, t: progress("download", d, t)) if progress else None,
                   stop=stop, opener=opener)
    log(f"setup: {step['id']} run")
    tool_log = os.path.join(folder, step["id"] + ".log")
    run(step, exe, game, tool_log, popen=popen,
        progress=(lambda d, t: progress("install", d, t)) if progress else None)
    missing = check(step, game)
    if missing:
        raise SetupError(explain_missing(step, missing))
    log(f"setup: {step['id']} checked")
    try:
        os.remove(exe)
    except OSError:
        pass
    return True


def cleanup(game):
    """Remove the staging folder once nothing is left in it."""
    p = os.path.join(game, STAGING_REL)
    try:
        if os.path.isdir(p) and not [n for n in os.listdir(p) if not n.endswith(".log")]:
            shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


if __name__ == "__main__":
    # python ntw3setup.py [manifest url]: what is installed, nothing changed.
    m = fetch_manifest(sys.argv[1]) if len(sys.argv) > 1 else fetch_manifest()
    st, game = napoleon()
    print(f"Steam: {steam()}   Napoleon: {st}   {game or ''}")
    if game:
        for s, state in detect(m, game):
            print(f"  {s['version']:6} {state:9} {s['title']}")
        print(f"  free: {free_bytes(game) / 1e9:.1f} GB   writable: {writable(game)}")
