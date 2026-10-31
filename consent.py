"""First-run terms: show them, ask, record the answer.

The answer is stored in two places. Locally in
%APPDATA%\\ntw3ladder\\consent.json, so a player is asked once rather than
every launch. On the server, by a POST to <server>/v1/consent carrying the
Steam ID, in-game name and terms version (with the client version and Steam
key as headers), because a record that lives only on the agreeing player's
machine is not evidence of anything. The local file also remembers whether
the server call succeeded, so an agreement made during an outage is retried.

TERMS_VERSION carries two numbers, "<text>+<client>", for example "1+0.3.3".
The text number says which wording the player saw and must be bumped whenever
the wording below changes. The client version is included so every new build
asks again: the client gains capabilities between versions, and relying on
someone remembering to bump the text number on exactly the right release is a
rule enforced by memory. Keeping both means the record stays honest about
which wording each player accepted.

NOTE FOR THE MAINTAINER: the wording below has not been reviewed by a lawyer.
It sets out plainly what the client does and disclaims what it reasonably can.
Have somebody qualified read it before it is relied on.
"""

import json
import os
import sys
import urllib.error
import urllib.request

from version import USER_AGENT, VERSION
import steamauth  # noqa: E402

#: The wording below. Bump when the text changes, never edit in place under a
#: number players have already agreed to.
#:
#: Each bump so far was a new disclosure: 2 rewrote "what is sent" to describe
#: exactly what the code does and dropped a claim about replay contents nobody
#: had checked; 3 added battle reports read from game memory and the game-file
#: repairs; 4 added the Napoleon.exe repair and the public red-coat badge; 5
#: withdrew the on-disk exe edit, disclosed the in-memory artillery fix and
#: made the repairs required; 6 added the one-click self-update.
TERMS_TEXT = "6"

#: What is stored and sent. Includes the client version, so a new build asks
#: again; see the module docstring.
TERMS_VERSION = f"{TERMS_TEXT}+{VERSION}"

STATE_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "ntw3ladder")
CONSENT_FILE = os.path.join(STATE_DIR, "consent.json")

