"""NTW3 Ladder overlay: an always-on-top ranking panel that sits beside the game.

Run it, then start NTW3. The panel stays above the game window and shows the
standings of whoever is in the lobby.

    python ladder_overlay.py                     live, polls the server
    python ladder_overlay.py --demo              fake data, no server needed
    python ladder_overlay.py --server http://host:8080

It is a separate borderless tkinter window rather than an in-game overlay:
NTW3 runs windowed, so a topmost window draws over it without hooking the
renderer. Everything is the Python standard library; nothing is pip-installed.

What this module touches outside its own window:

  Game memory   The lobby roster, map and battle state are READ from the
                running Napoleon.exe through lobby_memory, live_seats and
                results (Poller, MapWatcher). Nothing here is done to any
                other process. The one WRITE to the game is
                artilleryfix.apply, called once per game process from
                Poller.run while the game is on a menu or in a lobby: it
                patches a known crash in the running game's code. The exe
                on disk is never changed.
  Files         %APPDATA%\\ntw3ladder: overlay.json (panel position, read and
                written), postbattle.json (read), maps\\ (downloaded map
                pictures), results\\ (written only with
                --save-battle-results). The game's replay folder is listed,
                and its .replay files are uploaded by the uploader module.
  Network       Only the ladder server given by --server, NTW3LADDER_SERVER
                or the built-in default: GET rankings and map pictures, POST
                battle start, progress and end reports (map, host, player
                names and Steam IDs, the result as read off the screen) and a
                game-closed notice. Steam sign-in goes through the player's
                browser (steamauth). Window titles are read to find the game
                window and are not sent anywhere.
"""

import argparse
import ctypes
import ctypes.wintypes as wintypes
import json
import math
import re
import os
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Imported before anything opens a connection: switches TLS verification to
# the OS certificate store, so the panel reaches the server on a machine
# whose antivirus intercepts HTTPS.
import nettrust  # noqa: E402,F401
import consent  # noqa: E402
import firstsync  # noqa: E402
from version import VERSION, USER_AGENT  # noqa: E402
import steamauth  # noqa: E402
import mapnames  # noqa: E402
# The function, not the module: Poller.run rebinds `roster` to a local list,
# which would shadow the module.
from roster import seat_faults  # noqa: E402

try:
    from lobby_memory import (read_roster_state, save_button_showing,
                              scan_battle_signals, count_lobby_widget,
                              in_lobby, in_battle, map_from_status,
                              status_text,
                              read_map_widget, forget_map_widget,
                              find_roster_array,
                              status_text, STATUS_HOST_LEFT, STATUS_DROPPED,
                              STATUS_STEAM_LOST, STATUS_DESYNC,
                              read_results_screen, RESULT_WORDS, SAVE_TEXT,
                              is_verdict,
                              RESULTS_TEXT,
                              pick_lobby_map, read_map, results_regions,
                              region_set,
                              find_game, info_map,
                              LOBBY_WIDGET_MIN,
                              STATE_LOBBY, STATE_BATTLE, STATE_UNKNOWN,
                              SAVE_NEW_MIN, HOST_LEFT_NEW, DROPPED_NEW,
                              count_verdicts, verdicts_risen)
except Exception:                      # pragma: no cover - platform fallback
    # No-op stand-ins for every memory reader, so the panel still runs (with
    # no lobby and no reports) where the game's memory cannot be read.
    STATE_LOBBY, STATE_BATTLE, STATE_UNKNOWN = "lobby", "battle", "unknown"
    LOBBY_WIDGET_MIN = 10
    # Mirrored from lobby_memory so the results watcher does not raise
    # NameError on a platform where nothing can be read anyway.
    SAVE_NEW_MIN, HOST_LEFT_NEW, DROPPED_NEW = 3, 3, 2
    def count_verdicts(pid, max_region=0):
        return None

    def verdicts_risen(base, now):
        return {}
    # Same for the status reader: None means "fall back to scanning".
    STATUS_HOST_LEFT = "The host has left the game"
    STATUS_DROPPED = "You dropped from the game"
    STATUS_STEAM_LOST = "Connection to Steam was lost"
    STATUS_DESYNC = "A synchronisation error has occurred"
    RESULT_WORDS = ("Victory", "Defeat", "Draw")

    def is_verdict(t):
        w = (t or "").split()
        return (1 <= len(w) <= 3 and w[-1] in RESULT_WORDS
                and all(x[:1].isupper() for x in w))
    SAVE_TEXT = "Save replay"
    RESULTS_TEXT = "Battle Results"

    def status_text(pid):
        return None

    def read_results_screen(pid, only=None):
        return None, None, []

    def read_roster_state():
        # Seven values, matching the real one; the call site unpacks them.
        return [], STATE_UNKNOWN, 0, set(), None, {}, {}

    def read_map(pid=None, hints=None):
        return None, {}

    def results_regions():
        return set()

    def region_set(pid=None):
        return set()

    def find_game():
        return None, ""

    def info_map():
        return None

    def pick_lobby_map(hosts, sids):
        return None

    def save_button_showing(saves, baseline):
        return False

    def scan_battle_signals(pid, seen, tally=None, fresh=None):
        return ({"save": 0, "host_left": 0, "dropped": 0}, seen,
                dict(tally or {}), dict(fresh or {}))

    def in_lobby(pid, base=None):
        return None

    def in_battle(pid, base=None):
        return None

    def map_from_status(text):
        return ""

    def read_map_widget(pid=None, base=None):
        return ""

    def forget_map_widget(pid=None):
        return None

    def find_roster_array(pid=None, hints=None):
        return 0, set()

    def count_lobby_widget(pid, bases, seen):
        return 0, seen

# Seconds the panel shows "GAME STARTED" before collapsing. Not zero: a panel
# that vanishes the instant a battle loads looks like a crash.
BATTLE_HIDE_DELAY = 3

# The game keeps the lobby roster in a plain array of name pointers; live_seats
# reads that array directly, which beats inferring the roster from a scan.
try:
    import live_seats
except Exception:                      # pragma: no cover - platform fallback
    live_seats = None

try:
    from uploader import detect_player
except Exception:                      # pragma: no cover
    def detect_player(explicit=None):
        return None, None

# Colours sampled from the game's own lobby panels, so this reads as part of
# the game rather than a foreign app on top of it.
BODY = "#4A1410"     # panel interior, the game's Alliance boxes
HEADER = "#6B2318"   # the band behind "Alliance 1" / "Alliance 2"
# The game's seat lists alternate two tones behind their rows.
ROW_A = "#57210F"    # lighter band
ROW_B = "#43160E"    # darker band
GOLD = "#C9A961"     # the frame around every lobby panel
TITLE = "#FFFBF4"    # heading text, near-white
EDGE = "#2A0C08"     # outer line, a shade under the body
TEXT = "#E8DFC8"     # player names: off-white, not pure white
DIM = "#A89070"      # column headings and secondary text
WIN = "#93BF86"      # positive
LOSS = "#C08880"     # negative
COAT = "#9C3A2E"     # the Redcoatz badge beside a name: a madder-red coat
COAT_LINE = "#C08880"  # the unfixed coat's outline
COAT_BUTTON = "#A89070"  # the fixed coat's edge and buttons
# The host lamp beside the host's name: how their lobbies tend to end.
# Prototype: the server does not send it yet; the demo roster carries it.
HOST_GOOD = "#5DBA5A"    # green: battles reach the end
HOST_BAD = "#D2402E"     # red: lobbies that fall apart, host has left battles
HOST_WORST = "#141010"   # black: a pattern of leaving their own battles
HOST_RING = "#2A0C08"    # the lamp's rim, the panel's own edge colour
# How the FIN column shows a player's finished share. "pct" is what ships;
# the others are prototypes drawn by _draw_fin.
#   pct    "95%", green from 90%, red below, once there are 5 games
#   dot    one lamp: green / amber / red, hollow until 5 games
#   bars   four bars, lit by share, like signal strength
#   meter  a short bar filled to the share
#   pips   the last five battles, a filled pip for each one finished
FIN_STYLE = "pct"
FIN_AMBER = "#D9A648"
SKULL = "#CFC6B4"     # the over-budget skull: bone, muted like the coat
SKULL_HOLE = "#1E1A16"  # its eyes and nose
LINK = "#E3C77E"     # clickable text: a brighter gold than the frame

# The community site, offered when the game is not running.
SITE_URL = "https://redcoatz.com"
SITE_LABEL = "redcoatz.com"

# The live ladder, and the developer default. NTW3LADDER_SERVER overrides.
PRODUCTION_SERVER = "https://redcoatz.com"
LOCAL_SERVER = "http://localhost:8080"


def _default_server():
    """The server to talk to unless --server or NTW3LADDER_SERVER says otherwise.

    A packaged exe (sys.frozen, set by PyInstaller) is a player's copy and
    reaches the real ladder. A source run is a developer's and defaults to
    localhost, because this client POSTs reports and a debugging session aimed
    at production would write test data into real standings.
    """
    override = os.environ.get("NTW3LADDER_SERVER")
    if override:
        return override
    return PRODUCTION_SERVER if getattr(sys, "frozen", False) else LOCAL_SERVER


DEFAULT_SERVER = _default_server()

#: Marks the title-bar button drawn as a figure rather than a font glyph.
USER = "user"

#: Whether to print the battle-scan trace. Same test as _default_server: a
#: source run is a developer's. A packaged exe prints none of it.
DEV_TRACE = not getattr(sys, "frozen", False)

#: A battle-scan pass slower than this gets its own trace line, so a late
#: detection can be told from a slow loop.
SLOW_PASS_SECONDS = 1.5

# The game sets its UI in a wide old-style serif. These are the closest faces
# on a stock Windows install, in order of preference.
TITLE_FONTS = ("Book Antiqua", "Palatino Linotype", "Georgia",
               "Constantia", "serif")
# Opaque: over the game's dark backdrop any transparency only made the panel
# darker.
DEFAULT_OPACITY = 1.0

# Eight rows is a full 4v4, the largest lobby NTW3 allows.
SEATS = 8

# Sized for nine rows: the eight seats plus one reserved for the map line, so
# a full lobby never hides the map.
BASE_W, BASE_H = 371, 303

# Collapsed, the panel is a small square badge: it only needs to say "still
# running, still watching".
BASE_TAB_W, BASE_TAB_H = 38, 38
# Seconds between lobby passes. The lobby changes on a human timescale, so a
# slower poll showed a visibly wrong roster. The full memory scan competes for
# memory bandwidth, which is why it only runs in a lobby, where frame time
# does not matter; a battle is watched by RESULTS_POLL_SECONDS instead.
POLL_SECONDS = 5

# Seconds between checks during a battle. There is no full scan in a battle:
# the two questions (has the Save Replay button appeared, is the lobby back)
# are answered from a few megabytes of fresh or previously located regions.
# Half a second, so the results screen is caught while it is up.
RESULTS_POLL_SECONDS = 0.5

# How many structural (full-scan) reads of the end-of-battle screen one battle
# may spend, and the least time between two. These run only against a server
# on this machine (see _maybe_read_outcome). Each is a full scan of the game's
# memory and the trigger stays true for as long as the screen is up, so they
# are bounded and spaced; four passes ten seconds apart span the handover from
# the Save Replay offer to the Battle Results table.
OUTCOME_TRIES = 4
OUTCOME_RETRY_SECONDS = 10.0

# How often the status slot is sampled. The slot is one pointer holding
# whatever string the UI drew last, so it is cheap to read and easy to miss: a
# verdict drawn and painted over inside one poll is only seen if the slot is
# sampled in the gaps. It is also the only source that survives the results
# widgets being freed when a player clicks through quickly.
STATUS_SAMPLE_SECONDS = 0.05

# How often the lobby's map is looked up. A map changes when somebody joins a
# different lobby, so once a second is plenty; sampling faster forced the
# reading through a cache that kept naming the lobby just left.
MAP_SAMPLE_SECONDS = 1.0

# Passes in a row that must fail to see the lobby before the map is given up.
# The widget table's membership changes frame to frame, so a single miss is
# not evidence the player left.
LOBBY_GONE_CONFIRM = 2

# Distinct strings kept from the status slot, printed only when a battle ends
# without a verdict: the record of what the game was drawing instead.
STATUS_KEEP = 40

# Seconds after a battle ends before another start can be reported. As a
# battle ends the lobby is torn down and rebuilt, and a scan that catches it
# half-built reads as a battle. Nobody can re-ready and start again inside
# this window, and a late start report is still the same start.
START_SETTLE = 45.0

#: Scans in a row that must say "battle" before it is one. BATTLE is the
#: answer a scan falls back to when it found nothing, so one thin scan is a
#: guess and two running is the lobby being gone.
BATTLE_CONFIRM = 2

# Extra readings of the lobby marker before a pass decides the lobby is gone.
# It misses about one reading in eight.
LOBBY_RECHECKS = 3
LOBBY_RECHECK_SECONDS = 0.15

# Seconds between "battle still running" reports. Five minutes locates a
# client dying mid-battle closely enough and keeps eight players under a
# hundred requests an hour.
HEARTBEAT_SECONDS = 300

# How often, while waiting out POLL_SECONDS, to re-read just the regions that
# held lobby records last time. A few milliseconds against seconds for a full
# pass, so a map change shows in about a second.
MAP_RECHECK_SECONDS = 0.5

# AI seats are always "CPU <n>"; every other seat is a human with a Steam name.
AI_NAME = re.compile(r"^CPU \d+$", re.I)
GAME_CHECK_SECONDS = 3

# Consecutive misses before the game counts as gone. It drops its windows
# briefly between the menu and a battle load.
GAME_GONE_CHECKS = 2
# Retries of the game-closed report before quitting anyway, about half a
# minute at GAME_CHECK_SECONDS apart. An unreachable server must not strand an
# invisible process scanning a game that is no longer running.
CLOSE_REPORT_TRIES = 10
# Panel and map window positions. Read at startup, written when dragged.
STATE_FILE = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "ntw3ladder", "overlay.json"
)

# Where the script installed into the game writes the results table from the
# post-battle screen. Read here and attached to end-of-battle reports. See
# client/gamefiles and postbattle_hook.
POSTBATTLE_FILE = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "ntw3ladder",
    "postbattle.json")

# End-of-battle tables, one file per battle, written only with
# --save-battle-results and sent nowhere. The reading is half-finished, so
# these are research material rather than a feature.
RESULTS_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "ntw3ladder", "results"
)

# Downloaded battlefield pictures, kept between runs.
MAP_CACHE_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "ntw3ladder", "maps"
)

# Map window size in pixels: large enough to read a deployment line, small
# enough to sit over a lobby without burying it.
MAP_WINDOW_PX = 600

# The drawn frame around it: a title bar in the game's palette and a margin
# inside the gold edge.
MAP_WINDOW_HEADER = 26
MAP_WINDOW_PAD = 8

# How often the map window checks whether its download has finished.
MAP_POLL_MS = 120

# How often the map window re-asserts itself over other topmost windows.
# Windows has no band above topmost; the most recently raised peer wins, and a
# chat box that redraws on each message takes the top back.
MAP_TOP_MS = 400

# How often the panel checks whether a topmost window has been drawn over it.
# Checking is cheap; raising only when actually covered avoids a raise war
# with other topmost windows. See _covered.
PANEL_CHECK_MS = 120

# Readings in a row that must show the same broken lobby before it is named.
# A gap in an alliance's seats is also what a thin memory scan looks like; a
# real fault does not heal between polls and a bad scan does.
FAULT_READINGS = 3


def _win32_raise(win):
    """Put one of our own windows at the top of the topmost band without taking focus.

    SetWindowPos with SWP_NOACTIVATE, because this runs on a timer and stealing
    focus would make the game unplayable. The argtypes matter: without them
    ctypes passes HWND_TOPMOST (-1) as a 32-bit int, the call silently fails
    and the window stays wherever it was.
    """
    if sys.platform != "win32":
        return
    try:
        u32 = ctypes.windll.user32
        u32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_uint]
        u32.SetWindowPos.restype = ctypes.c_bool
        hwnd = u32.GetParent(win.winfo_id()) or win.winfo_id()
        u32.SetWindowPos(
            ctypes.c_void_p(hwnd),
            ctypes.c_void_p(-1),        # HWND_TOPMOST
            0, 0, 0, 0,
            0x0001 | 0x0002 | 0x0010)   # NOSIZE | NOMOVE | NOACTIVATE
    except Exception:                   # noqa: BLE001 - cosmetic, never fatal
        pass


def _covered(win):
    """True if a visible always-on-top window overlapping win sits above it.

    Walks the window z-order from the top down to win, checking only
    visibility, the topmost style and the window rectangle. Overlap is what
    keeps this from starting a raise war with a topmost window elsewhere on
    the screen, such as a chat box.
    """
    if sys.platform != "win32":
        return False
    try:
        u32 = ctypes.windll.user32
        hwnd = u32.GetParent(win.winfo_id()) or win.winfo_id()
        mine = wintypes.RECT()
        if not u32.GetWindowRect(hwnd, ctypes.byref(mine)):
            return False
        other, hit = wintypes.RECT(), wintypes.RECT()
        cur = u32.GetTopWindow(0)
        while cur and cur != hwnd:
            if (u32.IsWindowVisible(cur)
                    and u32.GetWindowLongW(cur, -20) & 0x8      # WS_EX_TOPMOST
                    and u32.GetWindowRect(cur, ctypes.byref(other))
                    and u32.IntersectRect(ctypes.byref(hit),
                                          ctypes.byref(mine),
                                          ctypes.byref(other))):
                return True
            cur = u32.GetWindow(cur, 2)                         # GW_HWNDNEXT
    except Exception:                   # noqa: BLE001 - cosmetic, never fatal
        return False
    return False


