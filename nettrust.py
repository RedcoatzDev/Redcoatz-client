"""Verify TLS against the operating system's certificate store, if truststore is installed.

Consumer antivirus (AVG and Avast among others) installs its own root CA and
intercepts every HTTPS connection on the machine to scan it. Windows trusts
that root, so browsers see nothing wrong, but Python ships its own
certificate bundle without it, and every upload fails with
CERTIFICATE_VERIFY_FAILED. Importing the antivirus root into Python's bundle
is unreliable (the AVG root seen was malformed and OpenSSL 3 rejects it).

What this changes: if the optional `truststore` package is present,
truststore.inject_into_ssl() makes Python's ssl module verify server
certificates against the Windows certificate store instead of Python's
bundled CA file. It adds no certificates anywhere and modifies no store; it
only changes which existing store Python consults, within this process.
Without truststore nothing changes and HTTPS works on any machine that is not
being intercepted.

Import this module once, before anything opens a connection. OS_TRUST says
which path is in use, for the hint shown when a handshake fails.
"""

try:
    import truststore

    truststore.inject_into_ssl()
    OS_TRUST = True
except Exception:
    # ImportError when not installed; any other failure must not stop a
    # client that would have worked without it.
    OS_TRUST = False


def tls_hint() -> str:
    """What to tell a player whose TLS handshake failed."""
    if OS_TRUST:
        return ("TLS is using the Windows certificate store, so antivirus "
                "HTTPS scanning is not the cause.")
    return ("If this machine runs antivirus with HTTPS scanning (AVG, Avast "
            "and similar), Python cannot verify the intercepted certificate. "
            "Run 'pip install truststore' and restart the client.")
