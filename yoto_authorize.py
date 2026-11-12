"""Human-controlled Yoto login using a loopback callback and PKCE S256."""

import argparse
import base64
import hashlib
import http.server
import json
import math
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from yoto_accounts import AccountIdentityError, NoRedirect, identify_account
from yoto_tls import CERTIFICATE_ERROR, certificate_error, configure_macos_trust
from yoto_upload import API, authorized_scopes, work_lock


DEFAULT_SCOPES = "openid user:content:view user:content:manage user:icons:manage"
AUTHORIZATION_RESPONSE_ERRORS = {
    "ACCOUNT_AUTH_RESPONSE_INVALID": "Yoto returned an unreadable login response.",
    "ACCOUNT_AUTH_TOKEN_INVALID": "Yoto returned an invalid login token.",
    "ACCOUNT_AUTH_EXPIRY_INVALID": "Yoto did not provide a usable authorization lifetime.",
    "ACCOUNT_AUTH_SCOPES_INSUFFICIENT": "Yoto did not grant the required permissions.",
    "ACCOUNT_AUTH_CONTENT_VIEW_DENIED": "Yoto refused to check your cards with this login.",
    "ACCOUNT_AUTH_ICON_ACCESS_DENIED": "Yoto refused to check your custom icons with this login.",
    "ACCOUNT_AUTH_SCOPE_CHECK_UNAVAILABLE": "Yoto permissions could not be checked.",
}

SCOPE_CHECKS = {
    "user:content:view": ("/content/mine", "ACCOUNT_AUTH_CONTENT_VIEW_DENIED"),
    "user:icons:manage": ("/media/displayIcons/user/me", "ACCOUNT_AUTH_ICON_ACCESS_DENIED"),
}


class AuthorizationResponseError(ValueError):
    def __init__(self, code):
        super().__init__(AUTHORIZATION_RESPONSE_ERRORS[code])
        self.code = code


def finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_authorization_response(token, required_scopes, requested_scope=None):
    if not isinstance(token, dict):
        raise AuthorizationResponseError("ACCOUNT_AUTH_RESPONSE_INVALID")
    if not isinstance(token.get("access_token"), str) or not token["access_token"].strip():
        raise AuthorizationResponseError("ACCOUNT_AUTH_TOKEN_INVALID")
    if not finite_number(token.get("expires_in")) or token["expires_in"] <= 60:
        raise AuthorizationResponseError("ACCOUNT_AUTH_EXPIRY_INVALID")
    if "scope" not in token:
        if requested_scope is None:
            raise AuthorizationResponseError("ACCOUNT_AUTH_RESPONSE_INVALID")
        token["scope"] = requested_scope
    if not isinstance(token["scope"], str):
        raise AuthorizationResponseError("ACCOUNT_AUTH_RESPONSE_INVALID")
    try:
        scopes = authorized_scopes(token)
    except ValueError:
        raise AuthorizationResponseError("ACCOUNT_AUTH_RESPONSE_INVALID") from None
    if not required_scopes.issubset(scopes):
        raise AuthorizationResponseError("ACCOUNT_AUTH_SCOPES_INSUFFICIENT")
    return token