def _no_activate(win):
    """Set WS_EX_NOACTIVATE on one of our own windows.

    Clicks still reach buttons and the drag handle, but the window never
    becomes the active one, so clicking it cannot pull a fullscreen game out
    of fullscreen. Keyboard focus goes with it, which neither window needs.
    """
    if sys.platform != "win32":
        return
    try:
        u32 = ctypes.windll.user32
        # Declared so a handle above 2 GB is not truncated to a C int.
        u32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u32.GetWindowLongW.restype = ctypes.c_long
        u32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                       ctypes.c_long]
        u32.SetWindowLongW.restype = ctypes.c_long
        hwnd = u32.GetParent(win.winfo_id()) or win.winfo_id()
        GWL_EXSTYLE, WS_EX_NOACTIVATE = -20, 0x08000000
        ex = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        u32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE)
    except Exception:                   # noqa: BLE001 - cosmetic, never fatal
        pass


# How long a cached battlefield picture is trusted, in seconds. Matches the
# server's Cache-Control on the same resource.
MAP_CACHE_MAX_AGE = 24 * 60 * 60


def map_label(key):
    """A one-line label for a map key.

    A name the lobby itself displays is left alone. A preset key such as
    ntw3_1805_austerlitz is turned into "Austerlitz (1805)" crudely on
    purpose: a copy of the server's naming table would drift.
    """
    if not key:
        return ""
    if not key.startswith("ntw3_"):
        return key
    slug = key.replace("ntw3_", "", 1).replace("hb_", "", 1)
    year = ""
    parts = slug.split("_")
    if parts and parts[0].isdigit() and len(parts[0]) == 4:
        year, parts = parts[0], parts[1:]
    name = " ".join(p.capitalize() for p in parts) or slug
    return f"{name} ({year})" if year else name

# Window-title words that mean "a battle is running, get out of the way". A
# fallback for when the game's memory cannot be read; if they are wrong the
# automatic hide simply never fires, and the panel can be collapsed by hand.
BATTLE_TITLE_HINTS = ("battle", "deployment")
LOBBY_TITLE_HINTS = ("lobby", "multiplayer")


def game_windows(pid=None):
    """Titles of visible top-level windows that look like Napoleon's.

    Enumerates every visible window and reads its title text (EnumWindows,
    GetWindowTextW); nothing else about any window is read, and the titles
    stay on this machine. With a pid, only windows owned by that process
    count. Without one the match is on the title alone, which is loose enough
    that a browser tab about NTW3 reads as the game.
    """
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        found = []

        def cb(hwnd, _):
            """EnumWindows callback: keep the title if visible, owned by pid, and game-like."""
            if not user32.IsWindowVisible(hwnd):
                return True
            if pid is not None:
                owner = wt.DWORD(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                if owner.value != pid:
                    return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                t = buf.value.strip()
                if t and any(k in t.lower() for k in ("napoleon", "ntw", "total war")):
                    found.append(t)
            return True

        user32.EnumWindows(proc(cb), 0)
        return found
    except Exception:
        return []


def _link(c, x, y, text, font, s, action, colour=None):
    """Underlined clickable text, centred on (x, y)."""
    colour = colour or LINK
    item = c.create_text(x, y, text=text, fill=colour, font=font)
    bb = c.bbox(item)
    if bb:
        c.create_line(bb[0], bb[3] - int(2 * s), bb[2], bb[3] - int(2 * s),
                      fill=colour)

    def click(_e):
        action()
        return "break"

    c.tag_bind(item, "<Button-1>", click)
    c.tag_bind(item, "<Enter>", lambda e: c.config(cursor="hand2"))
    c.tag_bind(item, "<Leave>", lambda e: c.config(cursor=""))
    return item


def _draw_signin_body(c, w, h, hdr, s, font, waiting, start=None,
                      reopen=None, cancel=None, quit_=None):
    """The signed-out body, shared by the panel and the start-up screen.

    Waiting: the browser is on Steam's page, so say so and offer the page
    again and a way out. Otherwise: a link to begin, and on the start-up
    screen a way to quit.
    """
    mid = w // 2
    y = hdr + (h - hdr) // 2 - int(22 * s)
    if waiting:
        c.create_text(mid, y, text="SIGN IN THROUGH STEAM", fill=TITLE,
                      font=font)
        c.create_text(mid, y + int(20 * s),
                      text="Finish signing in using your browser.",
                      fill=DIM, font=font)
        _link(c, mid, y + int(46 * s), "Browser did not open? Open it again",
              font, s, reopen, colour=DIM)
        _link(c, mid, y + int(70 * s), "Cancel", font, s, cancel)
    else:
        c.create_text(mid, y, text="SIGNED OUT", fill=LOSS, font=font)
        c.create_text(mid, y + int(20 * s),
                      text="Battles are not counted until you sign in.",
                      fill=DIM, font=font)
        _link(c, mid, y + int(46 * s), "Sign in through Steam", font, s, start)
        if quit_ is not None:
            _link(c, mid, y + int(70 * s), "Quit", font, s, quit_, colour=DIM)


def sign_in_screen(root, server, steam_id=None, log=print, scale=None):
    """The start-up Steam sign-in, drawn in the panel's own window.

    Returns the Steam ID the key proves, or None if the player quit. Shows
    nothing when a good key is already on file. The sign-in itself is
    steamauth.sign_in: it opens the player's browser on Steam's page and
    waits for the server to hand back a key. Cancel or a timeout returns to
    "signed out" with a link to try again; only Quit gives up. The window is
    withdrawn and emptied on return, ready for Overlay.
    """
    have = steamauth.key_for(steam_id, log=log)
    if have:
        return have
    if scale is None:
        try:
            scale = root.winfo_fpixels("1i") / 96.0
        except tk.TclError:
            scale = 1.0
    s = max(0.8, min(scale, 3.0))
    w, h = int(BASE_W * s), int(BASE_H * s)
    hdr = int(30 * s)
    x, y = _saved_position(root, w)

    have_fonts = {f.lower() for f in tkfont.families(root)}
    face = next((f for f in TITLE_FONTS if f.lower() in have_fonts),
                TITLE_FONTS[-1])
    px = max(9, int(11 * s))
    f_title = tkfont.Font(family=face, size=int((px + 2) * 1.2))
    f_small = tkfont.Font(family=face, size=max(9, px + 1))

    root.title("NTW3 Ladder")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=EDGE)
    root.geometry(f"{w}x{h}+{x}+{y}")
    c = tk.Canvas(root, width=w, height=h, highlightthickness=0, bd=0, bg=BODY)
    c.pack(fill="both", expand=True)

    st = {"url": "", "cancel": None, "busy": False, "result": None,
          "quit": False}
    finished = tk.BooleanVar(root, value=False)

    def draw():
        c.delete("all")
        c.create_rectangle(0, 0, w, h, fill=EDGE, outline="")
        c.create_rectangle(2, 2, w - 3, h - 3, fill=GOLD, outline="")
        c.create_rectangle(int(4 * s), int(4 * s), w - int(4 * s) - 1,
                           h - int(4 * s) - 1, fill=BODY, outline="")
        c.create_rectangle(int(4 * s), int(4 * s), w - int(4 * s) - 1, hdr,
                           fill=HEADER, outline="")
        c.create_line(int(4 * s), hdr, w - int(4 * s) - 1, hdr, fill=GOLD)
        c.create_text(w // 2, hdr // 2 + int(2 * s), text="NTW3 Ladder",
                      fill=TITLE, font=f_title)
        c.create_text(int(14 * s), hdr // 2 + int(2 * s), anchor="w",
                      text=f"v{VERSION}", fill=DIM, font=f_small)
        _draw_signin_body(c, w, h, hdr, s, f_small, waiting=st["busy"],
                          start=begin, reopen=reopen, cancel=cancel,
                          quit_=quit_)

    def reopen():
        if st["url"]:
            webbrowser.open(st["url"])

    def cancel():
        if st["cancel"] is not None:
            st["cancel"].set()

    def quit_():
        st["quit"] = True
        cancel()
        finished.set(True)

    def started(url, ev):
        st["url"], st["cancel"] = url, ev

    def begin():
        if st["busy"]:
            return
        st.update(busy=True, url="", cancel=None)
        draw()

        def go():
            try:
                st["result"] = steamauth.sign_in(server, log=log,
                                                 on_start=started)
            except Exception as e:           # noqa: BLE001
                log(f"steam sign-in failed: {e!r}")
            st["busy"] = False
        # Polled rather than called back from the thread: Tk is inside
        # wait_variable, not mainloop, and refuses calls from other threads.
        threading.Thread(target=go, daemon=True).start()
        root.after(200, landed)

    def landed():
        if st["busy"]:
            root.after(200, landed)
        elif st["result"] or st["quit"]:
            finished.set(True)
        else:
            draw()                           # back to "signed out", try again

    # Draggable like the panel; the position carries over to it.
    drag = {"x": 0, "y": 0}

    def press(e):
        drag["x"], drag["y"] = e.x, e.y

    def move(e):
        root.geometry(f"+{root.winfo_x() + e.x - drag['x']}"
                      f"+{root.winfo_y() + e.y - drag['y']}")
    c.bind("<Button-1>", press)
    c.bind("<B1-Motion>", move)
    root.bind("<Escape>", lambda e: quit_())

    draw()
    root.deiconify()
    begin()
    root.wait_variable(finished)

    root.unbind("<Escape>")
    c.destroy()
    root.withdraw()
    return None if st["quit"] else st["result"]


def _saved_position(root, w):
    """The panel's saved position, read from STATE_FILE, or the top right.

    A saved position that is off every screen is refused: monitors change.
    """
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        x, y = int(d["x"]), int(d["y"])
        if -50 <= x <= root.winfo_screenwidth() - 100 and \
           -50 <= y <= root.winfo_screenheight() - 100:
            return x, y
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return root.winfo_screenwidth() - w - 40, 80


def shown_name(name):
    """A name the panel can draw.

    A name with no printable character (some players use Unicode TAG
    characters) would draw as an empty row; the placeholder says a player is
    in that seat. Reports still carry the real string.
    """
    name = name or ""
    if any(ch.isprintable() and not ch.isspace() for ch in name):
        return name
    return "(hidden name)"


def signed_in():
    """Is a Steam key on file (steamauth's local key file)?

    Nothing is scanned, reported or uploaded without one, so a signed-out
    player cannot be playing rated games by accident.
    """
    return bool(steamauth.load().get("key"))


def game_pid():
    """The Napoleon process id (lobby_memory.find_game), or None."""
    try:
        from lobby_memory import find_game
        return find_game()[0] or None
    except Exception:
        return None


def game_is_running():
    """True/False if the Napoleon process is up, None if we cannot tell.

    "Cannot tell" (a failed import or an unsupported platform) is kept apart
    from "not running" so the panel never tells a player their running game
    is not running. Callers decide how to resolve None.
    """
    try:
        from lobby_memory import find_game
        return bool(find_game()[0])
    except Exception:
        return None


class MapWatcher(threading.Thread):
    """Name the lobby's map on its own clock, by reading the game's memory.

    Its own thread, so a newly hosted lobby does not wait for the Poller's
    slow full pass to show its map. Reads only: the seat array (to tell one
    lobby from the next), the lobby widget and the map line the lobby is
    drawing now, through lobby_memory. Region hints are kept between passes
    because they say where to look, not what the answer is.
    """

    def __init__(self, out, interval=MAP_SAMPLE_SECONDS):
        """out is the queue to the panel; interval the seconds between passes."""
        super().__init__(daemon=True)
        self.out = out
        self.interval = interval
        self._stop = threading.Event()
        self._last = None
        self._map = ""
        # Which lobby the reading belongs to (the seat array's address), and
        # the regions it was found in last time.
        self._sig = 0
        self._hints = None
        # Consecutive passes with no lobby widget. See run().
        self._gone = 0

    def stop(self):
        """Ask the thread to finish."""
        self._stop.set()

    def run(self):
        """Once a second: which lobby is this, is it still open, what map is drawn."""
        while not self._stop.is_set():
            try:
                pid = find_game()[0]
                if not pid:
                    self._map, self._last = "", None
                    self._stop.wait(1.0)
                    continue
                # The seat array is the lobby: a different allocation is a
                # different lobby, and a reading must not outlive its lobby.
                # The map line survives the army setup and lobby browser
                # screens (the label is drawn empty), so it cannot be used
                # to tell lobbies apart itself.
                arr, bases = find_roster_array(pid, hints=self._hints)
                if bases:
                    self._hints = bases
                # Zero is "not found this pass", not "a different lobby":
                # the hinted lookup misses occasionally on an untouched lobby.
                if arr and arr != self._sig:
                    self._sig, self._map = arr, ""
                    forget_map_widget(pid)
                # The lobby widget flickers the same way, so two misses in a
                # row before believing the lobby is gone.
                if in_lobby(pid):
                    self._gone = 0
                else:
                    self._gone += 1
                if self._gone >= LOBBY_GONE_CONFIRM:
                    self._map = ""
                    forget_map_widget(pid)
                else:
                    # Swept fresh every pass, never from a cache, so it names
                    # the lobby open now. Several looks per pass while there
                    # is no answer (the line is only in the widget tree when
                    # the lobby's own panel is drawn, and the table turns
                    # over frame to frame), one look once the map is known.
                    named = (read_map_widget(pid, fresh=True,
                                             tries=1 if self._map else None)
                             or map_from_status(status_text(pid) or ""))
                    if named:
                        self._map = named
                key = mapnames.to_key(self._map) if self._map else None
                if key and key != self._last:
                    self._last = key
                    self.out.put(("map", key))
            except Exception:                        # noqa: BLE001
                pass
            self._stop.wait(self.interval)


