"""Identify Yoto logins and keep their local upload histories separate."""

import base64
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from yoto_tls import CERTIFICATE_ERROR, certificate_error, configure_macos_trust


ISSUER = "https://login.yotoplay.com/"
USERINFO = ISSUER + "userinfo"
JWKS = ISSUER + ".well-known/jwks.json"
API_AUDIENCE = "https://api.yotoplay.com"
KEY_PATTERN = re.compile(r"[0-9a-f]{64}")
WORKSPACE_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
PRIVATE_FILES = ("authorization.json", "upload-state.json", "asset-selections.json")
IDENTITY_ERRORS = {
    "ACCOUNT_IDENTITY_UNAVAILABLE": "Could not verify the Yoto account.",
    "ACCOUNT_IDENTITY_REJECTED": "Yoto did not accept this login for account verification.",
    "ACCOUNT_IDENTITY_SERVICE_UNAVAILABLE": "Yoto's account verification service is unavailable.",
    "ACCOUNT_IDENTITY_KEYS_UNAVAILABLE": "Yoto's account verification keys are unavailable.",
    "ACCOUNT_IDENTITY_VERIFIER_UNAVAILABLE": "The local account signature verifier is unavailable.",
    "ACCOUNT_IDENTITY_TOKEN_INVALID": "Yoto's account identity could not be verified securely.",
}