def verify_implicit_scopes(token, required_scopes):
    missing = required_scopes.difference(token["scope"].split())
    if missing.difference(SCOPE_CHECKS):
        raise AuthorizationResponseError("ACCOUNT_AUTH_SCOPES_INSUFFICIENT")
    verified = []
    for scope in sorted(missing):
        route, denied = SCOPE_CHECKS[scope]
        request = urllib.request.Request(
            API + route,
            headers={"Authorization": "Bearer " + token["access_token"], "Accept": "application/json"},
        )
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if response.status != 200 or content_type != "application/json":
                    raise AuthorizationResponseError("ACCOUNT_AUTH_SCOPE_CHECK_UNAVAILABLE")
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            code = denied if status in {401, 403} else "ACCOUNT_AUTH_SCOPE_CHECK_UNAVAILABLE"
            raise AuthorizationResponseError(code) from None
        except OSError as error:
            if certificate_error(error):
                raise RuntimeError(CERTIFICATE_ERROR) from None
            raise AuthorizationResponseError("ACCOUNT_AUTH_SCOPE_CHECK_UNAVAILABLE") from None
        verified.append(scope)
    if verified:
        token["yotolio_verified_scopes"] = verified


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
                        help="Require verified custom-icon access before connecting")
    parser.add_argument("--identify-account", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--select-account", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.timeout <= 3600:
        parser.error("--port must be 1..65535 and --timeout must be 1..3600")
    configure_macos_trust()
    work = args.work_dir.expanduser().resolve()
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    token_file = work / "authorization.json"
    if token_file.exists() and not args.replace:
        parser.error("Authorization already exists; use --replace for a new login")
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(32)
    redirect_uri = f"http://127.0.0.1:{args.port}/callback"
    params = authorization_parameters(args.client_id, redirect_uri, verifier, state)
    if args.select_account:
        params["prompt"] = "select_account"
    auth_url = "https://login.yotoplay.com/authorize?" + urllib.parse.urlencode(params)
    result = {"done": False, "error": None}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def callback_failure(self, status):
            message = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Yotolio connection not completed</title>
<style>
body { background:#f8f6f1; color:#282524; font:18px/1.6 -apple-system,BlinkMacSystemFont,sans-serif;
       margin:0; padding:48px 24px; }
main { background:white; border:1px solid #e8dfd8; border-radius:18px; margin:auto; max-width:620px; padding:32px; }
h1 { color:#b84735; font-size:26px; line-height:1.3; margin-top:0; }
p { margin:16px 0; } .hint { color:#5f5752; font-size:16px; }
</style></head><body><main>
<h1>Could not finish connecting<br>无法完成连接</h1>
<p>Return to the Yotolio app or the terminal where you started setup to see the error and next steps.</p>
<p>请返回 Yotolio 应用或开始设置的终端，查看具体错误和下一步操作。</p>
<p class="hint">You can close this tab. / 你可以关闭这个页面。</p>
</main></body></html>""".encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(message)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(message)

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
                error = query["error"][0]
                if error == "invalid_scope":
                    error = ("ACCOUNT_AUTH_SCOPES_INSUFFICIENT" if args.identify_account
                             else AUTHORIZATION_RESPONSE_ERRORS["ACCOUNT_AUTH_SCOPES_INSUFFICIENT"])
                result.update(error=error, done=True)
                self.callback_failure(400)
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
                    try:
                        token = json.load(response)
                    except (ValueError, UnicodeError):
                        raise AuthorizationResponseError("ACCOUNT_AUTH_RESPONSE_INVALID") from None
                if isinstance(token, dict) and "yotolio_verified_scopes" in token:
                    raise AuthorizationResponseError("ACCOUNT_AUTH_RESPONSE_INVALID")
                required = {"user:content:view", "user:content:manage"}
                if args.icons_scope:
                    required.add("user:icons:manage")
                validate_authorization_response(token, {"user:content:manage"}, requested_scope=params["scope"])
                verify_implicit_scopes(token, required)
                validate_authorization_response(token, required, requested_scope=params["scope"])
                granted = token["scope"]
                token.update(obtained_at=time.time(), client_id=args.client_id)
                if args.identify_account:
                    id_token = token.pop("id_token", None)
                    token["yotolio_account_key"] = identify_account(
                        token["access_token"], client_id=args.client_id,
                        id_token=id_token, nonce=params["nonce"],
                    )
                with work_lock(work):
                    save_token(token_file, token, replace=args.replace)
                if args.identify_account:
                    message = (
                        "Yoto sign-in is complete. Return to Yotolio to finish connecting.\n"
                        "You may need to confirm earlier saved progress.\n\n"
                        "Yoto 登录已完成。请返回 Yotolio 完成连接，可能需要确认以前的上传进度。\n"
                        f"Permissions: {granted}"
                    ).encode()
                else:
                    message = (
                        "Yoto authorization complete.\n"
                        f"Permissions: {granted}\n"
                        "You may close this tab and return to the window where you started setup."
                    ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(message)
                print("AUTHORIZED: credentials saved privately; no token printed.", flush=True)
            except AccountIdentityError as error:
                result["error"] = error.code
                self.callback_failure(502)
            except urllib.error.HTTPError as error:
                result["error"] = f"Token exchange HTTP {error.code}; check app configuration"
                self.callback_failure(502)
            except AuthorizationResponseError as error:
                result["error"] = error.code if args.identify_account else f"Token exchange failed: {error}"
                self.callback_failure(502)
            except ValueError as error:
                result["error"] = ("ACCOUNT_AUTHORIZATION_UNUSABLE" if args.identify_account
                                   else f"Token exchange/retention failed: {error}")
                self.callback_failure(502)
            except (OSError, RuntimeError) as error:
                result["error"] = CERTIFICATE_ERROR if certificate_error(error) else f"Token exchange/retention failed: {error}"
                self.callback_failure(502)
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