class Poller(threading.Thread):
    """Reads the lobby out of the game's memory and reports it to the server.

    Off the UI thread, because a memory scan takes seconds. Each pass: work
    out which screen the game is on, read the roster, seats and map (all
    through lobby_memory and live_seats, read only), GET the standings of the
    players present, and POST a report when a battle starts, is still running,
    or ends. During a battle _watch_for_results takes over with cheap reads.

    Network errors go to the UI as a status rather than being raised: stale
    numbers with a warning beat a panel that vanishes.
    """

    def __init__(self, server, out, interval=POLL_SECONDS,
                 save_battle_results=False):
        """server is the ladder base URL; out the queue to the panel."""
        # Off unless --save-battle-results is given; see _maybe_save_results.
        self.save_battle_results = save_battle_results
        # Whether the multiplayer UI has been seen loaded at any point. Until
        # it has, a low widget count means "not playing multiplayer", not "in
        # a battle". See run().
        self._seen_lobby = False
        # Whether a lobby has been read properly since the last battle
        # started. A start is only reported off the back of one.
        self._lobby_confirmed = False
        # When the last battle stopped being watched. None until one has.
        self._battle_ended_at = None
        # Regions already accounted for, so the quick results check reads only
        # what is new. Empty means the next check reads everything once.
        self._known_regions = set()
        # Regions that held the lobby widget when the lobby was last open.
        # Re-read during a battle to notice the lobby coming back.
        self._lobby_bases = set()
        # Where everybody sat on the last reading, so a swap is noticed even
        # when the roster is identical.
        self._last_seats = None
        # The lobby as last read, from which a start report is built once the
        # game is in a battle and the lobby is gone from memory.
        self._lobby_snapshot = None
        # The battle now being fought. An ending has to name the same battle:
        # the server keys a battle on its map and roster.
        self._battle = None
        # Latched, so one battle reports one ending.
        self._end_reported = False
        # Separate from _end_reported: reaching the save screen and how the
        # battle ended are different claims, and a battle can make both.
        self._save_screen_reported = False
        # {text: count} as the end-of-battle screen showed it, and whether
        # the screen's own widget was found. Both per battle.
        self._outcomes = {}
        self._results_widget = False
        # Structural passes spent on this battle's result, and when the last
        # ran. See OUTCOME_TRIES.
        self._outcome_tries = 0
        self._outcome_at = 0.0
        # The verdict as the status slot showed it: quick and unreliable, kept
        # for the battles where the better reading is missed.
        self._status_outcome = None
        # What the status slot has held this battle, distinct and bounded.
        self._status_seen = []
        self._status_seen_last = None
        self._lobby_reported = False
        # When the last "still running" went out. None until a battle starts.
        self._last_beat = None
        self._last_state = STATE_UNKNOWN
        # Consecutive scans reading as a battle; see BATTLE_CONFIRM.
        self._battle_streak = 0
        # Last counts traced, and when. Reset at the start of every battle.
        self._trace_prev = None
        self._trace_at = 0.0
        # A broken lobby, and how many readings have agreed about it.
        self._last_faults = []
        self._fault_runs = 0
        # The map last sent to the panel, so an unchanged one is not resent.
        # Declared here because run() reads it on its first pass.
        self._last_map = None
        # Regions that held the lobby's map line, for the fast recheck.
        self._map_line_bases = set()
        # The local player's name (uploader.detect_player): the seat-array
        # search starts from it.
        self.local = (detect_player() or (None, None))[0]
        super().__init__(daemon=True)
        self.server = server.rstrip("/")
        # "dev" means a server on this machine. The address decides it, so
        # there is no flag to leave switched on by accident.
        self._dev = any(h in self.server
                        for h in ("localhost", "127.0.0.1", "[::1]"))
        self.out = out
        self.interval = interval
        self._stop = threading.Event()

    def stop(self):
        """Ask the thread to finish."""
        self._stop.set()

    def _watch_for_results(self):
        """Sit out a battle with cheap memory reads, until it ends or the game goes.

        Nobody joins a lobby that is not open, so a battle needs no roster. The
        two questions, has the Save Replay button appeared and is the lobby
        back, are answered from a few megabytes: the button lands in freshly
        committed regions, the lobby in the regions that held its widget last
        time. The status slot is sampled throughout for the game's dialogs and
        verdict. Reports go out as each is seen (see _report_end).

        Returns when the lobby is back or the game process is gone.
        """
        # "New" means new since this battle began; one set for the life of the
        # client made the save-screen detector fail on long battles.
        seen = region_set(self._pid()) or self._known_regions
        bases = self._lobby_bases
        # Per-battle counts, so a count is a count of this battle.
        tally = {}
        fresh = {}
        # Every traced elapsed time is measured from here.
        began = time.monotonic()
        # Baseline for the verdict count, taken while the results screen does
        # not exist yet. Developer machines only.
        self._verdict_base = None
        self._baseline_verdicts(self._pid())
        self._trace_prev = None
        self._trace_at = 0.0
        last_pass = began
        while not self._stop.is_set():
            # Sampled through the wait, not once at the end of it.
            self._sample_status(getattr(self, "_last_pid", None),
                                RESULTS_POLL_SECONDS)
            pid = self._pid()
            self._last_pid = pid
            if not pid:
                # The game process went away mid-battle. Reported as what was
                # seen, not a diagnosis: a crash, an alt-F4 and a power cut
                # look the same from here.
                if self._battle and not self._end_reported:
                    self._end_reported = True
                    self._report_end("game_gone")
                break
            # The heartbeat, from inside this loop because this is where the
            # client spends a battle.
            now = time.monotonic()
            if self._battle and (self._last_beat is None or
                                 now - self._last_beat >= HEARTBEAT_SECONDS):
                self._last_beat = now
                self._report_end("in_progress")
            try:
                started = time.monotonic()
                # Whether this pass thinks the battle is finishing. Gates the
                # structural read below.
                ending = False
                # The status slot: one pointer read that says what the UI drew
                # last. None means the pointer led nowhere (an offset that does
                # not match this build), and the scan below is all there is.
                st = status_text(pid)
                if st is not None:
                    self._battle_status(st)
                    if DEV_TRACE and st != getattr(self, "_last_status", None):
                        self._last_status = st
                        print("battle status: +%.1fs %r"
                              % (time.monotonic() - began, st[:60]))
                    # The slot is used for timing, the widgets for truth: the
                    # byte count below is slow to rise, and by then a quick
                    # player has clicked through and the widgets are freed. A
                    # false alarm here costs one wasted pass and reports
                    # nothing.
                    if (SAVE_TEXT in st or RESULTS_TEXT in st
                            or any(w in st for w in RESULT_WORDS)):
                        ending = True

                # Not an else: the status slot has gone null for the rest of
                # a battle before, so the scan always runs as well.
                sig, seen, tally, fresh = scan_battle_signals(
                    pid, seen, tally, fresh)
                if DEV_TRACE:
                    done = time.monotonic()
                    self._trace_scan(sig, tally, began,
                                     took=done - started,
                                     gap=started - last_pass,
                                     fresh=len(fresh))
                    last_pass = done
                if sig["save"] >= SAVE_NEW_MIN:
                    self.out.put(("results", sig["save"]))
                    # The trigger that does not depend on the status slot,
                    # which in production has often held no verdict at all.
                    # Stays true for the rest of the battle, because the
                    # verdict arrives on the screen that replaces this one.
                    ending = True
                    # The battle finished: the Save Replay offer was built.
                    # Reported so a battle nobody saved can be told from a
                    # battle that fell over.
                    if not self._save_screen_reported:
                        self._save_screen_reported = True
                        self._report_end("save_screen_seen")
                # The game's own dialogs. Host leaving first, because it is
                # the answer about the battle, where a drop is about this
                # machine alone. Latched to one report per battle.
                if not self._end_reported:
                    if sig["host_left"] >= HOST_LEFT_NEW:
                        self._end_reported = True
                        self._report_end("host_left")
                    elif sig["dropped"] >= DROPPED_NEW:
                        self._end_reported = True
                        self._report_end("dropped")
                # Last, because it is the slow one and the results screen
                # sits there until somebody clicks it away.
                if ending:
                    self._maybe_read_outcome(pid)
                # Is the lobby back? in_lobby confirms and never denies: its
                # False also covers the menu and the battle browser. The
                # widget count stays as the fallback for a build the pointer
                # walk cannot read.
                back, seen = count_lobby_widget(pid, bases, seen)
                if in_lobby(pid):
                    back = LOBBY_WIDGET_MIN
            except Exception:
                break
            if back >= LOBBY_WIDGET_MIN:
                # Back to the full pass, which refreshes the roster.
                self._report_lobby_back()
                break
        self._known_regions = seen
        # On a battle with no verdict, say what the slot held instead: it tells
        # a verdict that never appeared from one that was not recognised.
        if not self._outcomes and not self._status_outcome:
            kept = getattr(self, "_status_seen", [])
            print("battle ended with no verdict; the status slot held %d "
                  "distinct string(s)%s"
                  % (len(kept),
                     (": " + " | ".join(repr(x[:40]) for x in kept[-12:]))
                     if kept else ""))
        # So the next start waits for the lobby to settle.
        self._battle_ended_at = time.monotonic()

    def _too_soon_after_a_battle(self):
        """Is the lobby still being rebuilt from the battle that just ended? See START_SETTLE."""
        was = getattr(self, "_battle_ended_at", None)
        return was is not None and time.monotonic() - was < START_SETTLE

    def _sample_map(self, pid=None):
        """Latch the lobby's map off the widget drawing it, with the status slot as fallback.

        The widget is what is on screen now; the slot only holds the map line
        after the lobby happens to redraw it.
        """
        try:
            if pid is None:
                pid = self._pid()
            if not pid:
                return
            named = read_map_widget(pid) or map_from_status(status_text(pid) or "")
            if named:
                self._status_map = named
        except Exception:                            # noqa: BLE001
            pass

    def _sample_status(self, pid, seconds):
        """Sleep for `seconds`, reading the status slot every STATUS_SAMPLE_SECONDS.

        A string the game drew and painted over inside one poll is still seen.
        Distinct strings go in the bounded _status_seen list; _battle_status
        does the deciding.
        """
        end = time.monotonic() + seconds
        while not self._stop.is_set():
            if pid:
                try:
                    st = status_text(pid)
                except Exception:
                    st = None
                if st is not None:
                    if st and st != self._status_seen_last:
                        self._status_seen_last = st
                        self._status_seen.append(st)
                        if len(self._status_seen) > STATUS_KEEP:
                            self._status_seen.pop(0)
                    self._battle_status(st)
            if time.monotonic() >= end:
                return
            self._stop.wait(STATUS_SAMPLE_SECONDS)

    def _maybe_read_outcome(self, pid):
        """Read the end-of-battle screen structurally, if it is worth reading again.

        Skipped once answered, once OUTCOME_TRIES is spent, or within
        OUTCOME_RETRY_SECONDS of the last try. Also skipped unless the server
        is on this machine: both readers below are full scans of the game's
        memory, which is not done to a player mid-battle. On a player's
        machine the verdict comes from the status slot alone.
        """
        if self._outcomes or self._outcome_tries >= OUTCOME_TRIES:
            return
        if not self._dev:
            return
        now = time.monotonic()
        if now - self._outcome_at < OUTCOME_RETRY_SECONDS:
            return
        self._outcome_at = now
        self._outcome_tries += 1
        # The verdict count first: the only reader validated against a capture
        # of a real results screen.
        self._read_verdict_rise(pid)
        # The widget sweep once, as a diagnostic: it has never produced a
        # reading in production.
        if self._outcome_tries == 1:
            self._read_outcome(pid)

    def _baseline_verdicts(self, pid):
        """Count the result phrases resident in memory while the battle is still on.

        Only the rise from this baseline means anything: every phrase is
        resident from startup. Taken once, early. A full scan, so developer
        machines only; without it _read_verdict_rise does nothing.
        """
        if not self._dev:
            return
        self._verdict_base = count_verdicts(pid)
        if self._verdict_base is not None:
            print("verdict baseline: %d phrase(s) resident during the battle"
                  % len(self._verdict_base))

    def _read_verdict_rise(self, pid):
        """Which result phrases rose in memory since the baseline (a full scan).

        Every phrase that rose is kept with its rise, not one answer: a team
        screen shows two and nothing here knows which is this player's.
        """
        base = getattr(self, "_verdict_base", None)
        if not base:
            return
        try:
            now = count_verdicts(pid)
        except Exception as e:                   # noqa: BLE001 - diagnostic
            print("verdict scan: FAILED (%s: %s)" % (e.__class__.__name__, e))
            return
        risen = verdicts_risen(base, now)
        if not risen:
            return
        had = bool(self._outcomes)
        for text, n in risen.items():
            if n > self._outcomes.get(text, 0):
                self._outcomes[text] = n
        if not had:
            print("battle result: %r (Method C -- counts that rose)" % risen)

    def _read_outcome(self, pid):
        """The result as the end-of-battle widget tree names it (a full scan).

        Every outcome on screen is kept with a count rather than one picked: a
        team battle shows Victory and Defeat and nothing here knows which row
        is this player's. The raw text carries the margin ("Close Defeat"),
        which the ladder's unit-count rule cannot produce. Failure reports
        nothing and changes nothing.
        """
        had = bool(self._outcomes)
        try:
            ok, outcomes, named = read_results_screen(pid)
        except Exception as e:                   # noqa: BLE001 - diagnostic
            print("results read: FAILED (%s: %s)" % (e.__class__.__name__, e))
            return
        if ok is None:
            print("results read: the game could not be read")
            return
        self._log_results_read(ok, outcomes, named)
        # Merged, never replaced: the Save Replay offer and the Battle Results
        # table are two screens, and a pass on one sees nothing of the other.
        self._results_widget = self._results_widget or bool(ok)
        for text, n in (outcomes or {}).items():
            if n > self._outcomes.get(text, 0):
                self._outcomes[text] = n
        # Logged outside DEV_TRACE: this line is the evidence that the
        # structural read works on another machine.
        if self._outcomes and not had:
            print("battle result: %r (widget=%s, %d pass(es))"
                  % (self._outcomes, ok, self._outcome_tries))

    def _log_results_read(self, ok, outcomes, named):
        """Log what the end-of-battle read saw, every pass, including nothing.

        A silent failure is indistinguishable from a read that never ran.
        Three figures fail in different places: named widgets found at all
        (zero means the sweep is wrong), whether a wanted one was among them
        (no means the names are wrong), and the verdict text hits. Interesting
        names are listed so a renamed button can be found.
        """
        want = ("result", "dismiss", "save", "victor", "defeat", "draw")
        hits = sorted({n for _, n, _ in named
                       if any(w in n.lower() for w in want)})
        print("results read: %d named widget(s), wanted=%s, verdicts=%r%s"
              % (len(named), ok, outcomes or {},
                 (", interesting: " + ", ".join(hits[:12])) if hits else ""))

    def _battle_status(self, st):
        """Act on a status-slot string: latch a verdict, report a dialog, once each per battle.

        The slot churns as names and tooltips are drawn, but what matters
        recurs: a results screen redraws its verdict every second or two and a
        modal dialog stops anything else being drawn. Each report is latched.
        """
        # The verdict, by the game's own shape (is_verdict: "Victory Tickets"
        # is on screen all battle and is not one). First one wins: what the
        # screen said when the battle ended, not what it showed by the time
        # somebody clicked away. Kept under its own name, status_outcome, so
        # it is never confused with the widget reading.
        if not self._status_outcome and is_verdict(st):
            self._status_outcome = st
            # Also promoted into outcomes: the server treats a report with
            # exactly one outcome as the reporter's own claim, which is what
            # names the winning side.
            if not self._outcomes:
                self._outcomes = {st: 1}
            print("battle result: %r (status slot)" % st)
            # Sent now, on its own: the verdict appears after the ending has
            # often gone out already, and nothing would post it otherwise.
            self._report_end("outcome")
        if self._end_reported:
            return
        # Host leaving first: it is the answer about the battle, where a drop
        # is about this machine alone. startswith, because the game renders
        # these with a trailing full stop that the memory strings lack.
        if st.startswith(STATUS_HOST_LEFT):
            self._end_reported = True
            self._report_end("host_left")
        elif st.startswith(STATUS_DROPPED):
            self._end_reported = True
            self._report_end("dropped")
        elif st.startswith(STATUS_STEAM_LOST):
            # From outside this looks like a client that was switched off.
            self._end_reported = True
            self._report_end("steam_lost")
        elif st.startswith(STATUS_DESYNC):
            # Reported even though the desync log is uploaded separately: the
            # log is one file the game overwrites, the dialog cannot be missed.
            self._end_reported = True
            self._report_end("desync")

    def _report_lobby_back(self):
        """Report the ordinary ending: back in the lobby. Latched.

        Without it the archive cannot tell a finished battle from a client
        that stopped reporting; with it, silence means something went wrong.
        """
        if getattr(self, "_lobby_reported", False):
            return
        self._lobby_reported = True
        self._report_end("back_in_lobby")

    def _pid(self):
        """The Napoleon process id, or None."""
        try:
            from lobby_memory import find_game
            return find_game()[0]
        except Exception:
            return None

    def _fetch(self, url):
        """GET a JSON document from the ladder server."""
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def _prime(self):
        """GET the global ladder so the panel has something before the first scan finishes.

        The lobby-filtered answer replaces it a moment later. Failure is not
        reported; the loop is seconds behind with a real answer.
        """
        try:
            data = self._fetch(f"{self.server}/v1/rankings")
            data["scope"] = "global"
            self.out.put(("ok", data))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            pass

    def run(self):
        """The lobby loop: read the game, fetch standings, report state changes."""
        self._prime()
        while not self._stop.is_set():
            if not signed_in():
                # Locked: no memory reads, no roster, no reports until the
                # sign-in has put a key on file.
                self._last_state = STATE_UNKNOWN
                self._lobby_confirmed = False
                self.out.put(("state", (STATE_UNKNOWN, 0)))
                self.out.put(("map", None))
                self._stop.wait(2.0)
                continue
            blank = ([], STATE_UNKNOWN, 0, set(), None, {}, {})
            roster, state, saves, bases, map_key, hosts, signals = blank
            # Set inside the try, read well after it.
            by_pointer = False
            pid, fighting = None, None
            # True when the game is on neither the lobby nor a battle. The
            # last lobby's roster and map stay in memory after the player
            # leaves it, so every lobby reader would describe a lobby that is
            # gone.
            stale_lobby = False
            try:
                # Which screen is up, as a positive sighting of a lobby widget
                # or a battle widget, never the absence of one. The full scan
                # decides a battle by a low widget count, and a lobby halfway
                # through being rebuilt reads the same way, which is where
                # phantom battle starts came from.
                #
                # The full scan is skipped while the lobby is on screen: it
                # takes seconds, and in a lobby everything it answers is known
                # more cheaply (in_lobby, the seat array, the drawn map line).
                # It still runs elsewhere, because telling a battle from the
                # main menu is the one thing no pointer yet answers.
                pid = self._pid()
                fighting = in_battle(pid)
                by_pointer = in_lobby(pid)
                if fighting:
                    state = STATE_BATTLE
                elif by_pointer:
                    state = STATE_LOBBY
                else:
                    # The lobby marker misses about one reading in eight and
                    # costs milliseconds, so ask again before concluding.
                    if by_pointer is False:
                        for _ in range(LOBBY_RECHECKS):
                            time.sleep(LOBBY_RECHECK_SECONDS)
                            if in_lobby(pid):
                                by_pointer = True
                                break
                if by_pointer and not fighting:
                    state = STATE_LOBBY
                elif fighting is False and by_pointer is False:
                    # Both markers read, both absent: the menus, the lobby
                    # browser, or the loading screen into a battle. No lobby
                    # reader runs. The state is carried over rather than
                    # reset, because the loading screen reads like this too
                    # and a start is only reported from a lobby.
                    stale_lobby = True
                    state = self._last_state or STATE_UNKNOWN
                elif not fighting:
                    # Neither marker, and at least one could not be read. The
                    # scan runs for the roster, hosts and map.
                    (roster, state, saves, bases, map_key, hosts,
                     signals) = read_roster_state()
                    # False means the widget table was read and held no battle
                    # widget, which overrules the scan's count. None means it
                    # could not be read, and the scan stands.
                    if state == STATE_BATTLE and fighting is False:
                        state = self._last_state or STATE_UNKNOWN
            except Exception:
                # The game closing mid-scan, antivirus interference, a missing
                # privilege: none of them should take the panel down.
                roster, state, saves, bases, map_key, hosts, signals = blank
            # THE ONE WRITE TO THE GAME'S MEMORY. artilleryfix.apply patches
            # a known crash (a target buffer overrun) in the running game's
            # code, once per game process, and only when the widget table was
            # read and held no battle widget (fighting is False, not None):
            # rewriting code a thread is executing would crash the game.
            # Napoleon.exe on disk is never touched; see artilleryfix.py.
            if pid and fighting is False and pid != getattr(self, "_artillery_pid", None):
                self._artillery_pid = pid
                try:
                    import artilleryfix
                    ok, msg = artilleryfix.apply(pid)
                    print(f"artillery fix: ok={ok} {msg}")
                except Exception as e:          # noqa: BLE001
                    print(f"artillery fix skipped: {e!r}")

            if bases:
                # Where the lobby widget lives, so a battle can be sat out by
                # re-reading a few megabytes rather than the whole process.
                self._lobby_bases = bases

            # A low widget count is also what a single-player session that
            # never touched multiplayer looks like, so a battle is only a
            # battle if the multiplayer UI was there to be torn down.
            if state == STATE_LOBBY:
                self._seen_lobby = True
                # Armed by a roster or by the lobby pointer (the scan is
                # skipped in a lobby, so `roster` is empty on those passes).
                # One occupant is enough: a lobby against the CPU has one.
                if roster or by_pointer:
                    self._lobby_confirmed = True
            elif state == STATE_BATTLE and not self._seen_lobby:
                state = STATE_UNKNOWN

            # One thin scan is not a battle: the pass after a battle ends,
            # while the lobby is rebuilt, reads the same way. Wait for a
            # second reading and drop this pass.
            if state == STATE_BATTLE:
                self._battle_streak += 1
                if self._battle_streak < BATTLE_CONFIRM:
                    continue
            else:
                self._battle_streak = 0

            self.out.put(("state", (state, saves)))

            # A battle beginning is the one event nothing else records: a
            # replay is written when a battle ends, so a battle that collapses
            # leaves nothing. Reported from the last lobby reading, because
            # the lobby is gone from memory once the game is in a battle, and
            # only once a real lobby has been seen since the last battle, so
            # a rebuilding lobby cannot produce a phantom start.
            if (state == STATE_BATTLE and self._last_state == STATE_LOBBY
                    and self._lobby_confirmed
                    and not self._too_soon_after_a_battle()):
                self._report_start()
                # Spent. The next start needs its own lobby.
                self._lobby_confirmed = False
            elif (DEV_TRACE and state == STATE_BATTLE
                    and self._last_state == STATE_LOBBY):
                # With the reason: a suppressed start and an undetected one
                # look identical in a log otherwise.
                print("start suppressed: %s (roster had %d)"
                      % ("still settling after the last battle"
                         if self._too_soon_after_a_battle()
                         else "no confirmed lobby since the last battle",
                         len(roster)))
            self._last_state = state

            if state == STATE_BATTLE:
                # No map line during a battle: the lobby it refers to is over.
                self.out.put(("map", None))
                self._watch_for_results()
                continue

            humans = [(n, s) for n, s in roster if s]
            ai = [n for n, s in roster if not s]

            # The seat array (live_seats) is exact and carries SteamIDs, so
            # when it returns anything it replaces the scanned humans.
            lobby_seats = {}
            # Defined out here: seat_place reads it on every path.
            seat_raw = {}
            # What army each seat brought. Read separately and allowed to fail
            # on its own: the offset is unverified (see roster.ARMY_OFF), so
            # it is reported for checking against replays and nothing depends
            # on it.
            seat_army = {}
            # Who opened the lobby: the first record of the seat array.
            seat_host = ""
            # For _wait_watching_map, which runs between passes.
            self._stale_lobby = stale_lobby
            # Read below by seat_faults whether or not the reader ran.
            seats = []
            # Not after leaving a lobby: the seat array keeps its last
            # occupants too.
            if live_seats is not None and not stale_lobby:
                try:
                    seats = live_seats.read_seats(detail=True)
                    if seats:
                        first = min(seats, key=lambda s: s[5])
                        if first[5] == 0 and first[1]:
                            seat_host = str(first[1])
                        humans = [(n, sid) for n, sid, _t, _p, _r, _i in seats]
                        # The AI seats are not cleared: this reader finds
                        # seats by SteamID and a CPU has none. A stale CPU
                        # from the scan only misdescribes a lobby; AI seats
                        # are kept out of the battle key and every rating.
                        # Where each player sits, so the panel can draw the
                        # lobby's own arrangement.
                        lobby_seats = {n: (t, ps)
                                       for n, _s, t, ps, _r, _i in seats
                                       if t is not None and ps is not None}
                        # The raw byte the alliance and slot were decoded from
                        # (the split is an inference, see roster.team_of) and
                        # the seat's index, reported so a wrong slot can be
                        # diagnosed against replays.
                        seat_raw = {n: (r, i) for n, _s, _t, _p, r, i in seats}
                except Exception:
                    pass
                try:
                    seat_army = live_seats.read_armies() or {}
                except Exception:
                    seat_army = {}

            # The end-of-battle table, while it is still on screen and the
            # scan above has just located it. Opt-in; see _maybe_save_results.
            self._maybe_save_results(roster, map_key)

            # The map. Three signals, weakest last: the line the lobby itself
            # is drawing, then the server-browser record of the host sitting
            # in this lobby, then the thumbnail count (map_key), which is
            # stale exactly when a record matches. A browser record carries
            # the map name in the host's language, so it is resolved to a key
            # before it is believed.
            shown = (None if stale_lobby else
                     pick_lobby_map(hosts, {sid for _, sid in humans if sid}))
            # The drawn line is read each pass rather than remembered: every
            # map line the game has ever shown is still in memory, and only
            # the live lobby draws. Cleared the moment the lobby is off
            # screen, so one lobby's map can never name the next.
            if state == STATE_LOBBY:
                self._sample_map()
            if state != STATE_LOBBY:
                self._status_map = ""
                forget_map_widget()
            named = getattr(self, "_status_map", "")
            if state == STATE_LOBBY:
                # In a lobby the drawn line is the only answer taken, with one
                # exception that is not a count: the browser record of the
                # host sitting in this lobby, which names it by construction.
                # The counting fallbacks are wrong exactly when it matters (a
                # new lobby named after the one before it), and a confident
                # wrong map is recorded against the battle for good.
                if named:
                    key = mapnames.to_key(named) or ""
                else:
                    key = (info_map()
                           or (mapnames.to_key(shown) if shown else "") or "")
                self._lobby_sids = {sid for _, sid in humans if sid}
            elif stale_lobby:
                # The lobby-info record is as stale as the seats: no map.
                self._lobby_sids = set()
                key = None
            else:
                self._lobby_sids = set()
                key = (info_map()
                       or (mapnames.to_key(shown) if shown else map_key))

            # A lobby the game may refuse to start from (a gap in an
            # alliance's seats). Reported only once it has survived several
            # readings; see FAULT_READINGS.
            faults = seat_faults(seats) if seats else []
            if faults and faults == self._last_faults:
                self._fault_runs += 1
            else:
                self._fault_runs = 1 if faults else 0
            self._last_faults = faults
            self.out.put(("fault", faults if self._fault_runs >= FAULT_READINGS
                          else []))

            # The host, for the start report. The browser records are keyed
            # by the host's SteamID64; exactly one match in the room names the
            # host, two is unanswerable. The seat array (seat index 0) is
            # preferred: it is there for every lobby, where the records only
            # cover players who came in through the browser. Disagreement is
            # logged, not decided.
            sids = {sid for _, sid in humans if sid}
            owners = [sid for sid in hosts if sid in sids]
            scan_host = str(owners[0]) if len(owners) == 1 else ""
            host = seat_host or scan_host
            if DEV_TRACE and scan_host and seat_host and scan_host != seat_host:
                print("host: seat 0 says %s, browser record says %s"
                      % (seat_host, scan_host))
            # What the report carries per player: name and SteamID (the pair
            # cannot be recovered later, once the lobby is gone), and where
            # they sat, which is what lets a battle with no replay say which
            # side won. Team and slot are sent one-based so a missing value
            # is 0, matching the replay's own numbering.
            def seat_place(name):
                """The seat fields for one player's report entry, or {}."""
                tp = lobby_seats.get(name)
                if not tp:
                    return {}
                t, ps = tp
                out = {"team": t + 1, "slot": ps + 1}
                # The byte these were decoded from and the seat's index, so a
                # wrong split can be worked out from the archive against
                # replays rather than needing the bug reproduced.
                raw = seat_raw.get(name)
                if raw:
                    out["seat_raw"], out["seat_index"] = raw[0], raw[1]
                # The army, under its own name, so an unconfirmed reading can
                # be dropped later without disturbing the rest.
                army = seat_army.get(name)
                if army:
                    out["army"] = army
                return out

            players = [dict({"name": n, "steam_id": str(sid) if sid else ""},
                            **seat_place(n)) for n, sid in humans]
            # The CPUs too, marked as such, so a battle against three of them
            # is not reported as a battle with one player. The server keeps
            # them out of the battle key (see battleKey).
            players += [dict({"name": n, "steam_id": "", "is_ai": True},
                             **seat_place(n)) for n in ai]
            if DEV_TRACE:
                # The roster as it will be sent.
                print("lobby: %d human(s) %r, %d ai %r, places %r"
                      % (len(humans), [n for n, _ in humans], len(ai), ai,
                         lobby_seats))
            # Kept from the last real lobby while stale: the loading screen
            # into a battle reads as stale, and the start is built from this.
            if not stale_lobby:
                self._lobby_snapshot = {
                    "map": key or "",
                    "host": host,
                    "host_name": next((n for n, sid in humans
                                       if str(sid) == host), ""),
                    "players": players,
                }
            if key != self._last_map:
                self._last_map = key
                self.out.put(("map", key))

            try:
                # Only ask the server when the roster changes: ratings move
                # only when a replay is uploaded.
                key = tuple(sorted(n for n, _ in humans)) + tuple(sorted(ai))
                if key == getattr(self, "_last_key", None):
                    # Same roster, but people swap sides and places without
                    # anyone joining, so the seats go to the panel on their
                    # own, without a request.
                    if lobby_seats != getattr(self, "_last_seats", None):
                        self._last_seats = dict(lobby_seats)
                        self.out.put(("seats", {"seats": lobby_seats,
                                                "me": self.local or ""}))
                    self._wait_watching_map()
                    continue
                self._last_key = key
                self._last_seats = dict(lobby_seats)

                if humans:
                    # GET the standings of the players present: name and
                    # Steam ID per seat, in step, so badges follow the account
                    # whatever it is called today. See getRankings on the
                    # server.
                    q = "&".join("player=" + urllib.parse.quote(n)
                                 + "&steam_id=" + urllib.parse.quote(str(s or ""))
                                 for n, s in humans)
                    data = self._fetch(f"{self.server}/v1/rankings?{q}")
                    data["scope"] = "lobby"
                else:
                    data = self._fetch(f"{self.server}/v1/rankings")
                    data["scope"] = "global"
                # AI seats are shown but carry no rating.
                data["ai"] = ai
                data["seats"] = lobby_seats
                # The viewer, so the panel can mark their own deployment box.
                data["me"] = self.local or ""
                self.out.put(("ok", data))
            except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
                self.out.put(("error", str(e)))
            self._wait_watching_map()

    def _report_start(self):
        """POST /v1/gameStart: a battle began, from the last lobby reading.

        The body is the lobby snapshot: map, host, and each player's name,
        Steam ID, seat and army. Best effort on screen, but logged either way,
        with the reason when nothing is sent: this is the one event nothing
        else records.
        """
        if not signed_in():
            print("start not reported: not signed in")
            return
        snap = self._lobby_snapshot
        if not snap or not snap.get("players"):
            print("game start: nothing reported -- no lobby reading was held")
            return
        # One player means one human against the CPU: a test game that earns
        # no rating and would spoil the finish-rate. Reported anyway against a
        # local server, which is how the chain is tested without eight people.
        if len(snap["players"]) < 2 and not self._dev:
            print("game start: not reported -- one human, so a battle "
                  "against the CPU (reported when the server is local)")
            return
        self._lobby_snapshot = None
        # Held for the ending, which must name the same battle.
        self._battle = snap
        # So a postbattle file can be told to be this battle's.
        self._battle_began = time.time()
        # Per-battle state; see __init__ for what each is.
        self._end_reported = False
        self._save_screen_reported = False
        self._outcomes = {}
        self._results_widget = False
        self._outcome_tries = 0
        self._outcome_at = 0.0
        self._status_outcome = None
        self._status_seen = []
        self._status_seen_last = None
        self._lobby_reported = False
        self._last_beat = None
        try:
            body = json.dumps(snap).encode("utf-8")
            req = urllib.request.Request(
                f"{self.server}/v1/gameStart", data=body, method="POST",
                headers={"Content-Type": "application/json",
                         "User-Agent": USER_AGENT,
                         "X-Player": self.local or "",
                         **steamauth.headers()})
            with urllib.request.urlopen(req, timeout=10) as r:
                print("game start: reported %s, %d player(s), server said %s"
                      % (snap.get("map") or "?", len(snap["players"]),
                         r.getcode()))
        except Exception as e:                   # noqa: BLE001 - never fatal
            print("game start: report FAILED (%s: %s)"
                  % (e.__class__.__name__, e))

    def _trace_scan(self, sig, tally, began, took, gap, fresh=0):
        """Print what the battle scan is seeing. Developer builds only (DEV_TRACE).

        A line when the counts change, when a pass was slow, and once a minute
        regardless, so a quiet log still proves the loop is alive. Elapsed
        time is from the start of the battle.
        """
        now = time.monotonic()
        counts = (sig["save"], sig["host_left"], sig["dropped"])
        slow = took >= SLOW_PASS_SECONDS or gap >= SLOW_PASS_SECONDS
        if not (counts != self._trace_prev or slow
                or now - self._trace_at >= 60):
            return
        self._trace_prev = counts
        self._trace_at = now
        print("battle scan: +%.1fs save=%d host_left=%d dropped=%d "
              "regions=%d fresh=%d pass=%dms gap=%.2fs%s"
              % (now - began, counts[0], counts[1], counts[2], len(tally),
                 fresh, took * 1000, gap, "  SLOW" if slow else ""))

    def _report_end(self, kind):
        """POST /v1/gameStart with a kind: how a battle ended, or that it is still running.

        The same body as the start (the server keys a battle on its map and
        roster) plus kind, the outcomes read off the screen, and the game's
        own results table if the post-battle script wrote one. Best effort,
        logged either way. For a host leaving this is the only trace: no
        replay is written and no desync file.
        """
        if not signed_in():
            return
        snap = self._battle
        if not snap:
            return
        body = dict(snap)
        body["kind"] = kind
        # Every outcome on screen with its count, not one answer: a team
        # battle shows both. results_widget says the screen's own button was
        # found, so a reading from the wrong screen is recognisable later.
        if getattr(self, "_outcomes", None):
            body["outcomes"] = self._outcomes
        if getattr(self, "_results_widget", False):
            body["results_widget"] = True
        # The status-slot reading under its own name, never merged: one is a
        # fact about the widget tree, the other about what was last drawn.
        if getattr(self, "_status_outcome", None):
            body["status_outcome"] = self._status_outcome
        # The game's results table from POSTBATTLE_FILE, if written since this
        # battle began. Attached to every report from then on, so whichever
        # report goes out last carries it.
        post = self._postbattle_since(getattr(self, "_battle_began", 0))
        if post is not None:
            body["postbattle"] = post
        try:
            req = urllib.request.Request(
                f"{self.server}/v1/gameStart",
                data=json.dumps(body).encode("utf-8"), method="POST",
                headers={"Content-Type": "application/json",
                         "User-Agent": USER_AGENT,
                         "X-Player": self.local or "",
                         **steamauth.headers()})
            with urllib.request.urlopen(req, timeout=10) as r:
                # The verdict is named in the log, so a verdict never read can
                # be told from one read and dropped on the way out.
                print("battle end: reported %s, outcome %r, server said %s"
                      % (kind, body.get("status_outcome") or
                         body.get("outcomes") or None, r.getcode()))
        except Exception as e:                   # noqa: BLE001 - never fatal
            print("battle end: report FAILED (%s: %s)"
                  % (e.__class__.__name__, e))

    def _postbattle_since(self, began):
        """Read POSTBATTLE_FILE if it was written after `began` and parses. Else None.

        Parsed here so a half-written file is dropped on this side rather
        than sent as junk.
        """
        try:
            st = os.stat(POSTBATTLE_FILE)
            if st.st_mtime < began or st.st_size > (64 << 10):
                return None
            with open(POSTBATTLE_FILE, "r", encoding="utf-8") as f:
                doc = json.load(f)
            return doc if isinstance(doc, dict) and "info" in doc else None
        except (OSError, ValueError):
            return None

    def _maybe_save_results(self, roster, map_key):
        """Write the end-of-battle table to a file in RESULTS_DIR, once per battle.

        Off unless --save-battle-results. Reads the results screen out of the
        game's memory (results.read_now, a few megabytes in regions the scan
        has already found) and writes it locally; nothing reads RESULTS_DIR
        and it is sent nowhere. Research material: the kills and losses
        columns are still wrong, so the file keeps every string in address
        order under the parsed table.
        """
        if not self.save_battle_results:
            return
        try:
            regions = results_regions()
            if not regions:
                # No results screen. Arm for the next one.
                self._saved_results = None
                return
            if getattr(self, "_saved_results", None) == frozenset(regions):
                return  # Same screen as last poll; already written.

            pid, _name = find_game()
            if not pid:
                return
            import results as results_reader

            text, rows = results_reader.read_now(
                pid, regions,
                {"map": map_key or "unknown",
                 "roster": ", ".join(n for n, _s in roster) or "unknown"},
            )
            if not text:
                return

            os.makedirs(RESULTS_DIR, exist_ok=True)
            path = os.path.join(
                RESULTS_DIR, time.strftime("battle_%Y%m%d_%H%M%S.txt"))
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            self._saved_results = frozenset(regions)
            self.out.put(("note", f"battle results saved: {len(rows)} rows"))
        except Exception as e:
            # Never take the panel down for a bonus reading, but say so once:
            # a bare pass hid a missing hidden import for a whole release.
            if not getattr(self, "_results_warned", False):
                self._results_warned = True
                self.out.put(("note", f"battle results unavailable: "
                                      f"{type(e).__name__}: {e}"))

    def _wait_watching_map(self):
        """Sleep out the poll interval, re-reading the map cheaply every MAP_RECHECK_SECONDS.

        Re-reads only the regions that held the map last time, so a map that
        appears or changes just after a full pass shows in about a second.
        Reports only a map not already reported, and never clears one:
        silence means the regions moved, and the next full pass re-finds them.
        """
        deadline = time.monotonic() + self.interval
        while not self._stop.is_set() and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            self._stop.wait(min(MAP_RECHECK_SECONDS, max(remaining, 0)))
            if self._stop.is_set():
                return
            if getattr(self, "_stale_lobby", False):
                # Out of the lobby: these records are the last lobby's.
                continue
            try:
                self._sample_map()
                named = self._status_map
                key = (mapnames.to_key(named) if named else "") or ""
                # Never read_map's counting guess, only the record of the host
                # sitting in our lobby, as the full pass does.
                if not key:
                    _guess, hosts = read_map()
                    shown = pick_lobby_map(hosts, getattr(self, "_lobby_sids", set()))
                    key = info_map() or (mapnames.to_key(shown) if shown else "") or ""
            except Exception:
                continue
            if key and key != self._last_map:
                self._last_map = key
                self.out.put(("map", key))


