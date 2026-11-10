"""Steer a player who is not on NTW3 9.6.1 to install it.

9.6.1 is a rewrite of the mod's graphics packs that places the textures some
fifty units asked for and never had, the 23e and the 1799 combat generals
among them, so the battle no longer crashes on them. The ladder wants
everyone on it: a lobby plays best when every player has the same files,
and once a player has it the 23e repair in fix23e has nothing to do.

Whether a player has it is read from fix23e.status(): "superseded" means the
9.6.1 variants94b.pack is in place. "stock" and "fixed" are the 9.6 pack,
with or without our 23e repair, and those players are asked to upgrade.
"unknown" and "missing" are left alone: nothing can be said about a pack we
do not recognise, or an install we cannot find.

The server says what to do about a player who declines, through
GET /v1/client's "ntw3" object: {"version": "9.6.1", "url": ..., "required":
bool}. Advised (the default), the dialog returns and Redcoatz carries on with
the player's current files. Required, declining closes Redcoatz the way
declining the other crash fixes does. Either way "Yes" opens the download
page and closes Redcoatz, because Redcoatz would otherwise start the game and
the installer needs the game closed. No answer from the server means advised,
with no download button.

Network: none of its own. Writes nothing.
"""

NTW3_VERSION = "9.6.1"

#: What decide() returns in "action"
ADVISE, REQUIRE = "advise", "require"

#: What prompt() returns
GET, LATER, CLOSE = "get", "later", "close"


def decide(fix23e_state, info):
    """What to do about this install, or None when nothing.

    fix23e_state is fix23e.status()["state"]; info is the /v1/client answer
    (may be empty).
    """
    if fix23e_state not in ("stock", "fixed"):
        return None
    ntw3 = (info or {}).get("ntw3") or {}
    return {
        "action": REQUIRE if ntw3.get("required") else ADVISE,
        "version": ntw3.get("version") or NTW3_VERSION,
        "url": (ntw3.get("url") or "").strip(),
    }


def wording(d):
    """The dialog text for a decide() result."""
    v = d["version"]
    body = (
        "NTW3 %s is out, and your game is still on the earlier packs.\n\n"
        "%s rewrites the mod's graphics packs so that the units whose missing "
        "textures could crash a battle, the 23e and the 1799 combat generals "
        "among them, are repaired for good. The ladder is moving everyone to "
        "it so that every player in a lobby has the same files.\n\n"
        "Install it with Napoleon closed. It changes the packs in place "
        "(about 4 GB); the way back is reinstalling NTW3. Redcoatz works "
        "before and after.\n\n" % (v, v))
    if d["action"] == REQUIRE:
        body += ("Redcoatz needs %s before it runs. Yes opens the download "
                 "page and closes Redcoatz so you can install it. No closes "
                 "Redcoatz." % v)
    else:
        body += ("Yes opens the download page and closes Redcoatz so you can "
                 "install it. No carries on with your current files for now; "
                 "you will be asked again next time.")
    if not d["url"]:
        body = body.replace("opens the download page and closes Redcoatz",
                            "closes Redcoatz")
        body += "\n\nThe installer is posted on the Lordz NTW3 Discord."
    return body


def prompt(d, parent=None, log=print, open_url=None):
    """Ask, open the download page on Yes. Returns GET, LATER or CLOSE."""
    from tkinter import messagebox
    yes = messagebox.askyesno("NTW3 Ladder", wording(d), parent=parent)
    if yes:
        if d["url"]:
            try:
                if open_url is None:
                    import webbrowser
                    open_url = webbrowser.open_new_tab
                open_url(d["url"])
                log(f"ntw3check: opened {d['url']}")
            except Exception as e:             # noqa: BLE001
                log(f"ntw3check: could not open {d['url']}: {e!r}")
        return GET
    return CLOSE if d["action"] == REQUIRE else LATER
