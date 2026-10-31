"""The client's version, in one place.

Bump VERSION here and everything follows: the User-Agent on every request,
the string shown on the panel, and the Windows file properties build.py bakes
into the exe. build.py bumps the last number and rewrites this file, so the
source and the exe cannot drift apart. The download filename stays fixed so
the link on the site never rots.

This number also drives the consent prompt: consent.TERMS_VERSION is
"<terms text>+<this version>", so every new build asks the player to agree
again.

No imports, on purpose: ntw3ladder.py logs the version before loading
anything else, so a build that fails early can still be identified.
"""

VERSION = "0.8.2"

#: What every outgoing request identifies itself as.
USER_AGENT = f"ntw3ladder-client/{VERSION}"
