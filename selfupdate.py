"""Replace the running Redcoatz.exe with the newest one, when the player clicks.

Nothing here runs unless the player clicks update (or passes --self-update).
It works only from the built exe; run from source there is nothing to
replace, and the caller falls back to opening the website.

What it does:

  1. GET <site>/client-version.json for the published version, byte count
     and SHA-256. The site is https://redcoatz.com unless the
     REDCOATZ_UPDATE_SITE environment variable says otherwise (for testing).
  2. GET <site>/Redcoatz.exe into "<this exe>.new" in the exe's own folder,
     hashing as it arrives.
  3. Verify the size and SHA-256 against the published values. A file that
     does not match is deleted and nothing else happens. The download is
     never run before this check passes.
  4. Windows will not let a running program be overwritten, but it can be
     renamed: this exe becomes "<stem>.old.exe" and the new file takes this
     exe's name. Any failure renames the original back.
  5. Release the single-instance lock, start the new exe with
     --after-update <this pid> (and this run's other arguments), and exit.
  6. The new exe waits for this one to exit, then deletes "<stem>.old.exe"
     (cleanup), retrying for a while.

Same folder and same file name, so shortcuts keep working. The download goes
beside the exe rather than into %TEMP% because some virus scanners block
executables written there. Nothing is sent to the site but the request
itself, with the client version as User-Agent.
"""

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

from version import USER_AGENT

#: Where updates come from. REDCOATZ_UPDATE_SITE replaces it for testing; a
#: file:/// folder holding Redcoatz.exe and client-version.json works.
SITE = os.environ.get("REDCOATZ_UPDATE_SITE", "https://redcoatz.com").rstrip("/")
META_URL = SITE + "/client-version.json"
EXE_URL = SITE + "/Redcoatz.exe"


def can_self_update():
    """True when running as the built exe, which is the only thing to replace."""
    return bool(getattr(sys, "frozen", False)) and sys.executable.lower().endswith(".exe")


def _paths():
    """(this exe, the download path "<exe>.new", the backup path "<stem>.old.exe")."""
    exe = os.path.abspath(sys.executable)
    stem, _ext = os.path.splitext(exe)
    return exe, exe + ".new", stem + ".old.exe"


def _get(url, timeout=30):
    """Open a GET to the update site, uncached, with the client's User-Agent."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Cache-Control": "no-cache"})
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_meta():
    """GET client-version.json: {'version', 'bytes', 'sha256', ...} as the site publishes it."""
    with _get(META_URL) as r:
        return json.loads(r.read().decode("utf-8"))


def download(meta, progress=None):
    """Download the new exe to "<exe>.new" and verify it. Returns the path of the checked file.

    The SHA-256 is computed as the bytes arrive and compared with the
    published hash, and the size with the published size. On any mismatch
    or failure the partial file is removed and an exception is raised; the
    file is never left in place unverified. progress(done_bytes,
    total_bytes) is called as it arrives.
    """
    _exe, new, _old = _paths()
    want_size = int(meta.get("bytes") or 0)
    want_hash = (meta.get("sha256") or "").lower()
    if not want_hash:
        raise RuntimeError("the site does not publish a checksum for this version")
    h = hashlib.sha256()
    done = 0
    try:
        with _get(EXE_URL, timeout=60) as r, open(new, "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, want_size)
        if want_size and done != want_size:
            raise RuntimeError(f"download was {done} bytes, expected {want_size}")
        if h.hexdigest() != want_hash:
            raise RuntimeError("download does not match the published checksum")
        return new
    except BaseException:
        try:
            os.remove(new)
        except OSError:
            pass
        raise


def swap_and_restart(new):
    """Rename the checked file into place and start it as a detached process.

    Raises if that fails, having put the original back under its own name;
    the caller then exits only on success.
    """
    exe, _new, old = _paths()
    if os.path.exists(old):
        os.remove(old)                     # a leftover from an earlier update
    os.rename(exe, old)
    try:
        os.rename(new, exe)
    except OSError:
        os.rename(old, exe)                # undo: the original keeps its name
        raise

    # Release the single-instance lock so the new copy never sees "already
    # running" during the moment the two overlap.
    try:
        import singleton
        singleton.release()
    except Exception:                      # noqa: BLE001
        pass

    # A one-file build that starts another copy of itself must reset the
    # PyInstaller environment, or the child tries to reuse this process's
    # unpacked files, which are deleted when this process exits.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("_MEI", "_PYI"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    flags = 0x00000008 | 0x00000200        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen([exe, *_kept_args(), "--after-update", str(os.getpid())],
                         cwd=os.path.dirname(exe), env=env,
                         creationflags=flags, close_fds=True)
    except OSError:
        # Could not start the new one: put the original back so the player is
        # left with a working client under the usual name.
        os.rename(exe, new)
        os.rename(old, exe)
        try:
            os.remove(new)
        except OSError:
            pass
        raise


def _kept_args():
    """This run's command line for the new copy, minus our own update flags."""
    out, skip = [], False
    for a in sys.argv[1:]:
        if skip:
            skip = False
            continue
        if a == "--after-update":
            skip = True
            continue
        if a.startswith("--after-update=") or a == "--self-update":
            continue
        out.append(a)
    return out


