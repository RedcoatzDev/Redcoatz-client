"""Build Redcoatz.exe with PyInstaller.

    python build.py                one file, no console  (what players get)
    python build.py --console      same, but with a console window for debugging
    python build.py --onedir       a folder instead of one file

Output lands in client/dist/. By default the exe is then copied to
web/public/Redcoatz.exe and web/public/client-version.json is rewritten with
its version, size, SHA-256 and build date; --no-publish skips that.

Builds are reproducible across machines. Four things made identical source
produce different bytes, and each is pinned here:

  * timestamps in the archive: SOURCE_DATE_EPOCH is midnight UTC of the
    build date, which client-version.json records as "built";
  * hash-seeded ordering: PYTHONHASHSEED=0;
  * whatever else is installed beside PyInstaller: the build runs from
    client/.venv-build, a venv this script makes with exactly PINS in it.
    On 30 October 2026 the machine's setuptools rode into the exe (2 MB of
    it) because PyInstaller's hooks saw it installed;
  * the Universal C Runtime: PyInstaller bundles the api-ms-win-*.dll
    forwarders and ucrtbase.dll it finds, and those are whatever Windows
    SDK or Windows build the builder has. They are part of Windows 10 and
    later, so the spec drops them and the exe uses the system's.

Anyone with the same Python version (recorded in client-version.json) who
checks out the release tag and runs build.py --verify <built date> gets the
published hash; .github/workflows/verify.yml does exactly that on GitHub.

What the exe embeds: the Python runtime and the libraries the client imports,
the client's modules from this folder (ntw3ladder.py and everything it
imports, plus the hidden imports listed in main()), gamefiles/button_rematch.luac
(the results hook) as a data file, the redcoatz.ico icon, and a Windows
version resource carrying VERSION. Nothing else from the repository goes in.

Onefile and unsigned, like the NTW3 launcher itself, which is a PyInstaller
onefile build the whole community already runs; --onedir has a lower antivirus
false-positive rate and is one flag away. truststore is forced in because
nettrust.py imports it inside a try/except, which PyInstaller's static
analysis misses; leaving it out would restore certificate failures on
machines whose antivirus scans HTTPS.
"""

import argparse
import datetime
import json
import os
import shutil
import subprocess
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from version import VERSION  # noqa: E402

#: The Windows icon, six sizes in one file, built by scratch/mkicon2.py from
#: the Discord server's artwork. The three small frames are a crop of one
#: figure's head and shoulders, because two full figures at 16 pixels are
#: smudges; Windows picks the frame for the size it needs.
ICON = os.path.join(HERE, "redcoatz.ico")

#: The toolchain that goes into the exe, pinned so two machines build the
#: same bytes. Installed into VENV by _build_python; nothing else from the
#: building machine's Python reaches the archive. Bump deliberately, and
#: the next release records the new versions in client-version.json.
PYINSTALLER = "6.22.0"
HOOKS_CONTRIB = "2026.9"
TRUSTSTORE = "0.10.4"
PINS = [f"pyinstaller=={PYINSTALLER}",
        f"pyinstaller-hooks-contrib=={HOOKS_CONTRIB}",
        f"truststore=={TRUSTSTORE}"]
VENV = os.path.join(HERE, ".venv-build")


def _build_python():
    """The interpreter PyInstaller runs under: VENV, made from this one with
    exactly PINS installed. Rebuilt whenever the pins change.
    """
    py = os.path.join(VENV, "Scripts", "python.exe")
    stamp = os.path.join(VENV, "pins.txt")
    want = " ".join(PINS)
    have = ""
    if os.path.exists(stamp):
        with open(stamp, "r", encoding="utf-8") as f:
            have = f.read().strip()
    if have != want or not os.path.exists(py):
        print(f"  making {VENV}\n    {want}")
        shutil.rmtree(VENV, ignore_errors=True)
        subprocess.run([sys.executable, "-m", "venv", VENV], check=True)
        subprocess.run([py, "-m", "pip", "install", "-q",
                        "--disable-pip-version-check", *PINS], check=True)
        with open(stamp, "w", encoding="utf-8") as f:
            f.write(want + "\n")
    return py


