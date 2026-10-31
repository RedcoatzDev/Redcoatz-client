"""Refuse to run a client the server has retired, and check the ban list.

A bug in the client is not confined to the player running it: it uploads
results that become other people's ratings, so a version that gets that wrong
corrupts the ladder for everybody until that player updates. Stopping the
client is the only way to reach them.

Both checks fail open. Only an explicit answer from the server may block the
client; a timeout, DNS failure or outage lets it through, so a server problem
never becomes an outage for people trying to play.

Network: GET <server>/v1/client (minimum and latest versions, download URL)
and GET <server>/v1/banned?name=...&steam_id=... Both carry the client
version as User-Agent and nothing else. block_gui can open the download page
in the default browser when the player clicks.
"""

import json
import urllib.request

from version import VERSION, USER_AGENT


def parse(v):
    """'0.10.2' -> (0, 10, 2), so versions compare numerically rather than as strings."""
    out = []
    for part in str(v).split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def check(server, timeout=10):
    """GET <server>/v1/client and compare. Returns (allowed, info).

    info carries 'minimum', 'latest' and 'download' when the server
    answered, and is empty when it did not (in which case allowed is True).
    """
    url = f"{server.rstrip('/')}/v1/client"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            info = json.loads(r.read().decode("utf-8"))
    except Exception:
        return True, {}                 # could not ask; fail open

    minimum = info.get("minimum")
    if not minimum:
        return True, info
    return parse(VERSION) >= parse(minimum), info


def outdated_but_usable(info):
    """True when a newer version exists that the player is not required to take."""
    latest = info.get("latest")
    return bool(latest) and parse(VERSION) < parse(latest)


def block_gui(info, parent=None):
    """Tell the player they must update and offer the download. Blocks until dismissed.

    "Download the update" opens the server's download URL in the default
    browser. "Update now" appears only in the built exe and runs
    selfupdate.run_gui.
    """
    import tkinter as tk
    import webbrowser

    download = info.get("download") or "https://redcoatz.com"
    minimum = info.get("minimum", "?")
    latest = info.get("latest", minimum)

    owns_root = parent is None
    win = tk.Tk() if owns_root else tk.Toplevel(parent)
    win.title("NTW3 Ladder - update required")
    win.configure(bg="#10131C")
    win.minsize(520, 260)

    # The site, without the scheme: it reads as a place to go, and the button
    # next to it is what actually opens it.
    site = (download.split("//", 1)[-1].split("/", 1)[0]) or "redcoatz.com"

    tk.Label(win, text="Your client is out of date", bg="#10131C",
             fg="#E4C67E", font=("Georgia", 16, "bold")).pack(pady=(26, 14))
    tk.Label(win,
             text=(f"Your client version {VERSION} is out of date.\n"
                   f"Please download the latest version {latest} at {site}"),
             bg="#10131C", fg="#ECE6D7", font=("Georgia", 11),
             justify="center").pack(padx=30)
    tk.Label(win,
             text=("Battles cannot be reported until you update.\n"
                   "This keeps a fixed bug from affecting other players' "
                   "ratings."),
             bg="#10131C", fg="#8A93A6", font=("Georgia", 9),
             justify="center").pack(pady=(14, 0), padx=30)

    row = tk.Frame(win, bg="#10131C")
    row.pack(pady=22)

    def get_it():
        try:
            webbrowser.open_new_tab(download)
        except Exception:
            pass
        win.destroy()

    # One click, where the built exe can replace itself (selfupdate.py). The
    # website button stays beside it as the way that always works.
    try:
        import selfupdate
        can = selfupdate.can_self_update()
    except Exception:                      # noqa: BLE001
        can = False
    if can:
        import os

        def update_now():
            selfupdate.run_gui(win, on_restart=lambda: os._exit(0),
                               fallback=download)

        tk.Button(row, text="Update now", command=update_now, width=16,
                  bg="#AE3238", fg="#FFFBF4", relief="flat",
                  activebackground="#C9484A", font=("Georgia", 10, "bold")
                  ).pack(side="left", padx=8)

    tk.Button(row, text="Download the update", command=get_it, width=22,
              bg="#AE3238", fg="#FFFBF4", relief="flat",
              activebackground="#C9484A", font=("Georgia", 10, "bold")
              ).pack(side="left", padx=8)
    tk.Button(row, text="Quit", command=win.destroy, width=12,
              bg="#171C29", fg="#CBC4B2", relief="flat",
              font=("Georgia", 10)).pack(side="left", padx=8)

    win.protocol("WM_DELETE_WINDOW", win.destroy)
    if owns_root:
        win.mainloop()
    else:
        win.transient(parent)
        win.grab_set()
        parent.wait_window(win)
    try:
        win.destroy()
    except Exception:
        pass


def check_banned(server, name, steam_id, timeout=10):
    """GET <server>/v1/banned?name=...&steam_id=... Returns (banned, info).

    Fails open like the version check: only an answer from the server may
    stop the client.
    """
    import urllib.parse
    q = urllib.parse.urlencode({"name": name or "",
                                "steam_id": str(steam_id or "")})
    url = f"{server.rstrip('/')}/v1/banned?{q}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            info = json.loads(r.read().decode("utf-8"))
    except Exception:
        return False, {}
    return bool(info.get("banned")), info


def banned_gui(info, parent=None):
    """Tell a banned player why Redcoatz will not run, listing the battles. Blocks until dismissed."""
    import tkinter as tk

    owns_root = parent is None
    win = tk.Tk() if owns_root else tk.Toplevel(parent)
    win.title("Redcoatz")
    win.configure(bg="#10131C")
    win.minsize(520, 240)
    tk.Label(win, text="Banned from Redcoatz", bg="#10131C", fg="#E4C67E",
             font=("Georgia", 16, "bold")).pack(pady=(26, 14))
    tk.Label(win,
             text=("This player is banned from Redcoatz for cheating. They "
                   "brought more than the 10,000 allowable gold."),
             bg="#10131C", fg="#ECE6D7", font=("Georgia", 11),
             wraplength=460, justify="center").pack(padx=30)
    lines = []
    for b in (info.get("battles") or [])[:8]:
        when = str(b.get("at", ""))[:10]
        where = b.get("map_name") or b.get("map") or "unnamed map"
        lines.append(f"{where}, {when}: {int(b.get('gold', 0)):,} gold")
    if lines:
        tk.Label(win, text="\n".join(lines), bg="#10131C", fg="#8A93A6",
                 font=("Georgia", 9), justify="center").pack(pady=(14, 0))
    tk.Button(win, text="Close", command=win.destroy, width=12,
              bg="#171C29", fg="#CBC4B2", relief="flat",
              font=("Georgia", 10)).pack(pady=22)
    win.protocol("WM_DELETE_WINDOW", win.destroy)
    if owns_root:
        win.mainloop()
    else:
        win.transient(parent)
        win.grab_set()
        parent.wait_window(win)
    try:
        win.destroy()
    except Exception:
        pass
