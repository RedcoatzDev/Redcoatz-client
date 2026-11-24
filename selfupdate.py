"""Replace the running Redcoatz.exe with the newest one, when the player clicks.

Nothing here runs unless the player clicks update (or passes --self-update).
It works only from the built exe; run from source there is nothing to
replace, and the caller falls back to opening the website.

Where updates come from: the public GitHub repository
https://github.com/RedcoatzDev/Redcoatz-client, never redcoatz.com. Each
release there carries a Redcoatz.exe that GitHub itself built from the tagged
source (.github/workflows/verify.yml), and GitHub publishes that file's size
and SHA-256. So the code a player updates to is code anyone can read, built
by a machine we do not control, and a takeover of the ladder's website,
server or domain cannot push an update.

What it does:

  1. GET the latest release of that repository from GitHub's API: its
     version, and the Redcoatz.exe attached to it with GitHub's size and
     SHA-256. An exe that was not uploaded by GitHub's own build
     (github-actions[bot]), or that comes without GitHub's checksum, is
     refused: that would be a file attached by hand.
  2. Ask the player, naming the repository and the version. Nothing is
     downloaded until they say yes. Then GET that Redcoatz.exe into
     "<this exe>.new" in the exe's own folder, hashing as it arrives.
  3. Verify the size and SHA-256 against GitHub's. A file that does not
     match is deleted and nothing else happens. The download is never run
     before this check passes.
  4. Windows will not let a running program be overwritten, but it can be
     renamed: this exe becomes "<stem>.old.exe" and the new file takes this
     exe's name. Any failure renames the original back.
  5. Release the single-instance lock, start the new exe with
     --after-update <this pid> (and this run's other arguments), and exit.
  6. The new exe waits for this one to exit, then deletes "<stem>.old.exe"
     (cleanup), retrying for a while.

Same folder and same file name, so shortcuts keep working. The download goes
beside the exe rather than into %TEMP% because some virus scanners block
executables written there. Nothing is sent to GitHub but the requests
themselves, with the client version as User-Agent. REDCOATZ_UPDATE_API
replaces the GitHub API address, for testing only.
"""

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

from version import USER_AGENT, VERSION

#: The public repository updates come from, and its latest release.
REPO = "RedcoatzDev/Redcoatz-client"
REPO_URL = f"https://github.com/{REPO}"
API_URL = os.environ.get("REDCOATZ_UPDATE_API",
                         f"https://api.github.com/repos/{REPO}/releases/latest")
#: The asset, and who must have uploaded it: GitHub's own build.
ASSET = "Redcoatz.exe"
BUILDER = "github-actions[bot]"
#: Where a failed update sends the player instead.
SITE = "https://redcoatz.com"


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


def release_meta(rel):
    """{'version', 'bytes', 'sha256', 'url', 'page'} from a GitHub release
    document. Raises with a reason the player can read when the release does
    not carry an exe GitHub built and checksummed."""
    tag = str(rel.get("tag_name") or "")
    version = tag[1:] if tag[:1] in ("v", "V") else tag
    if not version:
        raise RuntimeError("the latest GitHub release has no version tag")
    if rel.get("draft") or rel.get("prerelease"):
        raise RuntimeError(f"v{version} on GitHub is not a full release")
    assets = [a for a in rel.get("assets") or [] if a.get("name") == ASSET]
    if len(assets) != 1:
        raise RuntimeError(f"the v{version} release on GitHub has no {ASSET} yet. "
                           "GitHub builds it a few minutes after the release is "
                           "published, so try again shortly")
    a = assets[0]
    uploader = (a.get("uploader") or {}).get("login")
    if uploader != BUILDER:
        raise RuntimeError(f"{ASSET} on the v{version} release was not built by GitHub "
                           f"(it was attached by {uploader or 'an unknown account'}), "
                           "so it was not installed")
    digest = str(a.get("digest") or "")
    if not digest.startswith("sha256:") or len(digest) != 7 + 64:
        raise RuntimeError(f"GitHub publishes no checksum for {ASSET} on v{version}")
    url = str(a.get("browser_download_url") or "")
    if (not url.startswith(f"https://github.com/{REPO}/releases/download/")
            and not os.environ.get("REDCOATZ_UPDATE_API")):
        raise RuntimeError("the download address is not on the Redcoatz repository")
    return {"version": version, "bytes": int(a.get("size") or 0),
            "sha256": digest[7:].lower(), "url": url,
            "page": str(rel.get("html_url") or f"{REPO_URL}/releases/tag/{tag}")}