TERMS = """
================================================================
              WELCOME TO THE NTW3 LADDER, BY REDCOATZ
================================================================

Redcoatz is a community-run competitive ladder for NTW3 line
battles. It ranks players from the battles they actually fought.

WHAT THIS PROGRAM DOES

  * It watches your Napoleon: Total War replay folder for new
    .replay files.
  * When a battle finishes, it uploads that replay file to the
    Redcoatz ladder server at redcoatz.com.
  * It reads your in-game name and Steam ID so the battle can be
    credited to you.
  * It reads the lobby roster so the overlay can show the ratings
    of the people you are about to play.
  * It reads the running game's memory to follow what is
    happening: which lobby you are in, when a battle starts, and
    how that battle ends. It writes to the game's memory in one
    place only: while the game runs, it adds a missing check to
    the game's artillery targeting, which stops large battles
    crashing. It changes nothing else about how the game plays,
    and the game's own program file is never modified.
  * It can repair two known crash bugs by editing files inside
    your Napoleon: Total War installation: a unit pack and the
    sound library mss32.dll. These repairs are required: Redcoatz
    only runs on an install that has them. It asks you first,
    every time, and if you say no it closes without changing
    anything. The original file is kept in a redcoatz_backup
    folder beside it, and Steam's "Verify integrity of game files"
    undoes any of it. If an earlier version changed Napoleon.exe,
    this one puts the original back.
  * When a newer version exists and you click to update, it
    downloads the new Redcoatz from redcoatz.com, checks that the
    file is exactly the published one, replaces itself with it in
    the same folder, and restarts. It never updates on its own.
  * If a battle desynchronises, the game itself writes a log naming
    who lost sync. The client sends that log, so the ladder has a
    record of battles that collapsed. The game writes this file
    only when a battle desynchronises.

WHAT IS SENT TO THE SERVER

  * Any file in your replay folder whose name ends in .replay.
  * Your in-game name and your Steam ID.
  * The desync log described above, on the occasions the game
    writes one. It names the players in that battle, which
    includes people other than you.
  * Reports about each battle you play: that one started, the map
    and who was in it, a short message every few minutes while it
    is still going, and how it ended -- whether the save-replay
    screen appeared, whether the host left, whether you were
    dropped, and whether the game closed on its own. These name
    the other players who were in that battle.
  * The version of this client, sent with every request.
  * Each time the client starts: which of the crash repairs
    described above are in place on your install.
  * The network address your connection comes from. It is stored
    with your agreement to these terms, as described in clause 7,
    and with any desync log you send.

  Uploaded replays and your resulting rating are shown publicly on
  redcoatz.com.

  Other players in a lobby with you see a red coat beside your
  name in their Redcoatz panel. It shows that you run Redcoatz,
  and whether its crash repairs are in place: a filled coat when
  they are, an outline when one is missing. Anyone who looks up
  your name on the ladder server can see the same.

TERMS

  1. Redcoatz is an unofficial, fan-made community project. It is
     not affiliated with, endorsed by, or associated with Creative
     Assembly, Sega, or the Lordz Modding Collective.

  2. This software is provided "as is", without warranty of any
     kind, express or implied. You use it at your own risk.

  3. The people who run Redcoatz are not liable for any damage,
     data loss, account issue, or other harm arising from using
     this software or the ladder.

  4. The ladder is run by volunteers. Ratings, matches and the
     service itself may change, be corrected, or stop entirely at
     any time, with no guarantee of availability.

  5. You are responsible for complying with the terms of service
     of Steam and of any game or mod you use this alongside.

  6. You may stop taking part at any time by exiting the client:
     press Escape while the panel has focus, or end the
     ntw3ladder process from Task Manager. Replays you have
     already uploaded, any desync logs sent, and the ratings and
     results derived from them, become part of the public ladder
     record. They are published on redcoatz.com and are not
     removed on request. Battle reports already sent are kept by
     the ladder server as its record of which battles were fought
     and which of them finished.

  7. If you agree, your Steam ID, your in-game name, the version
     of these terms, the time of your agreement, and the network
     address the agreement was sent from are stored by the ladder
     server, so that there is a record that these terms were shown
     and accepted.

================================================================
"""

PROMPT = "Type AGREE to accept and start, or DISAGREE to quit: "


