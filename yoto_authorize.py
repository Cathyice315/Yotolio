"""Human-controlled Yoto login using a loopback callback and PKCE S256."""

import argparse
import base64
import hashlib
import http.server
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_SCOPES = "openid user:content:view user:content:manage"


def authorization_parameters(client_id, redirect_uri, verifier, state):
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return {
        "audience": "https://api.yotoplay.com",
        "scope": DEFAULT_SCOPES,
        "response_type": "code",
        "response_mode": "query",
        "client_id": client_id,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "redirect_uri": redirect_uri,
        "state": state,
        "nonce": secrets.token_urlsafe(32),
    }


def save_token(path, token, replace=False):
    if not replace:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(token, stream)
        return
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(token, stream)
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", required=True, help="Your Yoto public client ID; not a password")
    parser.add_argument("--work-dir", type=Path, default=Path(".yoto-myo"))
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--timeout", type=int, default=900, help="Authorization window in seconds")
    parser.add_argument("--replace", action="store_true", help="Replace an expired authorization file")
    parser.add_argument("--icons-scope", action="store_true",
                        help="Also request user:icons:manage if your app/API requires it")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.timeout <= 3600:
        parser.error("--port must be 1..65535 and --timeout must be 1..3600")
    work = args.work_dir.expanduser().resolve()
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    token_file = work / "authorization.json"
    if token_file.exists() and not args.replace:
        parser.error("Authorization already exists; use --replace for a new login")
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(32)
    redirect_uri = f"http://127.0.0.1:{args.port}/callback"
    params = authorization_parameters(args.client_id, redirect_uri, verifier, state)
    if args.icons_scope:
        params["scope"] += " user:icons:manage"
    auth_url = "https://login.yotoplay.com/authorize?" + urllib.parse.urlencode(params)
    result = {"done": False, "error": None}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in {"/", "/start"}:
                self.send_response(302)
                self.send_header("Location", auth_url)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            if parsed.path == "/health":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ready")
                return
            if parsed.path != "/callback":
                self.send_error(404)
                return
            query = urllib.parse.parse_qs(parsed.query)
            if query.get("state") != [state]:
                self.send_error(400, "Invalid authorization state")
                return
            if "error" in query:
                result.update(error=query["error"][0], done=True)
                self.send_error(400, "Yoto authorization was not completed")
                return
            code = query.get("code", [None])[0]
            if not code:
                self.send_error(400, "Missing authorization code")
                return
            body = urllib.parse.urlencode({
                "grant_type": "authorization_code",
                "client_id": args.client_id,
                "code_verifier": verifier,
                "code": code,
                "redirect_uri": redirect_uri,
            }).encode()
            request = urllib.request.Request(
                "https://login.yotoplay.com/oauth/token", data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    token = json.load(response)
                if not token.get("access_token") or not token.get("expires_in"):
                    raise ValueError("Incomplete token response")
                granted = token.get("scope", "")
                required = {"user:content:view", "user:content:manage"}
                if not required.issubset(granted.split()):
                    raise ValueError("Required view/manage scopes were not granted")
                token.update(obtained_at=time.time(), client_id=args.client_id)
                save_token(token_file, token, replace=args.replace)
                message = (
                    "Yoto authorization complete.\n"
                    f"Permissions: {granted}\n"
                    "Return to your terminal. You may close this tab."
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(message)
                print("AUTHORIZED: credentials saved privately; no token printed.", flush=True)
            except urllib.error.HTTPError as error:
                result["error"] = f"Token exchange HTTP {error.code}; check app configuration"
                self.send_error(502, "Token exchange failed; return to your terminal")
            except (OSError, ValueError) as error:
                result["error"] = f"Token exchange/retention failed: {error}"
                self.send_error(502, "Authorization failed; return to your terminal")
            finally:
                result["done"] = True

    server = http.server.HTTPServer(("127.0.0.1", args.port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/health", timeout=5) as response:
            if response.read() != b"ready":
                raise RuntimeError("Callback server readiness check failed")
        print(f"Register this exact callback in Yoto: {redirect_uri}", flush=True)
        print(f"LOGIN_URL http://127.0.0.1:{args.port}/start", flush=True)
        deadline = time.monotonic() + args.timeout
        while not result["done"] and time.monotonic() < deadline:
            time.sleep(0.5)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    if not result["done"]:
        raise SystemExit("Authorization timed out; callback server stopped.")
    if result["error"]:
        raise SystemExit(result["error"])


if __name__ == "__main__":
    main()