def wait_for_exit(pid, timeout=30):
    """Block until process `pid` has exited, or `timeout` seconds pass."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        h = k32.OpenProcess(0x00100000, False, int(pid))   # SYNCHRONIZE
        if not h:
            return                         # already gone
        try:
            k32.WaitForSingleObject(ctypes.c_void_p(h), int(timeout * 1000))
        finally:
            k32.CloseHandle(ctypes.c_void_p(h))
    except Exception:                      # noqa: BLE001
        pass


def cleanup(log=print):
    """Delete "<stem>.old.exe" and "<exe>.new" if an update left them behind. Never raises; runs in the background.

    The old exe can only be deleted once it has exited, so this retries for
    a while; anything still there is tried again on the next start.
    """
    if not can_self_update():
        return

    def run():
        _exe, new, old = _paths()
        for path in (old, new):
            for _ in range(30):
                if not os.path.exists(path):
                    break
                try:
                    os.remove(path)
                    log(f"update: removed {os.path.basename(path)}")
                    break
                except OSError:
                    time.sleep(1)

    threading.Thread(target=run, daemon=True, name="update-cleanup").start()


def run_gui(parent, on_restart, fallback=SITE, log=print):
    """Download, verify and swap, with a small progress window over `parent`.

    on_restart() is called once the new exe has been started; it must make
    this process exit. On any failure the window says what went wrong and
    offers to open the website in the browser instead.
    """
    import tkinter as tk
    import webbrowser

    bg, fg, dim, gold = "#10131C", "#ECE6D7", "#8A93A6", "#E4C67E"
    win = tk.Toplevel(parent)
    win.title("Redcoatz update")
    win.configure(bg=bg)
    win.attributes("-topmost", True)
    win.resizable(False, False)
    head = tk.Label(win, text="Updating Redcoatz", bg=bg, fg=gold,
                    font=("Georgia", 13, "bold"))
    head.pack(padx=30, pady=(20, 8))
    msg = tk.Label(win, text="Checking for the latest version...", bg=bg,
                   fg=fg, font=("Georgia", 10), justify="center", wraplength=380)
    msg.pack(padx=30)
    row = tk.Frame(win, bg=bg)
    row.pack(pady=(14, 18))

    state = {"text": None, "done": False, "error": None}

    def work():
        try:
            meta = fetch_meta()
            ver = meta.get("version", "?")
            state["text"] = f"Downloading v{ver}..."

            def progress(done, total):
                if total:
                    state["text"] = (f"Downloading v{ver}... "
                                     f"{done * 100 // total}%")
            new = download(meta, progress)
            state["text"] = f"Installing v{ver}..."
            swap_and_restart(new)
            log(f"update: v{ver} installed and started")
            state["done"] = True
        except Exception as e:             # noqa: BLE001
            log(f"update failed: {e!r}")
            state["error"] = str(e) or type(e).__name__

    def site():
        try:
            webbrowser.open_new_tab(fallback)
        except Exception:                  # noqa: BLE001
            pass
        win.destroy()

    def poll():
        if not win.winfo_exists():
            return
        if state["text"]:
            msg.config(text=state["text"])
        if state["done"]:
            msg.config(text="Restarting with the new version...")
            win.after(300, on_restart)
            return
        if state["error"]:
            head.config(text="The update did not complete")
            msg.config(text=("Nothing was changed; this version keeps "
                             f"running.\n\n{state['error']}"), fg=dim)
            tk.Button(row, text="Get it from the website", command=site,
                      bg="#AE3238", fg="#FFFBF4", relief="flat",
                      font=("Georgia", 10, "bold")).pack(side="left", padx=6)
            tk.Button(row, text="Close", command=win.destroy, bg="#171C29",
                      fg="#CBC4B2", relief="flat",
                      font=("Georgia", 10)).pack(side="left", padx=6)
            return
        win.after(150, poll)

    threading.Thread(target=work, daemon=True, name="self-update").start()
    poll()
    return win