def _toolchain(py):
    """What the build interpreter actually has, for client-version.json."""
    out = subprocess.run(
        [py, "-c",
         "import platform, PyInstaller, importlib.metadata as m;"
         "print(platform.python_version(), PyInstaller.__version__,"
         " m.version('pyinstaller-hooks-contrib'), m.version('truststore'))"],
        capture_output=True, text=True, check=True).stdout.split()
    return dict(zip(("python", "pyinstaller", "hooks_contrib", "truststore"), out))


#: Dropped from the archive by the spec: see the module docstring.
UCRT_FILTER = (
    "# Added by build.py. The Universal C Runtime is part of Windows 10 and\n"
    "# later, and the copies PyInstaller finds differ from machine to machine,\n"
    "# so they stay out and the exe uses the system's.\n"
    "a.binaries = [b for b in a.binaries\n"
    "              if not (b[0].lower().startswith('api-ms-win-')\n"
    "                      or b[0].lower() == 'ucrtbase.dll')]\n")


def _write_spec(py, opts, script):
    """build/Redcoatz.spec: PyInstaller's own makespec from the options, then
    the UCRT filter inserted before the PYZ is built. Generated every time,
    so the spec never drifts from the options here.
    """
    subprocess.run([py, "-m", "PyInstaller.utils.cliutils.makespec",
                    *opts, script], cwd=HERE, check=True)
    spec = os.path.join(HERE, "build", "Redcoatz.spec")
    with open(spec, "r", encoding="utf-8") as f:
        text = f.read()
    if "pyz = PYZ(" not in text:
        raise SystemExit(f"{spec} has no 'pyz = PYZ(' line to anchor the UCRT filter on")
    text = text.replace("pyz = PYZ(", UCRT_FILTER + "pyz = PYZ(", 1)
    with open(spec, "w", encoding="utf-8") as f:
        f.write(text)
    return spec