class Overlay:
    """The panel itself: a borderless topmost tkinter window drawn on one canvas.

    Owns the Poller and MapWatcher threads and drains their queue on the Tk
    thread. Besides drawing, it saves its position to STATE_FILE, lists the
    game's replay folder to confirm a replay was saved, runs the uploader
    while collapsed, downloads map pictures into MAP_CACHE_DIR, and POSTs a
    game-closed notice before quitting.
    """

    def __init__(self, root, server, demo=False, scale=None, watch_game=True,
                 opacity=DEFAULT_OPACITY, update_to=None,
                 save_battle_results=False):
        """Build the window and start the threads (none in --demo)."""
        self.root = root
        self.server = server.rstrip("/")
        self.demo = demo
        # The newest version the server advertises, if newer than this one.
        self.update_to = update_to
        self.queue = queue.Queue()
        self.players = []
        # The lobby's map, as read from memory, and the window showing it.
        self.map_key = None
        self._map_win = None
        # (map, shape, my seat) the open map window was drawn for.
        self._map_drawn_for = None
        # Alliances whose seats have a hole in them, once several readings
        # have agreed. Empty in a healthy lobby.
        self.lobby_fault = []
        # {name: (alliance, position)} for the lobby on screen, empty when the
        # seats could not be read.
        self.seats = {}
        # The local player's name, as the game knows it. Sent by the Poller.
        self.me = ""
        self.ai = []
        self.scope = "global"
        self.status = "starting"
        self.last_ok = None

        # Scaled from the OS DPI so the panel is the same physical size on
        # any monitor.
        if scale is None:
            try:
                scale = root.winfo_fpixels("1i") / 96.0
            except tk.TclError:
                scale = 1.0
        self.s = max(0.8, min(scale, 3.0))

        self.w = int(BASE_W * self.s)
        self.h = int(BASE_H * self.s)
        self.tab_w = int(BASE_TAB_W * self.s)
        self.tab_h = int(BASE_TAB_H * self.s)
        self.collapsed = False
        self.watcher = None
        self._stopping = False
        self.auto_hidden = False
        # None until the first check: "unknown" and "not running" differ, and
        # only the second earns the footer notice.
        self.game_running = None
        # Which screen the game is on, from its memory. See _on_state.
        self.game_state = STATE_UNKNOWN
        # When the panel has announced it is about to collapse and has not
        # yet. Drives the footer only.
        self._hiding_at = None
        # The save-widget count while the battle was running, so a rise above
        # it is the results screen. None until sampled.
        self._save_base = None
        # Whether the save-the-replay message has been shown for this battle.
        self._asked_to_save = False
        # The .replay files that existed when this battle started, so a new
        # one can be recognised. None means the folder could not be read.
        self._replays_before = None
        # Whether a new replay has appeared since: turns the warning into a
        # confirmation.
        self._saved_seen = False
        # Life cycle: wait for a game, sit beside it, report its exit and go.
        # Auto-close is armed by _game_seen, so it cannot fire while waiting.
        self._game_seen = False
        self._game_gone = 0
        self._closing = False
        self._close_tries = 0

        root.title("NTW3 Ladder")
        root.overrideredirect(True)      # no OS title bar; we draw our own
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", opacity)
        except tk.TclError:
            pass
        # After -alpha, which replaces the extended style word rather than
        # adding to it; NOACTIVATE set before it is lost.
        root.update_idletasks()
        _no_activate(root)
        root.configure(bg=EDGE)

        x, y = self._restore_position()
        root.geometry(f"{self.w}x{self.h}+{x}+{y}")

        self.canvas = tk.Canvas(
            root, width=self.w, height=self.h,
            highlightthickness=0, bd=0, bg=BODY,
        )
        self.canvas.pack(fill="both", expand=True)

        px = max(9, int(11 * self.s))
        self.f_title = tkfont.Font(family=self._pick_font(TITLE_FONTS),
                                   size=int((px + 2) * 1.2))
        self.f_row = tkfont.Font(family=self._pick_font(TITLE_FONTS), size=px + 1)
        self.f_small = tkfont.Font(family=self._pick_font(TITLE_FONTS),
                                   size=max(9, px + 1))

        self._make_draggable()
        self.root.bind("<Escape>", lambda e: self.quit())
        # Double-click anywhere toggles, so the panel can always be dismissed
        # even if the automatic hide never fires.
        self.canvas.bind("<Double-Button-1>", lambda e: self.toggle())
        # The hover text: the window showing it, the timer that opens it,
        # and the regions drawn on this pass as [(x0, y0, x1, y1, text)].
        self._tip = None
        self._tip_after = None
        self._tip_regions = []
        # The account menu: open or not, whether disconnect is waiting for its
        # second click, and whether a sign-in or sign-out is in flight.
        self._menu_open = False
        self._menu_confirm = False
        self._menu_busy = ""
        self._menu_consumed = False
        # A sign-in waiting on the browser: (url to reopen, cancel event), or
        # None. Drawn by _draw_locked.
        self._signin = None

        self.draw()
        if demo:
            self.players = DEMO_PLAYERS
            self.players[0]["host"] = dict(DEMO_HOST[DEMO_HOST_MARK], mark=DEMO_HOST_MARK)
            self.players[1]["redcoatz"]["version"] = VERSION
            self.players[0]["redcoatz"]["version"] = VERSION
            self.seats = DEMO_SEATS
            self.status = "demo data"
            self.draw()
        else:
            self.poller = Poller(self.server, self.queue,
                                 save_battle_results=save_battle_results)
            self.poller.start()
            self.mapwatch = MapWatcher(self.queue)
            self.mapwatch.start()
            self.root.after(200, self._drain)
        self.root.after(PANEL_CHECK_MS, self._hold_on_top)
        if watch_game:
            self.root.after(1000, self._check_game)
        else:
            # --no-auto-hide turns off collapsing and the follow-the-game-out
            # exit, not the footer's game-running report.
            self.root.after(1000, self._poll_game_presence)

    def _hold_on_top(self):
        """Every PANEL_CHECK_MS, re-raise the panel if a topmost window covers it.

        Raising only when covered keeps the common case to a walk of a few
        window handles and avoids a raise war. Skipped while the map window
        is open (it holds itself on top) or a hover tip is showing. The map's
        liveness is checked against the window, not the reference: a dead
        Toplevel left here once switched the re-raise off for good.
        """
        win = self._map_win
        if win is not None and not win.winfo_exists():
            self._map_win = win = None

        if not win and self._tip is None and _covered(self.root):
            _win32_raise(self.root)
        self.root.after(PANEL_CHECK_MS, self._hold_on_top)

    def _pick_font(self, candidates):
        """First candidate the system has, so a missing font degrades to the next serif."""
        try:
            have = {f.lower() for f in tkfont.families(self.root)}
        except tk.TclError:
            return candidates[-1]
        for f in candidates:
            if f.lower() in have:
                return f
        return candidates[-1]

    # ---- window behaviour --------------------------------------------------

    def _make_draggable(self):
        """Drag by the body (there is no title bar); the position is saved on release."""
        self._drag = {"x": 0, "y": 0}

        def press(e):
            self._drag["x"], self._drag["y"] = e.x, e.y
            # Canvas item bindings fire before this one: a click the menu
            # handled says so, and any other click closes it.
            if self._menu_consumed:
                self._menu_consumed = False
            elif self._menu_open:
                self._menu_open = False
                self._menu_confirm = False
                self.draw()

        def drag(e):
            x = self.root.winfo_x() + e.x - self._drag["x"]
            y = self.root.winfo_y() + e.y - self._drag["y"]
            self.root.geometry(f"+{x}+{y}")

        self.canvas.bind("<Button-1>", press)
        self.canvas.bind("<B1-Motion>", drag)
        self.canvas.bind("<ButtonRelease-1>", lambda e: self._save_position())

    def _restore_position(self):
        """The panel's saved position from STATE_FILE, or a default."""
        return _saved_position(self.root, self.w)

    def _save_position(self):
        """Write the panel's position to STATE_FILE."""
        self._state_write(x=self.root.winfo_x(), y=self.root.winfo_y())

    @staticmethod
    def _state_read():
        """Read STATE_FILE as a dict; empty if missing or unreadable."""
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _state_write(self, **kw):
        """Merge keys into STATE_FILE rather than replacing it.

        The panel's position and the map window's share this file.
        """
        d = self._state_read()
        d.update(kw)
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(d, f)
        except OSError:
            pass  # a panel that cannot save its position still works

    def _map_position(self, w, h):
        """Where the map window opens: where the player last dragged it, else centred.

        A saved position off every screen is refused: monitors change.
        """
        d = self._state_read()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        try:
            x, y = int(d["map_x"]), int(d["map_y"])
            if -50 <= x <= sw - 100 and -50 <= y <= sh - 100:
                return x, y
        except (KeyError, ValueError, TypeError):
            pass
        return max(0, (sw - w) // 2), max(0, (sh - h) // 2)

    def quit(self):
        """Save the position and destroy the window once the current event has unwound.

        Destroying Tk from inside one of its own event handlers corrupts
        Tcl's heap and kills the process with no traceback.
        """
        self._save_position()
        self.root.after_idle(self.root.destroy)

    def _shutdown(self, notify=True):
        """Report the exit to the server (unless already done), then exit the process.

        os._exit, because the poller and uploader are daemon threads and one
        blocked in a socket read or a memory read can keep a headless process
        alive after the window has gone.
        """
        try:
            self._stopping = True
            self._save_position()
            if notify:
                self._notify_closed()
            if getattr(self, "poller", None):
                self.poller.stop()
            if getattr(self, "mapwatch", None):
                self.mapwatch.stop()
            self.root.destroy()
        except Exception:
            pass
        finally:
            os._exit(0)

    # ---- collapse ----------------------------------------------------------

    def _watch_replays(self, on):
        """Run the replay uploader on a thread while the panel is collapsed.

        Collapsed is the normal state during a battle, which is when a replay
        gets written. The uploader (uploader.Uploader) scans the game's replay
        folder, REPLAY_DIR, every 20 seconds and uploads new .replay files to
        the ladder server.
        """
        if on and self.watcher is None:
            try:
                from uploader import Uploader, REPLAY_DIR, detect_player
                name, _sid = detect_player() or (None, None)
                up = Uploader(self.server, name, REPLAY_DIR)

                def loop():
                    """Scan REPLAY_DIR and upload new replays every 20 s while collapsed."""
                    while self.collapsed and not self._stopping:
                        try:
                            for path in up.scan():
                                up.upload(path)
                            up.drain_queue()
                        except Exception:
                            pass          # a failed pass must not kill the panel
                        time.sleep(20)
                    self.watcher = None

                self.watcher = threading.Thread(target=loop, daemon=True)
                self.watcher.start()
            except Exception:
                self.watcher = None

    def toggle(self, collapsed=None):
        """Shrink to the small badge, or restore.

        Collapsing rather than hiding: a window that disappears leaves the
        player no way to get it back without a hotkey.
        """
        want = (not self.collapsed) if collapsed is None else collapsed
        if want == self.collapsed:
            return
        self.collapsed = want
        self._watch_replays(want)
        if want:
            self.canvas.config(width=self.tab_w, height=self.tab_h)
            self.root.geometry(f"{self.tab_w}x{self.tab_h}")
        else:
            self.canvas.config(width=self.w, height=self.h)
            self.root.geometry(f"{self.w}x{self.h}")

        # Make Windows apply the resize now. A borderless topmost window over
        # a fullscreen game otherwise waits for input before redrawing, which
        # is no good when the message is "save the replay now". Safe because
        # every caller is on the Tk thread.
        try:
            self.root.update_idletasks()
            self.root.lift()
            self.root.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.draw()

    def _on_state(self, payload):
        """React to the game moving between the lobby and a battle (from the Poller).

        Read from the game's memory rather than window titles: a title says a
        window exists, not which screen it shows.
        """
        state, saves = payload

        # The results screen reads as STATE_BATTLE, so it is found by the Save
        # Replay button appearing. Coming back out for it is the point: the
        # button is the last moment the replay can be saved.
        if state == STATE_BATTLE:
            if self._save_base is None:
                self._save_base = saves
            elif save_button_showing(saves, self._save_base):
                self._on_results_screen()
        else:
            self._save_base = None

        if state == self.game_state:
            return
        was, self.game_state = self.game_state, state

        # Into a battle: say so, then get out of the way. A panel that
        # vanishes without a word reads as a crash.
        if state == STATE_BATTLE and was == STATE_LOBBY:
            # Snapshot the replay folder now: a file appearing after this is
            # this battle's replay, even from a player quick to save.
            self._replays_before = self._replay_names()
            self._saved_seen = False
            # The map goes with the lobby it describes.
            self._close_map()
            if not self.collapsed:
                self._hiding_at = time.time() + BATTLE_HIDE_DELAY
                self.root.after(int(BATTLE_HIDE_DELAY * 1000),
                                self._hide_for_battle)
                self._tick_hide()

        # Back to the lobby: undo only a collapse the panel made itself.
        if state == STATE_LOBBY:
            self._asked_to_save = False
            if self.collapsed and self.auto_hidden:
                self._hiding_at = None
                self.auto_hidden = False
                self.toggle(False)

    def _replay_names(self):
        """List the .replay file names in the game's replay folder (uploader.REPLAY_DIR).

        A listing only; nothing is opened. None if the folder cannot be read,
        kept apart from empty so the reminder never claims a replay was saved
        because a listing failed.
        """
        try:
            from uploader import REPLAY_DIR
            return {n for n in os.listdir(REPLAY_DIR)
                    if n.lower().endswith(".replay")}
        except Exception:                  # noqa: BLE001 - missing, denied...
            return None

    def _on_results_screen(self):
        """Come back out and ask for the replay to be saved. Once per battle."""
        if self._asked_to_save:
            return
        self._asked_to_save = True
        self._hiding_at = None
        if self.collapsed and self.auto_hidden:
            self.auto_hidden = False
            self.toggle(False)
        self._watch_for_save()
        self.draw()

    def _watch_for_save(self):
        """List the replay folder every half second while the reminder is up.

        A new .replay file is the one certain sign the replay was saved.
        """
        if not self._asked_to_save or self._saved_seen:
            return
        now = self._replay_names()
        if now is not None and self._replays_before is not None:
            if now - self._replays_before:
                self._saved_seen = True
                self.draw()
                return
        self.root.after(500, self._watch_for_save)

    def _tick_hide(self):
        """Redraw while the collapse countdown runs, so the counter moves."""
        if not self._hiding_at:
            return
        self.draw()
        self.root.after(400, self._tick_hide)

    def _hide_for_battle(self):
        """Collapse after the countdown, unless the player has already acted by hand."""
        self._hiding_at = None
        if self.game_state == STATE_BATTLE and not self.collapsed:
            self.auto_hidden = True
            self.toggle(True)
        self.draw()

    def _check_game(self):
        """Every GAME_CHECK_SECONDS: is the game still running, and should the panel collapse.

        Only ever overrides a collapse the panel made itself; a collapse by
        hand stays. Follows the game out: once Napoleon has been seen and is
        gone for GAME_GONE_CHECKS checks, report and exit, so no headless
        memory-scanning process is left behind.
        """
        # Only the game's own windows: with no game process there is no
        # battle, whatever a browser tab is called.
        pid = game_pid()
        titles = " | ".join(game_windows(pid)).lower() if pid else ""
        in_battle = bool(titles) and any(h in titles for h in BATTLE_TITLE_HINTS)

        # The process, not window titles: this panel's own title contains
        # "NTW3" and would match.
        present = game_is_running()
        self._set_game_running(present)
        running = True if present is None else present   # cannot tell: stay open

        if running:
            self._game_seen = True
            self._game_gone = 0
            self._close_tries = 0
        elif not self._game_seen:
            # Waiting for the game to be launched. The panel never stands
            # itself down here, however long that takes.
            self._game_gone = 0
        else:
            self._game_gone += 1
            if self._game_gone >= GAME_GONE_CHECKS and self._report_and_close():
                return

        # Window-title fallback, only when memory has not answered. _on_state
        # does this properly; this collapses without the announcement because
        # a title gives no reliable moment to announce.
        if self.game_state == STATE_UNKNOWN:
            if in_battle and not self.collapsed:
                self.auto_hidden = True
                self.toggle(True)
            elif not in_battle and self.collapsed and self.auto_hidden:
                self.auto_hidden = False
                self.toggle(False)
        self.root.after(int(GAME_CHECK_SECONDS * 1000), self._check_game)

    def _poll_game_presence(self):
        """Game-presence check for --no-auto-hide: the footer only, no collapse or exit."""
        self._set_game_running(game_is_running())
        self.root.after(int(GAME_CHECK_SECONDS * 1000), self._poll_game_presence)

    def _set_game_running(self, present):
        """Record whether the game is running, redrawing only on a change."""
        if present:
            # Latched here too, so the quit button also disappears on the
            # --no-auto-hide path.
            self._game_seen = True
        if present != self.game_running:
            self.game_running = present
            self.draw()

    # ---- the lobby's map ---------------------------------------------------

    def _show_map(self):
        """Open the map window: the battlefield and deployment boxes, drawn by the server.

        A download (_map_png, cached in MAP_CACHE_DIR) and a blit: Tk reads
        PNG on its own, so there is no image library in the client and no
        second copy of the zone geometry.
        """
        if not self.map_key:
            return
        if self._map_win is not None and self._map_win.winfo_exists():
            self._raise_map(self._map_win)
            return

        key = self.map_key
        px = MAP_WINDOW_PX
        pad = MAP_WINDOW_PAD
        hdr = MAP_WINDOW_HEADER
        w, h = px + 2 * pad, px + hdr + 2 * pad

        win = tk.Toplevel(self.root)
        self._map_win = win
        # What this picture is of, so a later swap can notice it is stale.
        self._map_drawn_for = (key, self._battle_shape(), self._my_seat())
        # Borderless like the panel, with the frame drawn in the game's
        # palette instead of the system title bar.
        win.overrideredirect(True)
        win.configure(bg=EDGE)
        # Above the game, or it opens behind a fullscreen battle.
        win.attributes("-topmost", True)
        mx, my = self._map_position(w, h)
        win.geometry(f"{w}x{h}+{mx}+{my}")

        c = tk.Canvas(win, width=w, height=h, bg=BODY,
                      highlightthickness=0, bd=0)
        c.pack()

        # Frame and title bar, drawn rather than borrowed.
        c.create_rectangle(0, 0, w - 1, h - 1, outline=GOLD, width=2)
        c.create_rectangle(1, 1, w - 2, hdr, fill=HEADER, outline=HEADER)
        c.create_text(pad + 2, hdr // 2 + 1, anchor="w", text=map_label(key),
                      fill=TITLE, font=self.f_title)
        # The close button: a filled plate with the glyph on top, both tagged.
        # A canvas text item is only clickable where its ink is, so the plate
        # gives it an area; the multiplication sign is in every Windows font.
        bx, by = w - pad - 11, hdr // 2
        c.create_rectangle(bx - 10, by - 9, bx + 10, by + 9,
                           fill=HEADER, outline=HEADER, tags="shut")
        c.create_text(bx, by + 1, text="×", fill=TITLE,
                      font=self.f_title, tags="shut")

        # The topographic toggle, beside the close button and drawn the same
        # way: a plate to aim at, with the label on top.
        tx = w - pad - 34 - 46
        c.create_rectangle(tx - 44, by - 9, tx + 44, by + 9,
                           fill=HEADER, outline=HEADER, tags="topo")
        topo_label = c.create_text(tx, by + 1, text="Map",
                                   fill=TITLE, font=self.f_small, tags="topo")
        # The label names what is on screen, not what the next click gives.
        VIEWS = (("", "Map"), ("relief", "Topographic"), ("speed", "Speed"))

        holder = c.create_text(w // 2, hdr + pad + px // 2,
                               text="fetching the map...", fill=DIM,
                               font=self.f_small)
        view = {"i": 0, "holder": holder, "img": None}

        # Draggable from anywhere; the position is saved to STATE_FILE.
        drag = {"x": 0, "y": 0}

        def press(e):
            drag["x"], drag["y"] = e.x, e.y

        def move(e):
            win.geometry(f"+{win.winfo_x() + e.x - drag['x']}"
                         f"+{win.winfo_y() + e.y - drag['y']}")

        c.bind("<Button-1>", press)
        c.bind("<B1-Motion>", move)
        c.bind("<ButtonRelease-1>",
               lambda e: self._state_write(map_x=win.winfo_x(),
                                           map_y=win.winfo_y()))

        def shut(_e=None):
            # Through _close_map so the reference is cleared too; a dead
            # window left in self._map_win switches off the panel's re-raise.
            self._close_map()
            # Stop the canvas-wide press handler running on a destroyed widget.
            return "break"

        win.bind("<Escape>", shut)
        c.tag_bind("shut", "<Button-1>", shut)
        c.tag_bind("shut", "<Enter>", lambda e: c.config(cursor="hand2"))
        c.tag_bind("shut", "<Leave>", lambda e: c.config(cursor=""))

        def done(path, err):
            """Show the downloaded picture, or the reason there is none."""
            if not win.winfo_exists():
                return
            if err:
                # Some maps have no readable heightmap and so no topographic
                # view: a gap in the catalogue, not a failure to load.
                if VIEWS[view["i"]][0]:
                    c.itemconfig(view["holder"], fill=DIM,
                                 text="no %s view for this map"
                                      % VIEWS[view["i"]][0])
                    return
                c.itemconfig(view["holder"], text=f"could not load the map\n{err}",
                             fill=LOSS)
                return
            try:
                img = tk.PhotoImage(file=path)
            except tk.TclError as exc:
                c.itemconfig(view["holder"], text=f"could not read the image\n{exc}",
                             fill=LOSS)
                return
            c.delete("holder")
            # Held on the canvas: PhotoImage is collected the moment the last
            # Python reference goes, and Tk then silently draws nothing.
            c.image = img
            view["img"] = c.create_image(pad, hdr + pad, anchor="nw",
                                         image=img)
            # Again once the picture is in, over anything raised meanwhile.
            self._raise_map(win)

        # The worker fills a plain dict and the Tk thread polls it. Calling
        # root.after() from another thread is not safe.
        result = {}

        def fetch():
            """Worker thread: fetch the picture (from cache or the server) into result."""
            try:
                result["path"] = self._map_png(key, px, VIEWS[view["i"]][0])
            except Exception as exc:                     # noqa: BLE001
                result["err"] = str(exc)

        def poll():
            if not win.winfo_exists():
                return
            if "path" in result:
                done(result["path"], None)
            elif "err" in result:
                done(None, result["err"])
            else:
                win.after(MAP_POLL_MS, poll)

        def load():
            """(Re)fetch whichever view is selected, into the same window."""
            if view["img"] is not None:
                c.delete(view["img"])
                view["img"] = None
            c.delete("holder")
            view["holder"] = c.create_text(
                w // 2, hdr + pad + px // 2, tags="holder",
                text="fetching the %s..." % (VIEWS[view["i"]][0] or "map"),
                fill=DIM, font=self.f_small)
            result.clear()
            threading.Thread(target=fetch, daemon=True).start()
            win.after(MAP_POLL_MS, poll)

        def toggle(_e=None):
            view["i"] = (view["i"] + 1) % len(VIEWS)
            c.itemconfig(topo_label, text=VIEWS[view["i"]][1])
            load()
            return "break"

        c.tag_bind("topo", "<Button-1>", toggle)
        c.tag_bind("topo", "<Enter>", lambda e: c.config(cursor="hand2"))
        c.tag_bind("topo", "<Leave>", lambda e: c.config(cursor=""))
        c.addtag_withtag("holder", holder)

        threading.Thread(target=fetch, daemon=True).start()
        win.after(MAP_POLL_MS, poll)
        # As for the panel: a click here must not take activation off the
        # game, which gives up fullscreen when it loses it.
        win.update_idletasks()
        _no_activate(win)
        self._raise_map(win)
        self._hold_map_on_top(win)

    @staticmethod
    def _hold_map_on_top(win):
        """Re-raise the map window every MAP_TOP_MS until it closes.

        There is no z-order above topmost, only the most recent claim on it,
        and a chat box that redraws on each message takes it back.
        """
        if not win.winfo_exists():
            return                     # closed; stop the timer with it
        _win32_raise(win)
        win.after(MAP_TOP_MS, lambda: Overlay._hold_map_on_top(win))

    @staticmethod
    def _raise_map(win):
        """Put the map window above the other topmost windows, without taking focus.

        Dropping and re-setting -topmost re-raises inside the band, which
        lift() alone does not. Focus is never taken: with the game fullscreen
        that minimised it and it did not come back. So Escape only works when
        the window happens to hold focus; the x button is the reliable way out.
        """
        if not win.winfo_exists():
            return
        try:
            win.update_idletasks()
            win.lift()
            win.attributes("-topmost", False)
            win.attributes("-topmost", True)
            _win32_raise(win)
        except tk.TclError:
            pass                       # closed underneath us; nothing to raise

    def _close_map(self):
        """Close the map window and clear the reference. Also called when a battle starts."""
        win, self._map_win = self._map_win, None
        try:
            if win is not None and win.winfo_exists():
                win.destroy()
        except tk.TclError:
            pass

    def _my_seat(self):
        """(alliance, position) for the local player, zero-based, or None."""
        if not self.me:
            return None
        return self.seats.get(str(self.me))

    def _battle_shape(self):
        """The lobby as "AvB", or None if the seats could not be read."""
        if not self.seats:
            return None
        n = [0, 0]
        for team, _pos in self.seats.values():
            if team in (0, 1):
                n[team] += 1
        if not n[0] or not n[1]:
            return None
        return f"{n[0]}v{n[1]}"

    def _map_png(self, key, px, view=""):
        """Path to the map picture in MAP_CACHE_DIR, downloading it from the server if needed.

        GET /v1/maps/<key>/preview.png with the lobby shape and the viewer's
        seat, so the server draws the boxes this battle uses and marks ours.
        view picks the picture: "" the photograph with deployment boxes,
        "relief" coloured by height, "speed" by infantry movement cost. Each
        has its own cache entry, and the cache ages out at MAP_CACHE_MAX_AGE
        so a corrected drawing reaches every client.
        """
        os.makedirs(MAP_CACHE_DIR, exist_ok=True)
        # A display name carries spaces and brackets; squashed for the
        # filename only.
        safe = "".join(c if c.isalnum() else "_" for c in key)

        # The shape and the marked seat are part of the picture, so they are
        # part of its name; a six-box picture must not serve an eight-player
        # lobby for a day.
        shape = self._battle_shape()
        mine = self._my_seat()
        tag = f"_{shape}" if shape else ""
        tag += f"_{mine[0]}{mine[1]}" if mine else ""
        if view:
            tag += "_" + view
        path = os.path.join(MAP_CACHE_DIR, f"{safe}_{px}{tag}.png")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            if time.time() - os.path.getmtime(path) < MAP_CACHE_MAX_AGE:
                return path
        # The server takes either a preset key or the name the lobby displays;
        # the client cannot turn one into the other.
        seg = urllib.parse.quote(key, safe="")
        url = f"{self.server}/v1/maps/{seg}/preview.png?px={px}"
        if view:
            # No deployment boxes on these: they are pictures of the ground.
            url += "&view=" + view
        # The shape, so the picture shows the boxes this battle uses; some
        # maps re-space their line as the battle grows.
        if shape:
            url += f"&seats={shape}"
        # Which box is ours, marked, because the seat number is an identifier
        # and not a position along the line.
        if mine:
            url += f"&you={mine[0]}.{mine[1]}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
        except urllib.error.HTTPError as exc:
            # Name the map and the server: a map with no terrain and a server
            # without the endpoint want opposite responses.
            if exc.code == 404:
                raise RuntimeError(
                    f"{map_label(key)} is not available from {self.server}"
                ) from exc
            raise RuntimeError(f"{self.server} said {exc.code}") from exc
        # Via a temporary name, so an interrupted download cannot leave a
        # truncated PNG in the cache.
        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        return path

    def _update_now(self):
        """The version link: update in place (selfupdate) from the built exe, else open the site.

        One update window at a time.
        """
        try:
            import selfupdate
        except Exception:                  # noqa: BLE001
            selfupdate = None
        if selfupdate is None or not selfupdate.can_self_update():
            self._open_site()
            return
        win = getattr(self, "_update_win", None)
        if win is not None:
            try:
                if win.winfo_exists():
                    return
            except tk.TclError:
                pass
        # Shut down the way the close button does, so the server hears the
        # session end and the process is gone before the new one takes over.
        self._update_win = selfupdate.run_gui(
            self.root, on_restart=lambda: self._shutdown(notify=True))

    def _open_site(self):
        """Open SITE_URL in the player's browser. A failed launch must not take the panel down."""
        try:
            webbrowser.open_new_tab(SITE_URL)
        except Exception:
            pass

    def _notify_closed(self):
        """POST /v1/gameClosed: the game has gone. True if the server acknowledged.

        The body is only {"event": "game_closed"} plus the Steam key header.
        The result decides whether the panel may close.
        """
        try:
            req = urllib.request.Request(
                f"{self.server}/v1/gameClosed",
                data=json.dumps({"event": "game_closed"}).encode("utf-8"),
                # The key, like every other POST; without it the server
                # answers 401.
                headers={"Content-Type": "application/json",
                         "User-Agent": USER_AGENT,
                         **steamauth.headers()})
            with urllib.request.urlopen(req, timeout=3) as r:
                code = getattr(r, "status", None) or r.getcode()
                return 200 <= code < 300
        except Exception:
            return False  # the server may be down, or not have the endpoint yet

    def _report_and_close(self):
        """Report the game's exit and stand down once that lands, or after CLOSE_REPORT_TRIES.

        Returns True when the process is on its way out. A report that did
        not land leaves the panel up so the next check retries it.
        """
        self._closing = True
        if self._notify_closed():
            self._shutdown(notify=False)
            return True
        self._close_tries += 1
        if self._close_tries >= CLOSE_REPORT_TRIES:
            self._shutdown(notify=False)
            return True
        self.draw()               # show the attempt count while it retries
        return False

    # ---- data --------------------------------------------------------------

    def _seat_players(self, rows, seats):
        """Order the roster by alliance then seat if the seats are known, else by rating.

        With no seats, no seat numbers are drawn either: a number invented
        from list order would look exactly like a real one.
        """
        placed = {n: tuple(v) for n, v in seats.items()}
        if placed and all(str(p.get("name", "")) in placed for p in rows):
            self.players = sorted(rows,
                                  key=lambda p: placed[str(p["name"])])
            self.seats = placed
        else:
            self.players = sorted(
                rows, key=lambda p: (-(p.get("rating") or 0),
                                     str(p.get("name", "")).lower()))
            self.seats = {}

    def _refresh_map_if_moved(self):
        """Reopen the map window when the lobby shape or the viewer's seat has changed."""
        win = self._map_win
        if win is None or not win.winfo_exists():
            return
        now = (self.map_key, self._battle_shape(), self._my_seat())
        if now == getattr(self, "_map_drawn_for", None):
            return
        self._close_map()
        self._show_map()

    def _drain(self):
        """Apply queued messages from the Poller and MapWatcher, then redraw. Every 500 ms."""
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "ok":
                    # A fresh answer from the server.
                    self.me = payload.get("me") or self.me
                    self._seat_players(payload.get("players", []),
                                       payload.get("seats") or {})
                    self.ai = payload.get("ai", [])
                    self.scope = payload.get("scope", "global")
                    self.status = "connected"
                    self.last_ok = time.time()
                elif kind == "fault":
                    self.lobby_fault = payload
                elif kind == "seats":
                    # A swap with no new ratings: reorder and move the map
                    # mark, and leave scope and AI list alone.
                    self.me = payload.get("me") or self.me
                    self._seat_players(self.players,
                                       payload.get("seats") or {})
                    self._refresh_map_if_moved()
                elif kind == "note":
                    # A log line from the poller thread, not a status.
                    print(payload)
                    continue
                elif kind == "map":
                    self.map_key = payload
                elif kind == "state":
                    self._on_state(payload)
                elif kind == "results":
                    # The quick check found the Save Replay button. It only
                    # runs during a battle, so no state guard is needed.
                    self._on_results_screen()
                else:
                    self.status = "offline"
                self.draw()
        except queue.Empty:
            pass
        self.root.after(500, self._drain)

    # ---- drawing -----------------------------------------------------------

    def draw(self):
        """Redraw the whole canvas."""
        self._draw_panel()
        # The menu last, over everything, whichever early return _draw_panel
        # took.
        if self._menu_open and not self.collapsed:
            self._draw_menu(self.canvas, self.w, int(30 * self.s), self.s)

    def _draw_panel(self):
        """Draw the frame, title bar, roster, map line and status line."""
        if self.collapsed:
            self._draw_tab()
            return
        c = self.canvas
        c.delete("all")
        self._tip_hide()
        self._tip_regions = []
        s = self.s
        w, h = self.w, self.h
        pad = int(6 * s)
        hdr = int(30 * s)

        c.create_rectangle(0, 0, w, h, fill=EDGE, outline="")
        c.create_rectangle(2, 2, w - 3, h - 3, fill=GOLD, outline="")
        c.create_rectangle(int(4 * s), int(4 * s), w - int(4 * s) - 1,
                           h - int(4 * s) - 1, fill=BODY, outline="")
        c.create_rectangle(int(4 * s), int(4 * s), w - int(4 * s) - 1, hdr,
                           fill=HEADER, outline="")
        c.create_line(int(4 * s), hdr, w - int(4 * s) - 1, hdr, fill=GOLD)

        title = "NTW3 Ladder"
        c.create_text(w // 2, hdr // 2 + int(2 * s), text=title,
                      fill=TITLE, font=self.f_title)
        self._draw_version(c, w, hdr, s, title)

        cx = w - int(16 * s)
        cy = hdr // 2 + int(2 * s)
        r = int(5 * s)
        # The title bar's buttons, laid out from the right by a loop, because
        # the quit button comes and goes.
        buttons = []

        # A quit button only until a game has been seen: a topmost window
        # with no way out must not be left on a screen where the game never
        # started. Once a game is running the panel leaves when the game
        # does, and quitting by accident would stop the replay uploads.
        if not self._game_seen:
            buttons.append(("×", self.quit, LOSS))

        # Minimise.
        buttons.append(("–", lambda: self.toggle(True), TITLE))

        # The account menu, drawn on this canvas (no window, no focus change):
        # where the Steam connection is made and broken.
        buttons.append((USER, self._toggle_menu, TITLE))

        for i, (glyph, action, hot) in enumerate(buttons):
            bx = cx - i * int(19 * s)
            rest = TITLE if glyph == "–" else DIM
            tag = f"btn{i}"
            # Filled with the bar's colour so the whole circle takes the click,
            # not just the strokes of what is drawn in it.
            hit = c.create_oval(bx - r - 2, cy - r - 2, bx + r + 2, cy + r + 2,
                                fill=HEADER, outline="")
            if glyph == USER:
                marks = self._draw_user_icon(
                    c, bx, cy, r, rest, tag,
                    filled=bool(steamauth.load().get("key")))
            else:
                marks = [c.create_text(bx, cy, text=glyph, fill=rest,
                                       font=self.f_small, tags=tag)]

            def paint(col, t=tag):
                # Filled marks recolour their fill, hollow ones their stroke.
                c.itemconfig(t, fill=col)
                c.itemconfig(t + "o", outline=col)

            for it in [hit] + marks:
                c.tag_bind(it, "<Button-1>", lambda e, f=action: f())
                c.tag_bind(it, "<Enter>", lambda e, p=paint, col=hot: p(col))
                c.tag_bind(it, "<Leave>", lambda e, p=paint, col=rest: p(col))
            c.create_oval(bx - r, cy - r, bx + r, cy + r, outline=DIM)

        if not self.demo and not signed_in():
            self._draw_locked(c, w, h, hdr, s)
            return

        y = hdr + int(10 * s)
        left = int(14 * s)
        right = w - int(14 * s)


        c.create_text(left, y, anchor="w", text="PLAYER",
                      fill=DIM, font=self.f_small)
        c.create_text(right - int(112 * s), y, anchor="e", text="RATING",
                      fill=DIM, font=self.f_small)
        c.create_text(right - int(52 * s), y, anchor="e", text="W-L-D",
                      fill=DIM, font=self.f_small)
        # Percentage of battles this player was still commanding at the end.
        c.create_text(right, y, anchor="e", text="FIN",
                      fill=DIM, font=self.f_small)
        y += int(6 * s)
        c.create_line(left, y, right, y, fill="#6B3A20")
        y += int(12 * s)

        row_h = int(23 * s)
        # Exactly SEATS rows, so a full lobby is never silently clipped.
        max_rows = min(SEATS, max(1, (h - y - int(30 * s)) // row_h))

        # Where the two alliances part, if the seats were readable: one side,
        # a gap, the other side, as the lobby shows it.
        gap_at = None
        if self.seats and self.players:
            first = self.seats.get(str(self.players[0].get("name")),
                                   (0, 0))[0]
            gap_at = next(
                (i for i, p in enumerate(self.players)
                 if self.seats.get(str(p.get("name")), (first, 0))[0] != first),
                None)

        # Every row's baseline. The gap is a whole row, so the stripes below
        # it keep their rhythm.
        rows_y, yy = [], y
        for k in range(SEATS + (1 if gap_at is not None else 0)):
            rows_y.append(yy)
            yy += row_h
        if gap_at is not None:
            rows_y = rows_y[:gap_at] + rows_y[gap_at + 1:]

        # Stripe the whole field, as the game's seat lists do.
        for k, by in enumerate(rows_y):
            c.create_rectangle(int(6 * s), by - int(9 * s), w - int(6 * s),
                               by + int(9 * s),
                               fill=(ROW_A if k % 2 == 0 else ROW_B),
                               outline="")

        if not self.players and not self.ai:
            c.create_text(w // 2, y + int(30 * s),
                          text="waiting for the server..." if not self.demo else "no players yet",
                          fill=DIM, font=self.f_small)
        for i, p in enumerate(self.players[:max_rows]):
            y = rows_y[i] if i < len(rows_y) else y + row_h
            rank = p.get("rank", i + 1)
            name = str(p.get("name", "?"))
            if len(name) > 18:
                name = name[:17] + "…"
            wins, losses = p.get("wins", 0), p.get("losses", 0)
            draws = p.get("draws", 0)

            # The seat number, where this player deploys. Only when read.
            nx = left
            seat = self.seats.get(str(p.get("name")))
            if seat is not None:
                c.create_text(left, y, anchor="w", text=f"{seat[1] + 1}",
                              fill=DIM, font=self.f_row)
                nx = left + int(14 * s)

            # The red coat: this player runs Redcoatz, filled when every crash
            # fix is on, an outline when not (see redcoatzBadge on the
            # server). A skull outranks it: the server caught this player
            # fielding an army far over the lobby's gold limit (overbudget.go).
            badge = p.get("redcoatz")
            band = ROW_A if i % 2 == 0 else ROW_B
            if p.get("skull"):
                self._tip_region(c, nx + int(7 * s), y, s, band,
                                 lambda: self._draw_skull(c, nx + int(7 * s), y, s),
                                 skull_text(p["skull"]))
                nx += int(21 * s)
            elif badge:
                self._tip_region(c, nx + int(7 * s), y, s, band,
                                 lambda: self._draw_coat(c, nx + int(7 * s), y, s,
                                                         bool(badge.get("protected"))),
                                 coat_text(badge))
                nx += int(21 * s)

            label = shown_name(name)
            c.create_text(nx, y, anchor="w", text=label,
                          fill=TEXT if label == name else DIM, font=self.f_row)
            # The host, with the lamp for how their lobbies tend to end.
            host = p.get("host")
            if isinstance(host, dict) and host.get("mark"):
                lx = nx + self.f_row.measure(label) + int(10 * s)

                def lamp(lx=lx, y=y, mark=host["mark"]):
                    self._draw_host_lamp(c, lx, y, s, mark)
                    c.create_text(lx + int(11 * s), y, anchor="w", text="host",
                                  fill=DIM, font=self.f_small)
                self._tip_region(c, lx + int(16 * s), y, s, band, lamp,
                                 host_text(host), half_w=22)
            c.create_text(right - int(112 * s), y, anchor="e",
                          text=str(p.get("rating", "-")), fill=GOLD, font=self.f_row)
            rec = f"{wins}-{losses}-{draws}"
            col = WIN if wins > losses else (LOSS if losses > wins else DIM)
            # A player the server has never scored gets no fake record.
            new = not p.get("known", True) and p.get("games", 0) == 0
            if new:
                rec, col = "new", DIM
            c.create_text(right - int(52 * s), y, anchor="e", text=rec,
                          fill=col, font=self.f_row)

            # Battles seen through to the end, as a percentage. A dash with no
            # games, and no colour until there are five: one early leave is
            # not a pattern.
            games = p.get("games", 0)
            if new or not games:
                fin, fcol = "-", DIM
            else:
                done = p.get("finished", 0)
                fin = f"{round(100 * done / games)}%"
                fcol = DIM
                if games >= 5:
                    fcol = WIN if done >= 0.9 * games else LOSS
            if FIN_STYLE == "pct" or fin == "-":
                c.create_text(right, y, anchor="e", text=fin, fill=fcol,
                              font=self.f_row)
            else:
                self._draw_fin(c, right, y, s, p.get("finished", 0), games,
                               p.get("recent"))

        # Carry on below the last row drawn, whether or not a gap was inserted.
        y = (rows_y[min(len(self.players[:max_rows]), len(rows_y) - 1)]
             if rows_y else y)
        y += row_h

        # AI seats last, dimmed and unranked.
        for name in self.ai[:max(0, max_rows - len(self.players))]:
            c.create_text(left, y, anchor="w", text=name,
                          fill=DIM, font=self.f_row)
            c.create_text(right, y, anchor="e", text="AI", fill=DIM,
                          font=self.f_row)
            y += row_h

        # The lobby's map, in the reserved ninth row, with a link to view it.

        if self.map_key:
            my = y + int(4 * s)
            c.create_text(left, my, anchor="w", text=map_label(self.map_key),
                          fill=DIM, font=self.f_small)
            item = c.create_text(right, my, anchor="e", text="view map",
                                 fill=LINK, font=self.f_small)
            bb = c.bbox(item)
            if bb:
                c.create_line(bb[0], bb[3] - int(2 * s), bb[2],
                              bb[3] - int(2 * s), fill=LINK)

            def open_map(_e):
                self._show_map()
                # Stop the click reaching the canvas, whose double-click
                # binding would collapse the panel out from under the cursor.
                return "break"

            c.tag_bind(item, "<Button-1>", open_map)
            c.tag_bind(item, "<Enter>", lambda e: c.config(cursor="hand2"))
            c.tag_bind(item, "<Leave>", lambda e: c.config(cursor=""))

        # Status line, in priority order. A player must never mistake old
        # standings for live ones.
        c.create_line(int(10 * s), h - int(24 * s), w - int(10 * s),
                      h - int(24 * s), fill="#6B3A20")
        if self._closing:
            # Why the panel is still here after the game went.
            self._draw_status_link(
                c, w, h, s,
                f"GAME CLOSED - reporting "
                f"({self._close_tries}/{CLOSE_REPORT_TRIES})", LOSS)
            return
        # The one message worth interrupting for: a battle nobody saved is a
        # battle the ladder never sees, and this screen is the last chance.
        if self._asked_to_save and self.game_state == STATE_BATTLE:
            if self._saved_seen:
                msg, col = "REPLAY SAVED  -  uploading", WIN
            else:
                msg, col = ("SAVE THE REPLAY  -  or this battle will not count",
                            LOSS)
            c.create_text(w // 2, h - int(13 * s), text=msg, fill=col,
                          font=self.f_small)
            return
        if self._hiding_at:
            left = max(0, int(round(self._hiding_at - time.time())))
            c.create_text(w // 2, h - int(13 * s),
                          text=f"GAME STARTED - minimising to ★ in {left}s",
                          fill=WIN, font=self.f_small)
            return
        if self.game_running is False:
            # No game outranks every other status; the one useful thing left
            # to offer is the site. Demo keeps its label.
            self._draw_status_link(
                c, w, h, s,
                "DEMO - NO LIVE GAME" if self.demo else "NO LIVE GAME DETECTED",
                LOSS)
            return
        if not self.demo and not steamauth.headers():
            # The server records nothing from a client without a key, so a
            # signed-out player is told their battles are not counted.
            msg, col = "NOT SIGNED IN TO STEAM - battles not reported, see ⚙", LOSS
        elif self.status == "connected" and self.last_ok:
            age = int(time.time() - self.last_ok)
            when = f"{age}s ago" if age > 2 else "just now"
            # Which list this is: the top of the ladder and the lobby look
            # identical otherwise.
            where = "IN LOBBY" if self.scope == "lobby" else "TOP RANKED"
            msg = f"{where} - {when}"
            col = DIM
        elif self.demo:
            msg, col = "DEMO DATA - not connected to a server", DIM
        else:
            msg, col = f"offline - showing last known ({self.status})", LOSS
        c.create_text(w // 2, h - int(13 * s), text=msg, fill=col, font=self.f_small)

    # ---- the settings menu ------------------------------------------------

    def _draw_user_icon(self, c, x, y, r, colour, tag, filled=True):
        """The account silhouette inside a button of radius r: filled when signed in, an outline when not.

        Returns the canvas items. Filled ones carry `tag` and outline ones
        `tag + "o"`, so a hover knows which property to recolour.
        """
        k = r / 10.0
        if filled:
            look = dict(fill=colour, outline="", tags=tag)
        else:
            look = dict(fill="", outline=colour, width=max(1, round(1.3 * k)),
                        tags=tag + "o")
        edge = [(8.6 * math.cos(math.radians(d)), 8.6 * math.sin(math.radians(d)))
                for d in range(35, 146, 10)]
        shoulders = [(-7.0, 4.9), (-6.0, 2.6), (-3.4, 1.3), (3.4, 1.3),
                     (6.0, 2.6), (7.0, 4.9)] + edge
        return [
            c.create_polygon(*[v for px_, py_ in shoulders
                               for v in (x + px_ * k, y + py_ * k)],
                             smooth=True, **look),
            c.create_oval(x - 3.6 * k, y - 7.0 * k, x + 3.6 * k, y + 0.2 * k,
                          **look),
        ]

    def _toggle_menu(self):
        """Open or close the account menu."""
        self._menu_consumed = True
        self._menu_open = not self._menu_open
        self._menu_confirm = False
        self.draw()

    def _menu_items(self):
        """(label, action or None, colour) per menu row. None is not clickable."""
        items = []
        if self._menu_busy:
            items.append((self._menu_busy, None, DIM))
        elif steamauth.load().get("key"):
            if self._menu_confirm:
                items.append(("Disconnect?  Click again to confirm", self._disconnect, LOSS))
            else:
                items.append(("Disconnect Steam account", self._disconnect, TEXT))
        else:
            items.append(("Sign in through Steam", self._connect, TEXT))
        return items

    def _draw_menu(self, c, w, hdr, s):
        """The account menu, drawn on the canvas so clicks never take focus from the game.

        Redrawn with everything else, so its state lives on self.
        """
        items = self._menu_items()
        doc = steamauth.load()
        # The player's name if known for this key, the Steam ID otherwise.
        who = (doc.get("name") or doc.get("steam_id")) if doc.get("key") else ""
        head = f"Steam: connected as {who}" if who else "Steam: not connected"
        f = self.f_small
        row_h = int(21 * s)
        pad = int(10 * s)
        width = max(f.measure(t) for t, _a, _c in items + [(head, None, None)]) + 2 * pad
        right = w - int(6 * s)
        left = right - width
        top = hdr + int(2 * s)
        bottom = top + row_h * (len(items) + 1) + int(6 * s)
        c.create_rectangle(left, top, right, bottom, fill=GOLD, outline="")
        c.create_rectangle(left + 1, top + 1, right - 1, bottom - 1, fill=BODY, outline="")
        y = top + int(3 * s) + row_h // 2
        c.create_text(left + pad, y, anchor="w", text=head, fill=DIM, font=f)
        y += row_h
        c.create_line(left + pad, y - row_h // 2, right - pad, y - row_h // 2, fill=HOST_RING)
        for label, action, col in items:
            item = c.create_text(left + pad, y, anchor="w", text=label, fill=col, font=f)
            if action is not None:
                band = c.create_rectangle(left + 1, y - row_h // 2, right - 1,
                                          y + row_h // 2, fill=BODY, outline="")
                c.tag_lower(band, item)
                for it in (item, band):
                    c.tag_bind(it, "<Button-1>", lambda e, a=action: self._menu_click(a))
                    c.tag_bind(it, "<Enter>", lambda e, t=item, b=band:
                               (c.itemconfig(t, fill=LINK), c.itemconfig(b, fill=ROW_A)))
                    c.tag_bind(it, "<Leave>", lambda e, t=item, b=band, k=col:
                               (c.itemconfig(t, fill=k), c.itemconfig(b, fill=BODY)))
            y += row_h

    def _menu_click(self, action):
        """Run a menu item's action and mark the click as handled."""
        self._menu_consumed = True
        action()

    def _disconnect(self):
        """Sign out of Steam, on the second click.

        steamauth.sign_out revokes the key on the server and deletes it here,
        on a thread so the panel never stalls.
        """
        if not self._menu_confirm:
            self._menu_confirm = True
            self.draw()
            return
        self._menu_confirm = False
        self._menu_busy = "Disconnecting…"
        self.draw()

        def go():
            try:
                steamauth.sign_out(self.server, log=print)
            except Exception as e:           # noqa: BLE001
                print(f"steam sign-out failed: {e!r}")
            self._menu_busy = ""
            self.root.after(0, self.draw)
        threading.Thread(target=go, daemon=True).start()

    def _connect(self):
        """Sign in to Steam from the menu: steamauth.sign_in opens the browser, a thread waits for the key.

        No wait window; the menu row says what is happening.
        """
        if self._signin:
            return
        self._menu_busy = "Signing in… finish in your browser"
        self._signin = ("", None)
        self.draw()

        def started(url, cancel):
            self._signin = (url, cancel)
            self.root.after(0, self.draw)

        def go():
            try:
                steamauth.sign_in(self.server, log=print, on_start=started)
            except Exception as e:           # noqa: BLE001
                print(f"steam sign-in failed: {e!r}")
            self._menu_busy = ""
            self._signin = None
            self.root.after(0, self.draw)
        threading.Thread(target=go, daemon=True).start()

    def _draw_version(self, c, w, hdr, s, title):
        """The version in the title bar's left corner; a link to update when the server has a newer build.

        The corner shares the line with a centred title, so each form is
        measured longest-first and the first that fits is drawn. Not a
        dialog: an optional update must not stand between a player and the
        game.
        """
        y = hdr // 2 + int(2 * s)
        x = int(10 * s)
        # Half the title, plus a gap, is what the centre claims.
        room = (w // 2) - (self.f_title.measure(title) // 2) - x - int(8 * s)

        if self.update_to:
            options = (f"v{VERSION} → v{self.update_to}",
                       f"v{VERSION} ↑", f"v{VERSION}")
        else:
            options = (f"v{VERSION}",)

        label = next((o for o in options
                      if self.f_small.measure(o) <= room), None)
        if label is None:
            # Not even the bare version fits; say nothing.
            return

        if not self.update_to:
            c.create_text(x, y, anchor="w", text=label, fill=DIM,
                          font=self.f_small)
            return

        item = c.create_text(x, y, anchor="w", text=label, fill=LINK,
                             font=self.f_small)
        bb = c.bbox(item)
        if bb:
            c.create_line(bb[0], bb[3] - int(2 * s), bb[2], bb[3] - int(2 * s),
                          fill=LINK)

        def click(_e):
            self._update_now()
            # Stop the click reaching the canvas's double-click and drag
            # bindings.
            return "break"

        c.tag_bind(item, "<Button-1>", click)
        c.tag_bind(item, "<Enter>", lambda e: c.config(cursor="hand2"))
        c.tag_bind(item, "<Leave>", lambda e: c.config(cursor=""))

    def _draw_locked(self, c, w, h, hdr, s):
        """The body while signed out: no standings, no lobby, one way in.

        The whole panel stands down, so a signed-out client never looks as if
        it were reporting a rated battle.
        """
        if self._signin:
            url, cancel = self._signin

            def reopen():
                if url:
                    webbrowser.open(url)

            def give_up():
                if cancel is not None:
                    cancel.set()
            _draw_signin_body(c, w, h, hdr, s, self.f_small, waiting=True,
                              reopen=reopen, cancel=give_up)
        else:
            _draw_signin_body(c, w, h, hdr, s, self.f_small, waiting=False,
                              start=self._connect)

    def _draw_status_link(self, c, w, h, s, msg, col):
        """Status text followed by the site link, measured and centred as one unit."""
        sep = "  -  "
        tw = self.f_small.measure(msg + sep)
        lw = self.f_small.measure(SITE_LABEL)
        x0 = max(int(6 * s), (w - (tw + lw)) // 2)
        y = h - int(13 * s)

        c.create_text(x0, y, anchor="w", text=msg + sep, fill=col,
                      font=self.f_small)
        item = c.create_text(x0 + tw, y, anchor="w", text=SITE_LABEL,
                             fill=LINK, font=self.f_small)
        # Underlined: colour alone does not read as clickable beside gold
        # ratings.
        bb = c.bbox(item)
        if bb:
            c.create_line(bb[0], bb[3] - int(2 * s), bb[2], bb[3] - int(2 * s),
                          fill=LINK)

        def click(_e):
            self._open_site()
            # Stop the click reaching the canvas's double-click and drag
            # bindings.
            return "break"

        c.tag_bind(item, "<Button-1>", click)
        c.tag_bind(item, "<Enter>", lambda e: c.config(cursor="hand2"))
        c.tag_bind(item, "<Leave>", lambda e: c.config(cursor=""))

    def _draw_skull(self, c, cx, cy, s):
        """A skull about 14px tall centred on (cx, cy), the size of the coat."""
        c.create_oval(cx - 6 * s, cy - 7 * s, cx + 6 * s, cy + 3 * s,
                      fill=SKULL, outline="")
        c.create_rectangle(cx - 3.5 * s, cy + 1 * s, cx + 3.5 * s, cy + 6 * s,
                           fill=SKULL, outline="")
        for ex in (-2.6, 2.6):
            c.create_oval(cx + (ex - 1.8) * s, cy - 3.5 * s,
                          cx + (ex + 1.8) * s, cy + 0.2 * s,
                          fill=SKULL_HOLE, outline="")
        c.create_polygon(cx, cy + 0.8 * s, cx - 1 * s, cy + 2.6 * s,
                         cx + 1 * s, cy + 2.6 * s, fill=SKULL_HOLE, outline="")
        for tx in (-1.8, 0, 1.8):
            c.create_line(cx + tx * s, cy + 4 * s, cx + tx * s, cy + 6 * s,
                          fill=SKULL_HOLE, width=max(1, 0.6 * s))

    def _tip_region(self, c, cx, cy, s, band, draw, text, half_w=9):
        """Draw an icon with hover text.

        A rectangle in the row's band colour goes under the icon so the
        pointer counts anywhere in its box, not only on its lines. The region
        is recorded for anything that opens a tip without a mouse.
        """
        x0, y0 = cx - int(half_w * s), cy - int(9 * s)
        x1, y1 = cx + int(half_w * s), cy + int(9 * s)
        before = c.find_all()
        last = before[-1] if before else 0
        c.create_rectangle(x0, y0, x1, y1, fill=band, outline="")
        draw()
        for item in c.find_all():
            if item > last:
                c.tag_bind(item, "<Enter>", lambda e, t=text: self._tip_soon(t, e))
                c.tag_bind(item, "<Leave>", lambda e: self._tip_hide())
        self._tip_regions.append((x0, y0, x1, y1, text))

    def _tip_soon(self, text, event):
        """Open the hover text after a short pause."""
        self._tip_hide()
        self._tip_after = self.root.after(
            350, lambda: self._tip_show(text, event.x, event.y + int(14 * self.s)))

    def _tip_show(self, text, x, y):
        """A small framed box at canvas (x, y), topmost like the panel and never taking focus."""
        self._tip_hide()
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=GOLD)
        lab = tk.Label(win, text=text, justify="left", bg=BODY, fg=TEXT,
                       font=self.f_small, padx=int(8 * self.s), pady=int(5 * self.s))
        lab.pack(padx=1, pady=1)
        win.update_idletasks()
        sx = self.root.winfo_rootx() + x
        sy = self.root.winfo_rooty() + y
        # Inside the panel's width where it fits, so it reads as part of
        # the panel and not as a stray window over the game; on screen always.
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        panel_right = self.root.winfo_rootx() + self.root.winfo_width()
        sx = min(sx, panel_right - win.winfo_reqwidth() - int(6 * self.s))
        sx = max(0, min(sx, sw - win.winfo_reqwidth()))
        sy = max(0, min(sy, sh - win.winfo_reqheight()))
        win.geometry(f"+{sx}+{sy}")
        win.lift()
        self._tip = win

    def _tip_hide(self):
        """Cancel a pending tip and close an open one."""
        if self._tip_after is not None:
            try:
                self.root.after_cancel(self._tip_after)
            except Exception:                   # noqa: BLE001
                pass
            self._tip_after = None
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:                   # noqa: BLE001
                pass
            self._tip = None

    def _draw_fin(self, c, right, cy, s, done, games, recent):
        """The FIN column in one of the prototype styles. See FIN_STYLE."""
        share = done / games if games else 0
        enough = games >= 5
        col = DIM if not enough else (WIN if share >= 0.9 else
                                      FIN_AMBER if share >= 0.75 else LOSS)
        if FIN_STYLE == "dot":
            r = 4.5 * s
            cx = right - int(8 * s)
            if enough:
                c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=col,
                              outline=HOST_RING, width=max(1, s))
            else:
                c.create_oval(cx - r, cy - r, cx + r, cy + r, fill="",
                              outline=DIM, width=max(1, s))
        elif FIN_STYLE == "bars":
            lit = 4 if share >= 0.9 else 3 if share >= 0.8 else 2 if share >= 0.7 else 1
            if not enough:
                lit = 0
            bw, gap = 3 * s, 2 * s
            x = right - 4 * (bw + gap)
            for k in range(4):
                hgt = (4 + 3 * k) * s
                x0, x1 = x + k * (bw + gap), x + k * (bw + gap) + bw
                if k < lit:
                    c.create_rectangle(x0, cy + 6 * s - hgt, x1, cy + 6 * s,
                                       fill=col, outline="")
                else:
                    c.create_rectangle(x0, cy + 6 * s - hgt, x1, cy + 6 * s,
                                       fill="", outline=DIM, width=max(1, 0.7 * s))
        elif FIN_STYLE == "meter":
            w_, h_ = 30 * s, 6 * s
            x0, y0 = right - w_, cy - h_ / 2
            c.create_rectangle(x0, y0, right, y0 + h_, fill="", outline=DIM,
                               width=max(1, 0.8 * s))
            if games:
                c.create_rectangle(x0, y0, x0 + w_ * share, y0 + h_,
                                   fill=col if enough else DIM, outline="")
        elif FIN_STYLE == "pips":
            r = 2.6 * s
            step = 7 * s
            hist = list(recent or [])[-5:]
            if not hist and games:
                # Nothing recent known: the share, spread over five pips.
                n = round(5 * share)
                hist = [True] * n + [False] * (5 - n)
            x = right - 5 * step + step / 2
            for k, ok in enumerate(hist):
                cx = x + k * step
                if ok:
                    c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=col,
                                  outline="")
                else:
                    c.create_oval(cx - r, cy - r, cx + r, cy + r, fill="",
                                  outline=LOSS, width=max(1, 0.8 * s))

    def _draw_host_lamp(self, c, cx, cy, s, mark):
        """The host lamp, about 9px across at (cx, cy): green, red or black by `mark`. See HOST_GOOD."""
        col = {"good": HOST_GOOD, "bad": HOST_BAD, "worst": HOST_WORST}.get(mark)
        if not col:
            return
        r = 4.5 * s
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=col,
                      outline=COAT_BUTTON if mark == "worst" else HOST_RING,
                      width=max(1, s))
        if mark != "worst":
            hr = max(1, 1.3 * s)
            c.create_oval(cx - 1.6 * s - hr, cy - 1.6 * s - hr,
                          cx - 1.6 * s + hr, cy - 1.6 * s + hr,
                          fill="#FFFBF4", outline="")

    def _draw_coat(self, c, cx, cy, s, full):
        """A red coat about 14px tall at (cx, cy): filled with gold buttons when `full`, an outline otherwise."""
        pts = [(-5, -6), (-2, -7), (0, -5), (2, -7), (5, -6), (7, -2), (5, -1),
               (4, 7), (-4, 7), (-5, -1), (-7, -2)]
        flat = []
        for x, yy in pts:
            flat += [cx + x * s, cy + yy * s]
        if full:
            c.create_polygon(flat, fill=COAT, outline=COAT_BUTTON,
                             width=max(1, s))
            r = max(1, s)
            for by in (-2, 1, 4):
                c.create_oval(cx - r, cy + by * s - r, cx + r, cy + by * s + r,
                              fill=COAT_BUTTON, outline="")
        else:
            c.create_polygon(flat, fill="", outline=COAT_LINE,
                             width=max(1.5, 1.5 * s))

    def _draw_tab(self):
        """The collapsed badge: a framed star."""
        c = self.canvas
        c.delete("all")
        s = self.s
        w, h = self.tab_w, self.tab_h
        c.create_rectangle(0, 0, w, h, fill=EDGE, outline="")
        c.create_rectangle(1, 1, w - 2, h - 2, fill=GOLD, outline="")
        c.create_rectangle(int(3 * s), int(3 * s), w - int(3 * s) - 1,
                           h - int(3 * s) - 1, fill=HEADER, outline="")
        star = tkfont.Font(family=self._pick_font(TITLE_FONTS),
                           size=max(11, int(15 * s)))
        c.create_text(w // 2, h // 2, text="★", fill=GOLD, font=star)


def skull_text(skull):
    """What the skull means, from the server's badge: battles and worst army."""
    lines = ["Confirmed cheater."]
    n = skull.get("battles") or 0
    gold = skull.get("max_gold") or skull.get("gold") or 0
    limit = skull.get("limit") or 10000
    if gold:
        lines.append(f"Fielded {gold:,} gold in a {limit:,} lobby"
                     + (f", in {n} battles." if n > 1 else "."))
    return "\n".join(lines)


def coat_text(badge):
    """What the coat means: running Redcoatz, fixes on or not."""
    if badge.get("protected"):
        return "Running updated Redcoatz"
    # Hollow is mostly an old client that never reports its crash fixes; a
    # newer one with a fix declined reads the same here.
    return "Running an older version of Redcoatz"


def host_text(host):
    """The host's whole record, as the lamp summarises it."""
    hosted = host.get("hosted") or 0
    fin = host.get("finished") or 0
    pct = f" ({round(100 * fin / hosted)}%)" if hosted else ""
    head = {"good": "Reliable host: battles reach the end.",
            "bad": "Unreliable host: lobbies often fall apart.",
            "worst": "Has left their own battles."}.get(host.get("mark"), "Host record")
    lines = [head,
             f"{hosted} games hosted",
             f"{fin} reached the save screen{pct}",
             f"{host.get('replays') or 0} replays uploaded",
             f"{host.get('host_left') or 0} left by the host",
             f"{host.get('broken') or 0} broken by a crash or drop"]
    return "\n".join(lines)


# The --demo lobby: a 4v4 with one of everything the panel can show (coat,
# skull, host lamp). DEMO_SEATS is {name: (alliance, position)}.
DEMO_HOST_MARK = "good"
DEMO_HOST = {
    "good": {"hosted": 41, "finished": 38, "replays": 33, "host_left": 0, "broken": 3},
    "bad": {"hosted": 26, "finished": 17, "replays": 14, "host_left": 1, "broken": 8},
    "worst": {"hosted": 16, "finished": 10, "replays": 7, "host_left": 4, "broken": 2},
}
DEMO_PLAYERS = [
    {"rank": 1, "name": "jojo_mcbean", "rating": 1341, "wins": 26, "losses": 13, "draws": 2,
     "games": 41, "finished": 39, "redcoatz": {"protected": True}, "host": {"mark": "good"}},
    {"rank": 2, "name": "The Shizzle", "rating": 1298, "wins": 21, "losses": 15, "draws": 1,
     "games": 37, "finished": 34, "redcoatz": {"protected": True}},
    {"rank": 3, "name": "CombatWombat", "rating": 1265, "wins": 18, "losses": 14, "draws": 3,
     "games": 35, "finished": 30, "redcoatz": {"protected": False}},
    {"rank": 4, "name": "Imperator", "rating": 1240, "wins": 15, "losses": 15, "draws": 0,
     "games": 30, "finished": 28},
    {"rank": 5, "name": "Phantom", "rating": 1233, "wins": 14, "losses": 14, "draws": 2,
     "games": 30, "finished": 20, "skull": {"gold": 13200}},
    {"rank": 6, "name": "[BNR] My Clanker", "rating": 1210, "wins": 12, "losses": 13, "draws": 1,
     "games": 26, "finished": 19},
    {"rank": 7, "name": "Rommel", "rating": 1188, "wins": 10, "losses": 14, "draws": 0,
     "games": 24, "finished": 22},
    {"rank": 8, "name": "Lord Farquaad", "rating": 1160, "wins": 8, "losses": 16, "draws": 1,
     "games": 25, "finished": 15},
]
DEMO_SEATS = {p["name"]: (0 if i < 4 else 1, i % 4) for i, p in enumerate(DEMO_PLAYERS)}
DEMO_RECENT = {
    "jojo_mcbean": [1, 1, 1, 1, 1], "The Shizzle": [1, 1, 0, 1, 1],
    "CombatWombat": [1, 0, 1, 1, 1], "Imperator": [1, 1, 1, 1, 0],
    "Phantom": [0, 1, 0, 1, 1], "[BNR] My Clanker": [1, 0, 0, 1, 1],
    "Rommel": [1, 1, 1, 1, 1], "Lord Farquaad": [0, 1, 0, 0, 1],
}
for _p in DEMO_PLAYERS:
    _p["recent"] = [bool(x) for x in DEMO_RECENT[_p["name"]]]


def main():
    """Parse arguments, get consent and the first sync done, then run the panel."""
    ap = argparse.ArgumentParser(description="NTW3 Ladder overlay")
    ap.add_argument("--server", default=DEFAULT_SERVER,
                    help="ladder server base URL")
    ap.add_argument("--demo", action="store_true",
                    help="show fake standings, for checking the overlay against the game")
    ap.add_argument("--scale", type=float, default=None,
                    help="override DPI scale, e.g. 1.5")
    ap.add_argument("--no-auto-hide", action="store_true",
                    help="stay open during battles instead of collapsing")
    ap.add_argument("--save-battle-results", action="store_true",
                    help="after each battle, write the end-of-battle table to "
                         "%%APPDATA%%\\ntw3ladder\\results. Off by default: "
                         "the kills and losses columns are still wrong, so "
                         "these files are material for fixing that, not a "
                         "feature. Written locally and uploaded nowhere.")
    args = ap.parse_args()

    # Ask Windows for real pixels rather than a blurry upscaled window.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    # Consent first, in a window, before anything reads the game or reaches
    # the server. --demo talks to no server and reads no game, so it is not
    # gated.
    if not args.demo:
        name, sid = detect_player()
        if not consent.require_consent_gui(args.server, player=name,
                                           steam_id=sid):
            return 1
        # Once, straight after agreeing: upload the replays already on disk
        # so a new player's history counts. Its own window, because it takes
        # a while.
        if name and firstsync.pending():
            firstsync.run_gui(args.server, name)

    root = tk.Tk()
    Overlay(root, args.server, demo=args.demo, scale=args.scale,
            watch_game=not args.no_auto_hide,
            save_battle_results=args.save_battle_results)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())