def fetch_meta():
    """The latest release on GitHub, as release_meta reads it."""
    req = urllib.request.Request(API_URL, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/vnd.github+json",
                                                   "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return release_meta(json.loads(r.read().decode("utf-8")))


def is_newer(meta):
    """True when the release is newer than this build."""
    def parse(v):
        out = []
        for part in str(v).split("."):
            digits = "".join(c for c in part if c.isdigit())
            out.append(int(digits) if digits else 0)
        return tuple((out + [0, 0, 0])[:3])
    return parse(meta["version"]) > parse(VERSION)


def confirm_text(meta):
    """What the player is asked before anything is downloaded."""
    return ("This update will download Redcoatz v%s from\n%s\n"
            "and install it on your computer, replacing this copy.\n\n"
            "GitHub built that file from the public source code you can read "
            "there, and Redcoatz checks it against GitHub's checksum before "
            "running it.\n\nAre you sure you want to proceed?"
            % (meta["version"], REPO_URL))


def download(meta, progress=None):
    """Download the new exe to "<exe>.new" and verify it. Returns the path of the checked file.

    The SHA-256 is computed as the bytes arrive and compared with GitHub's,
    and the size with GitHub's. On any mismatch
    or failure the partial file is removed and an exception is raised; the
    file is never left in place unverified. progress(done_bytes,
    total_bytes) is called as it arrives.
    """
    _exe, new, _old = _paths()
    want_size = int(meta.get("bytes") or 0)
    want_hash = (meta.get("sha256") or "").lower()
    if not want_hash:
        raise RuntimeError("no checksum for this version")
    h = hashlib.sha256()
    done = 0
    try:
        with _get(meta["url"], timeout=60) as r, open(new, "wb") as f:
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
            raise RuntimeError("the download does not match GitHub's checksum")
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
    """Ask, download, verify and swap, with a small window over `parent`.

    Looks up the latest GitHub release first and asks the player, naming the
    repository and the version, before anything is downloaded; "No" closes
    the window and nothing changes. on_restart() is called once the new exe
    has been started; it must make this process exit. On any failure the
    window says what went wrong and offers the website instead.
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
    msg = tk.Label(win, text="Checking GitHub for the latest release...", bg=bg,
                   fg=fg, font=("Georgia", 10), justify="center", wraplength=380)
    msg.pack(padx=30)
    row = tk.Frame(win, bg=bg)
    row.pack(pady=(14, 18))

    state = {"text": None, "done": False, "error": None, "meta": None,
             "asked": False, "go": threading.Event(), "cancel": False}

    def work():
        try:
            meta = fetch_meta()
            ver = meta.get("version", "?")
            if not is_newer(meta):
                state["current"] = (f"This is v{VERSION}, the newest release on GitHub. "
                                    "If the ladder says a newer version is out, GitHub is "
                                    "still building it; try again in a few minutes.")
                return
            # Nothing is downloaded until the player has said yes.
            state["meta"] = meta
            state["go"].wait()
            if state["cancel"]:
                return
            log(f"update: the player agreed to v{ver} from {meta['url']}")
            state["text"] = f"Downloading v{ver} from GitHub..."

            def progress(done, total):
                if total:
                    state["text"] = (f"Downloading v{ver} from GitHub... "
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

    def proceed():
        for w in row.winfo_children():
            w.destroy()
        head.config(text="Updating Redcoatz")
        msg.config(text="Starting the download...")
        state["go"].set()

    def cancel():
        state["cancel"] = True
        state["go"].set()
        log("update: the player declined")
        win.destroy()

    def view():
        try:
            webbrowser.open_new_tab(state["meta"]["page"])
        except Exception:                  # noqa: BLE001
            pass

    def poll():
        if not win.winfo_exists():
            state["cancel"] = True
            state["go"].set()
            return
        if state["meta"] and not state["asked"]:
            state["asked"] = True
            head.config(text="Install this update?")
            msg.config(text=confirm_text(state["meta"]), justify="left")
            tk.Button(row, text="Yes, install it", command=proceed, bg="#AE3238",
                      fg="#FFFBF4", relief="flat",
                      font=("Georgia", 10, "bold")).pack(side="left", padx=6)
            tk.Button(row, text="View on GitHub", command=view, bg="#171C29",
                      fg="#CBC4B2", relief="flat",
                      font=("Georgia", 10)).pack(side="left", padx=6)
            tk.Button(row, text="No", command=cancel, bg="#171C29",
                      fg="#CBC4B2", relief="flat",
                      font=("Georgia", 10)).pack(side="left", padx=6)
        if state["text"]:
            msg.config(text=state["text"])
        if state["done"]:
            msg.config(text="Restarting with the new version...")
            win.after(300, on_restart)
            return
        if state.get("current"):
            head.config(text="You have the newest version")
            msg.config(text=state["current"], fg=dim)
            tk.Button(row, text="Close", command=win.destroy, bg="#171C29",
                      fg="#CBC4B2", relief="flat",
                      font=("Georgia", 10)).pack(side="left", padx=6)
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