class AccountError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class AccountIdentityError(AccountError):
    def __init__(self, code="ACCOUNT_IDENTITY_UNAVAILABLE"):
        super().__init__(
            code, IDENTITY_ERRORS[code] + " The existing login and upload history were not changed.",
        )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def _base64url(value, maximum):
    if (not isinstance(value, str) or not value or len(value) > maximum
            or not re.fullmatch(r"[A-Za-z0-9_-]+", value)):
        raise ValueError("Invalid base64url")
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _json_object(data):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON member")
            result[key] = value
        return result

    value = json.loads(data, object_pairs_hook=unique_pairs)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def _der(tag, value):
    length = len(value)
    encoded_length = (bytes([length]) if length < 128 else
                      bytes([0x80 | ((length.bit_length() + 7) // 8)])
                      + length.to_bytes((length.bit_length() + 7) // 8, "big"))
    return bytes([tag]) + encoded_length + value


def _public_key(jwk):
    modulus = _base64url(jwk.get("n"), 1400)
    exponent = _base64url(jwk.get("e"), 12)
    n, e = int.from_bytes(modulus, "big"), int.from_bytes(exponent, "big")
    if (not 2048 <= n.bit_length() <= 8192 or n % 2 == 0
            or not 3 <= e <= 0xffffffff or e % 2 == 0):
        raise ValueError("Invalid RSA public key")

    def integer(value):
        value = value.lstrip(b"\0") or b"\0"
        return _der(0x02, (b"\0" if value[0] & 0x80 else b"") + value)

    rsa = _der(0x30, integer(modulus) + integer(exponent))
    algorithm = bytes.fromhex("300d06092a864886f70d0101010500")
    public_key = _der(0x30, algorithm + _der(0x03, b"\0" + rsa))
    encoded = base64.b64encode(public_key)
    lines = [encoded[index:index + 64] for index in range(0, len(encoded), 64)]
    return b"-----BEGIN PUBLIC KEY-----\n" + b"\n".join(lines) + b"\n-----END PUBLIC KEY-----\n"


def _signing_key(key_id):
    request = urllib.request.Request(JWKS, headers={"Accept": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
            data = response.read(262_145)
        if len(data) > 262_144:
            raise ValueError("Key set is too large")
        keys = _json_object(data).get("keys")
        if not isinstance(keys, list):
            raise ValueError("Invalid key set")
        matches = [
            key for key in keys
            if isinstance(key, dict) and key.get("kid") == key_id
            and key.get("kty") == "RSA" and key.get("alg", "RS256") == "RS256"
            and key.get("use", "sig") == "sig"
            and ("key_ops" not in key or
                 (isinstance(key["key_ops"], list) and "verify" in key["key_ops"]))
        ]
        if len(matches) != 1:
            raise ValueError("Signing key not found")
        return _public_key(matches[0])
    except urllib.error.HTTPError as error:
        error.close()
        raise AccountIdentityError("ACCOUNT_IDENTITY_KEYS_UNAVAILABLE") from None
    except (OSError, ValueError, UnicodeError) as error:
        if certificate_error(error):
            raise RuntimeError(CERTIFICATE_ERROR) from None
        raise AccountIdentityError("ACCOUNT_IDENTITY_KEYS_UNAVAILABLE") from None


def _verify_signature(public_key, message, signature):
    executable = "/usr/bin/openssl" if sys.platform == "darwin" else shutil.which("openssl")
    if not executable:
        raise AccountIdentityError("ACCOUNT_IDENTITY_VERIFIER_UNAVAILABLE")
    try:
        with tempfile.TemporaryDirectory(prefix="yotolio-verify-") as directory:
            key_path = Path(directory) / "public-key.pem"
            signature_path = Path(directory) / "signature.bin"
            key_path.write_bytes(public_key)
            signature_path.write_bytes(signature)
            result = subprocess.run(
                [executable, "dgst", "-sha256", "-verify", str(key_path), "-signature", str(signature_path)],
                input=message, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                check=False,
            )
    except (OSError, subprocess.SubprocessError):
        raise AccountIdentityError("ACCOUNT_IDENTITY_VERIFIER_UNAVAILABLE") from None
    if result.returncode:
        raise AccountIdentityError("ACCOUNT_IDENTITY_TOKEN_INVALID")


def _verified_subject(raw_token, client_id, nonce, access_token, *, identity_token):
    try:
        if not isinstance(raw_token, str) or len(raw_token) > 65_536:
            raise ValueError("Invalid token")
        parts = raw_token.split(".")
        if len(parts) != 3:
            raise ValueError("Invalid token")
        header = _json_object(_base64url(parts[0], 4096))
        claims = _json_object(_base64url(parts[1], 49_152))
        signature = _base64url(parts[2], 2048)
        key_id = header.get("kid")
        if (header.get("alg") != "RS256" or not isinstance(key_id, str)
                or not key_id or len(key_id) > 256 or "crit" in header
                or header.get("b64", True) is not True):
            raise ValueError("Unsupported token header")
        subject = claims.get("sub")
        if (claims.get("iss") != ISSUER or not isinstance(subject, str)
                or not subject or len(subject) > 255 or not subject.isascii()
                or any(ord(char) < 32 or ord(char) == 127 for char in subject)):
            raise ValueError("Invalid token subject")
        audience = claims.get("aud")
        if isinstance(audience, str):
            audience = [audience]
        if (not isinstance(audience, list) or not audience or len(audience) > 10
                or any(not isinstance(item, str) for item in audience)):
            raise ValueError("Invalid token audience")
        now = time.time()
        for name in ("iat", "exp"):
            value = claims.get(name)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("Invalid token time")
        if claims["exp"] <= now or claims["iat"] > now + 120 or claims["iat"] > claims["exp"]:
            raise ValueError("Expired token")
        if "nbf" in claims:
            value = claims["nbf"]
            if type(value) not in (int, float) or not math.isfinite(value) or value > now + 120:
                raise ValueError("Token not yet valid")
        if identity_token:
            token_nonce = claims.get("nonce")
            if (audience != [client_id] or not isinstance(token_nonce, str) or not token_nonce.isascii()
                    or not isinstance(nonce, str) or not nonce.isascii()
                    or not hmac.compare_digest(token_nonce, nonce)):
                raise ValueError("Invalid identity token")
            if "azp" in claims and claims["azp"] != client_id:
                raise ValueError("Invalid authorized party")
            if "at_hash" in claims:
                digest = hashlib.sha256(access_token.encode("ascii")).digest()[:16]
                expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
                if (not isinstance(claims["at_hash"], str) or not claims["at_hash"].isascii()
                        or not hmac.compare_digest(claims["at_hash"], expected)):
                    raise ValueError("Invalid access token hash")
        else:
            parties = [claims[name] for name in ("azp", "client_id") if name in claims]
            if (API_AUDIENCE not in audience or not parties or any(party != client_id for party in parties)
                    or subject.endswith("@clients")):
                raise ValueError("Invalid access token")
        public_key = _signing_key(key_id)
        _verify_signature(public_key, (parts[0] + "." + parts[1]).encode("ascii"), signature)
        return subject
    except (ValueError, UnicodeError, OverflowError):
        raise AccountIdentityError("ACCOUNT_IDENTITY_TOKEN_INVALID") from None


def identify_account(access_token, *, client_id=None, id_token=None, nonce=None):
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("Yoto did not provide an access token for account identification.")
    configure_macos_trust()
    request = urllib.request.Request(
        USERINFO, headers={"Authorization": "Bearer " + access_token, "Accept": "application/json"},
    )
    failure = "ACCOUNT_IDENTITY_SERVICE_UNAVAILABLE"
    diagnostic = "userinfo_response=invalid"
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
            data = response.read(65_537)
        if len(data) > 65_536:
            raise ValueError("Account response is too large")
        info = json.loads(data)
        subject = info.get("sub") if isinstance(info, dict) else None
        if not isinstance(subject, str) or not subject or len(subject) > 255:
            raise ValueError("Account response has no valid subject")
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        diagnostic = f"userinfo_http={status}" if 100 <= status <= 599 else "userinfo_http=other"
        if status in {401, 403}:
            failure = "ACCOUNT_IDENTITY_REJECTED"
    except (OSError, ValueError, UnicodeError) as error:
        if certificate_error(error):
            raise RuntimeError(CERTIFICATE_ERROR) from None
        diagnostic = "userinfo_error=network" if isinstance(error, OSError) else diagnostic
    else:
        return hashlib.sha256((ISSUER + "\0" + subject).encode("utf-8")).hexdigest()

    if not client_id:
        raise AccountIdentityError()
    print(f"IDENTITY_DIAGNOSTIC {diagnostic}", flush=True)
    candidate = id_token if id_token is not None else access_token
    if id_token is None and access_token.count(".") != 2:
        raise AccountIdentityError(failure)
    subject = _verified_subject(candidate, client_id, nonce, access_token, identity_token=id_token is not None)
    return hashlib.sha256((ISSUER + "\0" + subject).encode("utf-8")).hexdigest()


def load_bindings(base):
    path = Path(base) / "account-bindings.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        bindings = data.get("accounts") if isinstance(data, dict) and data.get("version") == 1 else None
        if (not isinstance(bindings, dict)
                or any(not isinstance(key, str) or not KEY_PATTERN.fullmatch(key)
                       or not isinstance(name, str) or not WORKSPACE_PATTERN.fullmatch(name)
                       for key, name in bindings.items())
                or len(set(bindings.values())) != len(bindings)):
            raise ValueError("Invalid account bindings")
        return bindings
    except (OSError, ValueError, UnicodeError) as error:
        raise AccountError(
            "ACCOUNT_RECORDS_UNREADABLE",
            "Saved account records cannot be read. They were not changed; restore them before connecting."
        ) from error


def workspace_path(base, name):
    if not isinstance(name, str) or not WORKSPACE_PATTERN.fullmatch(name):
        raise RuntimeError("Invalid saved account workspace.")
    base = Path(base)
    if name == "default":
        return base
    profiles = base / "profiles"
    work = profiles / name
    if profiles.is_symlink() or work.is_symlink():
        raise RuntimeError("A saved account folder is a symbolic link. Existing data was not changed.")
    return work


def fingerprint(work):
    result = []
    for name in PRIVATE_FILES:
        path = work / name
        if not path.exists():
            result.append(None)
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        result.append(digest.hexdigest())
    return tuple(result)


def saved_token(work):
    path = work / "authorization.json"
    if not path.exists():
        return None
    try:
        token = json.loads(path.read_text(encoding="utf-8"))
        return token if isinstance(token, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def legacy_identity(work):
    token = saved_token(work)
    if not token:
        return None
    key = token.get("yotolio_account_key")
    if isinstance(key, str) and KEY_PATTERN.fullmatch(key):
        return key
    try:
        if token["obtained_at"] + token["expires_in"] <= time.time() + 60:
            return None
        return identify_account(token.get("access_token"))
    except (KeyError, TypeError, RuntimeError):
        return None


def account_plan(base, key, current):
    if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
        raise RuntimeError("The new login has no verified Yoto account. Nothing was changed.")
    base = Path(base)
    bindings = load_bindings(base)
    if key in bindings:
        name = bindings[key]
        work = workspace_path(base, name)
        if not work.is_dir():
            raise AccountError("ACCOUNT_HISTORY_MISSING",
                               "This account's saved history folder is missing. Restore it before connecting.")
        token = saved_token(work)
        if (work / "authorization.json").exists() and (
            not token or token.get("yotolio_account_key") != key
        ):
            raise AccountError(
                "ACCOUNT_LOGIN_CHANGED",
                "The saved login was changed outside Yotolio and cannot be safely replaced. "
                "The login and upload history were not changed."
            )
        return {"target": name, "fingerprint": None, "candidates": []}

    names = ["default"]
    profiles = base / "profiles"
    if profiles.exists():
        if profiles.is_symlink():
            raise RuntimeError("The saved account folder is a symbolic link. Existing data was not changed.")
        names.extend(sorted(
            path.name for path in profiles.iterdir()
            if not path.is_symlink() and path.is_dir() and WORKSPACE_PATTERN.fullmatch(path.name)
        ))
    names.sort(key=lambda name: (name != current, name != "default", name))
    claimed = set(bindings.values())
    matches, unknown = [], []
    for name in names:
        if name in claimed:
            continue
        work = workspace_path(base, name)
        if not any((work / item).exists() for item in PRIVATE_FILES):
            continue
        before = fingerprint(work)
        identity = legacy_identity(work)
        if before != fingerprint(work):
            raise AccountError("ACCOUNT_HISTORY_CHANGED",
                               "An older upload history changed while it was being checked. Connect again.")
        candidate = {"workspace": name, "fingerprint": before}
        if identity == key:
            matches.append(candidate)
        elif identity is None and any((work / item).exists() for item in PRIVATE_FILES[1:]):
            unknown.append(candidate)
    if len(matches) > 1:
        raise AccountError(
            "ACCOUNT_MULTIPLE_HISTORIES",
            "Multiple older histories belong to this Yoto account and cannot be safely combined automatically. "
            "All histories remain unchanged."
        )
    if matches:
        return {"target": matches[0]["workspace"], "fingerprint": matches[0]["fingerprint"], "candidates": []}
    return {"target": None, "fingerprint": None, "candidates": unknown}


def fresh_workspace(base, bindings):
    base = Path(base)
    if "default" not in bindings.values() and not any((base / name).exists() for name in PRIVATE_FILES):
        return "default"
    while True:
        name = "account-" + secrets.token_hex(16)
        if name not in bindings.values() and not workspace_path(base, name).exists():
            return name