def _version_file():
    """Write the resource block that gives the exe its Windows file version,
    so Properties -> Details answers "which version is this?" without running
    it. Windows wants four numbers; VERSION carries three, so the fourth is 0.
    """
    parts = [int(p) for p in VERSION.split(".")][:3]
    while len(parts) < 3:
        parts.append(0)
    a, b, c = parts
    path = os.path.join(HERE, "build", "version_info.txt")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({a}, {b}, {c}, 0), prodvers=({a}, {b}, {c}, 0),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
    date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Redcoatz'),
      StringStruct('FileDescription', 'Redcoatz'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('InternalName', 'Redcoatz'),
      StringStruct('OriginalFilename', 'Redcoatz.exe'),
      StringStruct('ProductName', 'Redcoatz'),
      StringStruct('ProductVersion', '{VERSION}'),
    ])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""")
    return path


def _published_version():
    """The version currently sitting in web/public, or None."""
    meta = os.path.join(os.path.dirname(HERE), "web", "public",
                        "client-version.json")
    try:
        with open(meta, "r", encoding="utf-8") as f:
            return json.load(f).get("version")
    except (OSError, ValueError):
        return None


def _sha256(path):
    """SHA-256 of a file, read in blocks."""
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _bump(v):
    """'0.3.2' -> '0.3.3'. The last number only; the others are decisions."""
    parts = [int(p) for p in re.findall(r"\d+", v)][:3]
    while len(parts) < 3:
        parts.append(0)
    parts[-1] += 1
    return ".".join(str(p) for p in parts)


def _write_version(v):
    """Rewrite the VERSION assignment in client/version.py, in place.
    Anchored at the start of a line so a mention in the docstring is not hit.
    """
    path = os.path.join(HERE, "version.py")
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    out, n = re.subn(r'(?m)^VERSION = "[^"]*"', f'VERSION = "{v}"', src, count=1)
    if n != 1:
        raise SystemExit(f"could not find the VERSION assignment in {path}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)
    return v


def _choose_version(asked, force, publishing):
    """The version this build will carry, bumping the patch number when the
    current one is already published.

    Two builds sharing a number is a failure nothing can detect afterwards, so
    the bump happens here and version.py is rewritten so source and binary
    agree. --force keeps the number, for a published build that never reached
    anybody; it should be a decision rather than a default.
    """
    if asked:
        if asked == _published_version() and not force:
            raise SystemExit(f"version {asked} is already published "
                             f"(--force to build over it)")
        return _write_version(asked) if asked != VERSION else asked

    was = _published_version()
    if not publishing or was is None or was != VERSION:
        return VERSION
    if force:
        print(f"  --force: keeping {VERSION}, which is already published")
        return VERSION
    nxt = _bump(VERSION)
    print(f"  {VERSION} is already published; bumped to {nxt}")
    return _write_version(nxt)


def main():
    """Choose the version, run PyInstaller, publish into web/public."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--console", action="store_true",
                    help="keep a console window, for debugging")
    ap.add_argument("--onedir", action="store_true",
                    help="produce a folder rather than a single exe")
    ap.add_argument("--clean", action="store_true",
                    help="discard caches first (already the default when "
                         "publishing)")
    ap.add_argument("--dirty", action="store_true",
                    help="reuse PyInstaller's caches even when publishing. "
                         "Faster, and how a broken release gets built")
    ap.add_argument("--no-publish", action="store_true",
                    help="do not copy the result into web/public")
    ap.add_argument("--force", action="store_true",
                    help="reuse the current version even though it is already "
                         "published")
    ap.add_argument("--version",
                    help="build this version instead of bumping the patch "
                         "number, e.g. --version 0.4. Rewrites "
                         "client/version.py so the source and the binary agree")
    ap.add_argument("--verify", metavar="YYYY-MM-DD",
                    help="rebuild a published release to compare hashes: "
                         "use this build date for SOURCE_DATE_EPOCH, keep "
                         "the version, build clean, publish nothing")
    args = ap.parse_args()
    if args.verify:
        args.no_publish = True
        args.clean = True

    # Module global, so _version_file() and the publish step see the chosen
    # number rather than the imported one.
    global VERSION
    VERSION = _choose_version(args.version, args.force, not args.no_publish)

    py = _build_python()
    tools = _toolchain(py)

    # A published build is always built from scratch: PyInstaller once rebuilt
    # only the exe wrapper around a stale cached payload, and the release
    # could not start. Incremental builds are for iterating.
    if args.clean or (not args.no_publish and not args.dirty):
        for d in ("build", "dist"):
            shutil.rmtree(os.path.join(HERE, d), ignore_errors=True)

    # What the spec is made from; see _write_spec.
    opts = [
        "--name", "Redcoatz",
        "--onedir" if args.onedir else "--onefile",
        "--console" if args.console else "--windowed",
        "--icon", ICON,
        # The results hook travels as a data file beside the modules, so
        # postbattle_hook.SOURCE resolves under sys._MEIPASS in the exe.
        # truststore is imported behind a try/except and has to be named.
        "--add-data", os.path.join(HERE, "gamefiles", "button_rematch.luac")
        + os.pathsep + "gamefiles",
        "--hidden-import", "postbattle_hook",
        "--hidden-import", "truststore",
        # Imported normally; naming them makes a missing one a build error
        # rather than a crash on a player's machine.
        "--hidden-import", "consent",
        "--hidden-import", "steamauth",
        "--hidden-import", "firstsync",
        "--hidden-import", "gamelaunch",
        "--hidden-import", "nettrust",
        "--hidden-import", "uploader",
        "--hidden-import", "ladder_overlay",
        "--hidden-import", "mapnames",
        "--hidden-import", "lobby_memory",
        "--hidden-import", "live_seats",
        "--hidden-import", "roster",
        "--hidden-import", "lobby",
        # The rest are imported lazily, inside functions, so PyInstaller's
        # static analysis never sees them; left out, the feature silently does
        # nothing in the frozen exe while working from source.
        "--hidden-import", "results",
        "--hidden-import", "desync",
        "--hidden-import", "fix23e",
        "--hidden-import", "fixmiles",
        "--hidden-import", "fixbuffer",
        "--hidden-import", "clientstatus",
        "--hidden-import", "artilleryfix",
        "--hidden-import", "selfupdate",
        "--hidden-import", "gameprefs",
        "--hidden-import", "singleton",
        "--version-file", _version_file(),
        "--paths", HERE,
        "--specpath", os.path.join(HERE, "build"),
    ]
    spec = _write_spec(py, opts, os.path.join(HERE, "ntw3ladder.py"))
    cmd = [
        py, "-m", "PyInstaller", "--noconfirm",
        "--distpath", os.path.join(HERE, "dist"),
        "--workpath", os.path.join(HERE, "build"),
        spec,
    ]

    # Reproducibility: the build date, as midnight UTC, is the only timestamp
    # PyInstaller may write, and the hash seed is fixed so every set and dict
    # it iterates comes out in the same order.
    built = (datetime.date.fromisoformat(args.verify) if args.verify
             else datetime.datetime.now(datetime.timezone.utc).date())
    epoch = int(datetime.datetime.combine(
        built, datetime.time(), datetime.timezone.utc).timestamp())
    env = dict(os.environ, SOURCE_DATE_EPOCH=str(epoch), PYTHONHASHSEED="0")
    print(f"SOURCE_DATE_EPOCH={epoch} ({built})  PYTHONHASHSEED=0  "
          + "  ".join(f"{k} {v}" for k, v in tools.items()))
    print(" ".join(cmd), "\n")
    r = subprocess.run(cmd, cwd=HERE, env=env)
    if r.returncode != 0:
        return r.returncode

    exe = os.path.join(HERE, "dist",
                       "Redcoatz" if args.onedir else "",
                       "Redcoatz.exe")
    exe = os.path.normpath(exe)
    if not os.path.exists(exe):
        print("\nbuild reported success but the exe is not where expected")
        return 1
    print(f"\nbuilt {exe}  ({os.path.getsize(exe)/1024/1024:.1f} MB)  v{VERSION}")
    print(f"sha256 {_sha256(exe)}")

    # Copy into the website by default: the download link points at the app's
    # own origin, so a build not copied here leaves the site serving the
    # previous exe.
    if not args.no_publish and not args.onedir:
        dest = os.path.join(os.path.dirname(HERE), "web", "public",
                            "Redcoatz.exe")
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(exe, dest)
            print(f"published -> {dest}")

            # What the site shows under the download button and what the
            # server reads as the latest version, written by the same step
            # that copies the exe so the number and the file cannot disagree.
            meta = os.path.join(os.path.dirname(dest), "client-version.json")
            with open(meta, "w", encoding="utf-8") as f:
                json.dump({
                    "version": VERSION,
                    "bytes": os.path.getsize(exe),
                    # selfupdate.py checks its download against this.
                    "sha256": _sha256(exe),
                    # The UTC date whose midnight was SOURCE_DATE_EPOCH;
                    # build.py --verify <built> rebuilds this exact exe.
                    "built": built.isoformat(),
                    # What a verifier needs to get the same bytes: this
                    # Python, and build.py installs the rest (PINS).
                    **tools,
                }, f, indent=2)
            print(f"published -> {meta}")
            print("  commit this, then deploy, or the site serves the old one")
            print("  the server reads its 'latest' from this file, so the "
                  "deploy is the whole release")
            # The server's CLIENT_VERSION config var is only a fallback for a
            # slug without this file.
            print("  CLIENT_MIN_VERSION is the separate, deliberate one that "
                  "REFUSES older clients; it stays a config var on purpose.")
        except OSError as e:
            print(f"could not publish to the website: {e}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
