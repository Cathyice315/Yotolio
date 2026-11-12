"""Use the macOS CA bundle when a Python installation has no default trust store."""

import os
from pathlib import Path
import ssl
import sys
import urllib.error


MACOS_CA_BUNDLE = "/etc/ssl/cert.pem"
CERTIFICATE_ERROR = (
    "The secure connection could not be verified. On macOS, use the official Python "
    "installer's Install Certificates.command; otherwise update your trusted certificates "
    "or ask your network administrator for help, then try again. "
    "Certificate verification remains enabled."
)


def configure_macos_trust():
    if sys.platform != "darwin" or any(key in os.environ for key in ("SSL_CERT_FILE", "SSL_CERT_DIR")):
        return False
    paths = ssl.get_default_verify_paths()
    if paths.cafile or paths.capath:
        return False
    if not Path(MACOS_CA_BUNDLE).is_file():
        raise RuntimeError(
            "Python has no trusted certificate store. Use the official Python installer's "
            "Install Certificates.command, then try again."
        )
    try:
        context = ssl.create_default_context(cafile=MACOS_CA_BUNDLE)
    except (OSError, ssl.SSLError) as error:
        raise RuntimeError("The macOS certificate bundle could not be loaded.") from error
    if context.cert_store_stats()["x509_ca"] == 0:
        raise RuntimeError("The macOS certificate bundle contains no trusted certificates.")
    os.environ["SSL_CERT_FILE"] = MACOS_CA_BUNDLE
    print("TLS: Using the macOS certificate bundle with verification enabled.", flush=True)
    return True


def certificate_error(error):
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    return isinstance(reason, ssl.SSLCertVerificationError)