def _load():
    """Read consent.json, or {} if missing or unreadable."""
    try:
        with open(CONSENT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(doc):
    """Write consent.json. Not fatal if it fails: the player is asked again next launch."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(CONSENT_FILE, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
    except OSError as e:
        print(f"  (could not save your answer: {e})")


def has_agreed():
    """True if this machine has already accepted the current terms."""
    return _load().get("terms_version") == TERMS_VERSION


def _post(server, steam_id, player):
    """POST {steam_id, player, terms_version} to <server>/v1/consent. True if acknowledged."""
    body = json.dumps({
        "steam_id": str(steam_id),
        "player": player or "",
        "terms_version": TERMS_VERSION,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{server.rstrip('/')}/v1/consent", data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": USER_AGENT, **steamauth.headers()})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def require_consent(server, player=None, steam_id=None):
    """Console flow: show the terms if not yet accepted. True if we may proceed.

    Returns False when the player declines, and when there is no console to
    ask on: a client started without a terminal must not assume consent it
    never obtained.
    """
    doc = _load()
    agreed = doc.get("terms_version") == TERMS_VERSION

    if agreed:
        # Retry a record that never reached the server.
        if not doc.get("posted") and steam_id:
            if _post(server, steam_id, player):
                doc["posted"] = True
                _save(doc)
        return True

    if not sys.stdin or not sys.stdin.isatty():
        print("The Redcoatz terms have not been accepted yet, and there is no "
              "console to show them on.\nRun 'python uploader.py' in a "
              "terminal once to read and accept them.")
        return False

    print(TERMS)
    if steam_id:
        print(f"  Signing as: {player or 'unknown'}  (Steam ID {steam_id})\n")
    else:
        print("  Your Steam ID could not be read. Start Napoleon first so the "
              "agreement can be recorded against your account.\n")

    try:
        answer = input(PROMPT).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nNo answer given; nothing was uploaded.")
        return False

    if answer not in ("agree", "a", "yes", "y"):
        print("\nDeclined. Nothing has been uploaded and the client will now "
              "exit.\nYou can run it again at any time.")
        return False

    doc = {"terms_version": TERMS_VERSION, "player": player,
           "steam_id": str(steam_id) if steam_id else None, "posted": False}
    if steam_id and _post(server, steam_id, player):
        doc["posted"] = True
        print("\nThank you. Your agreement has been recorded.\n")
    else:
        # Still proceed: the player did agree, and the retry above catches up.
        print("\nThank you. (The ladder server could not be reached to record "
              "this just now; it will be sent automatically later.)\n")
    _save(doc)
    return True


def sync(server, player=None, steam_id=None):
    """Send an agreement that was recorded here but never reached the server.

    The server records nothing without the Steam key, so an agreement posted
    before the sign-in was refused by design. This is the retry, run once the
    key exists. True when the server has the record.
    """
    doc = _load()
    if doc.get("terms_version") != TERMS_VERSION:
        return False
    if doc.get("posted"):
        return True
    sid = steam_id or doc.get("steam_id")
    if sid and _post(server, sid, player or doc.get("player")):
        doc["posted"] = True
        _save(doc)
        return True
    return False


def _accept(server, player, steam_id):
    """Shared tail of both flows: store the answer, tell the server."""
    doc = {"terms_version": TERMS_VERSION, "player": player,
           "steam_id": str(steam_id) if steam_id else None, "posted": False}
    if steam_id and _post(server, steam_id, player):
        doc["posted"] = True
    _save(doc)
    return doc["posted"]


def require_consent_gui(server, player=None, steam_id=None, parent=None):
    """The same decision as require_consent, asked in a Tk window.

    The client normally runs with no console, so the console flow would
    refuse every time. Shares TERMS_VERSION and the consent file with it, so
    agreeing in either place counts for both.

    Pass `parent` when the caller already has a Tk root; this then opens a
    Toplevel instead of a second interpreter, which Tcl's threading rules
    make unsafe to tear down.
    """
    if has_agreed():
        doc = _load()
        if not doc.get("posted") and steam_id and _post(server, steam_id, player):
            doc["posted"] = True
            _save(doc)
        return True

    import tkinter as tk

    result = {"ok": False}
    owns_root = parent is None
    root = tk.Tk() if owns_root else tk.Toplevel(parent)
    root.title("NTW3 Ladder - Terms")
    root.configure(bg="#10131C")
    root.minsize(680, 560)

    tk.Label(root, text="Welcome to the NTW3 Ladder, by Redcoatz",
             bg="#10131C", fg="#E4C67E",
             font=("Georgia", 15, "bold")).pack(pady=(16, 4))
    tk.Label(root, text="Please read and accept before the client starts.",
             bg="#10131C", fg="#8A93A6", font=("Georgia", 10)).pack()

    frame = tk.Frame(root, bg="#10131C")
    frame.pack(fill="both", expand=True, padx=16, pady=12)
    bar = tk.Scrollbar(frame)
    bar.pack(side="right", fill="y")
    text = tk.Text(frame, wrap="word", bg="#0B0E15", fg="#ECE6D7",
                   insertbackground="#ECE6D7", relief="flat",
                   font=("Consolas", 10), yscrollcommand=bar.set,
                   padx=14, pady=12)
    text.pack(side="left", fill="both", expand=True)
    bar.config(command=text.yview)
    text.insert("1.0", TERMS)
    if steam_id:
        text.insert("end", f"\n  Signing as: {player or 'unknown'}"
                           f"   (Steam ID {steam_id})\n")
    else:
        text.insert("end", "\n  Your Steam ID could not be read. Start "
                           "Napoleon first so the agreement can be recorded "
                           "against your account.\n")
    # Read-only, but still selectable so a player can copy the terms out.
    text.config(state="disabled")

    buttons = tk.Frame(root, bg="#10131C")
    buttons.pack(pady=(0, 16))

    def agree():
        result["ok"] = True
        result["answered"] = True
        print("  terms window: AGREE clicked", flush=True)
        root.destroy()

    def decline():
        result["ok"] = False
        result["answered"] = True
        print("  terms window: DISAGREE or window closed", flush=True)
        root.destroy()

    tk.Button(buttons, text="I Disagree", command=decline, width=16,
              bg="#171C29", fg="#CBC4B2", relief="flat",
              activebackground="#202638", font=("Georgia", 10)
              ).pack(side="left", padx=8)
    tk.Button(buttons, text="I Agree", command=agree, width=16,
              bg="#AE3238", fg="#FFFBF4", relief="flat",
              activebackground="#C9484A", font=("Georgia", 10, "bold")
              ).pack(side="left", padx=8)

    # Closing the window is not agreement.
    root.protocol("WM_DELETE_WINDOW", decline)

    # Bring it to the front: a dialog that opens behind the browser or the
    # game looks like the program not starting. Topmost is dropped
    # immediately afterwards.
    try:
        root.lift()
        root.attributes("-topmost", True)
        root.after(400, lambda: root.attributes("-topmost", False))
        root.focus_force()
    except Exception:
        pass

    # Log that it really is on screen, and where, so an invisible dialog can
    # be diagnosed afterwards.
    try:
        root.update_idletasks()
        print(f"  terms window: geometry={root.winfo_geometry()} "
              f"viewable={bool(root.winfo_viewable())} "
              f"screen={root.winfo_screenwidth()}x{root.winfo_screenheight()}",
              flush=True)
    except Exception as e:
        print(f"  terms window: could not measure: {e!r}", flush=True)

    # A heartbeat in the log while the window is up, so a log that stops here
    # shows how long the dialog survived before the process ended.
    import time as _time
    opened = _time.time()

    def tick():
        if result.get("answered"):
            return
        print(f"  terms window: still open after "
              f"{_time.time() - opened:.0f}s", flush=True)
        try:
            root.after(3000, tick)
        except Exception:
            pass

    try:
        root.after(3000, tick)
    except Exception:
        pass

    if owns_root:
        root.mainloop()
    else:
        # transient() only when the parent is actually on screen. Tk withdraws
        # a transient window whose master is withdrawn, and the caller hides
        # its root deliberately, so the terms would flash and vanish while the
        # client waited invisibly for an answer.
        if parent.winfo_viewable():
            root.transient(parent)
        else:
            print("  terms window: parent hidden, skipping transient()",
                  flush=True)
        root.grab_set()
        # Prove it is still up after the modal setup, not just before it.
        try:
            root.update_idletasks()
            print(f"  terms window: after modal setup viewable="
                  f"{bool(root.winfo_viewable())} "
                  f"state={root.state()}", flush=True)
        except Exception as e:
            print(f"  terms window: post-setup check failed: {e!r}", flush=True)
        parent.wait_window(root)

    print(f"  terms window: closed after {_time.time() - opened:.0f}s, "
          f"answer={'agree' if result['ok'] else 'decline/dismiss'}", flush=True)

    try:
        root.destroy()
    except Exception:
        pass          # already gone via a button, which is the usual path

    if result["ok"]:
        _accept(server, player, steam_id)
    return result["ok"]
