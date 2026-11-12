"""Local Yoto setup wizard for the native window or a browser."""

import argparse
import copy
import hashlib
import http.client
import http.server
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser

import yoto_accounts as accounts
from yoto_authorize import (
    AUTHORIZATION_RESPONSE_ERRORS, AuthorizationResponseError, finite_number,
    save_token, validate_authorization_response,
)
from yoto_discovery import (
    COVER_EXTENSIONS, IMAGE_EXTENSIONS, load_selections, placeholder_cover, selection_store,
)
from yoto_upload import authorized_scopes, work_lock


PROJECT = Path(__file__).resolve().parent
WEB = PROJECT / "web"
ACTIVE = {"running", "stopping"}
OPERATIONS = {"prepare", "scan", "auth", "upload", "existing", "verify", "inspect", "recreate"}
UI_API_VERSION = 12


class UIError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


class OutdatedServiceError(RuntimeError):
    pass


def write_private(path, value):
    temporary = path.with_suffix(".new")
    descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    temporary.replace(path)


def choose_folder():
    if sys.platform == "darwin":
        result = subprocess.run(
            ["osascript", "-e", 'POSIX path of (choose folder with prompt "Choose your Yoto collection folder")'],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode:
            if "(-128)" in result.stderr:
                return None
            raise UIError("PICKER_FAILED", "Folder picker failed. Paste the folder path instead.")
        return result.stdout.strip()
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        raise UIError("PICKER_FAILED", "This Python has no folder picker. Paste the folder path instead.") from None
    root = tkinter.Tk()
    root.withdraw()
    try:
        return filedialog.askdirectory(title="Choose your Yoto collection folder") or None
    finally:
        root.destroy()


def choose_image():
    if sys.platform == "darwin":
        result = subprocess.run(
            ["osascript", "-e", 'POSIX path of (choose file with prompt "Choose an image" of type {"public.image"})'],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode:
            if "(-128)" in result.stderr:
                return None
            raise UIError("PICKER_FAILED", "Image picker failed. Try again.")
        return result.stdout.strip()
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        raise UIError("PICKER_FAILED", "This Python has no image picker.") from None
    root = tkinter.Tk()
    root.withdraw()
    try:
        return filedialog.askopenfilename(
            title="Choose an image", filetypes=[("Images", "*.png *.jpg *.jpeg *.gif")]
        ) or None
    finally:
        root.destroy()


class App:
    def __init__(self, base):
        self.base = Path(base).resolve()
        self.base.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.config_file = self.base / "ui-config.json"
        self.config_present = self.config_file.exists()
        self.config = json.loads(self.config_file.read_text(encoding="utf-8")) if self.config_present else {
            "workspace": "default", "root": "", "client_id": "", "oauth_port": 8788, "icons_scope": False,
        }
        self.lock = threading.RLock()
        self.csrf = secrets.token_urlsafe(32)
        self.job = None
        self.last_upload = None
        self.last_inspection = None
        self.last_review = None
        self.review_identity = None
        self.scan_revision = 0
        self.process = None
        self.thread = None
        self.albums = []
        self.stop_requested = False
        self.closing = False
        self.pending_login = None

    @property
    def work(self):
        return accounts.workspace_path(self.base, self.config["workspace"])

    def busy(self):
        return self.job is not None and self.job["state"] in ACTIVE

    def require_idle(self):
        if self.closing:
            raise UIError("SHUTTING_DOWN", "The local service is shutting down.", 409)
        if self.busy():
            raise UIError("BUSY", "A task is running. Stop it and wait before changing settings.", 409)

    def require_resolved_account(self):
        if self.pending_login:
            raise UIError("ACCOUNT_CONFIRMATION_REQUIRED",
                          "Confirm or cancel the previous upload history before continuing.", 409)

    def save_config(self, value):
        with self.lock, work_lock(self.base, "account-bindings.lock"):
            self.require_idle()
            self.require_resolved_account()
            workspace = value.get("workspace", self.config["workspace"])
            if not isinstance(workspace, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", workspace):
                raise UIError("INVALID_WORKSPACE", "Use 1–40 lowercase letters, numbers, underscores or hyphens.")
            if workspace != self.config["workspace"]:
                raise UIError("ACCOUNT_CHANGED", "The Yoto account changed. Refresh and try again.", 409)
            self.require_current_config()
            root = value.get("root", "")
            client_id = value.get("client_id", "")
            if not isinstance(root, str) or not isinstance(client_id, str):
                raise UIError("INVALID_CONFIG", "Folder path and public client ID must be text.")
            root = str(Path(root.strip()).expanduser().resolve()) if root.strip() else ""
            if root and not Path(root).is_dir():
                raise UIError("INVALID_ROOT", "Choose an existing collection folder.")
            client_id = client_id.strip()
            if client_id and not re.fullmatch(r"[A-Za-z0-9_-]{4,200}", client_id):
                raise UIError("INVALID_CLIENT", "Paste only the public client ID, not a URL or secret.")
            port = value.get("oauth_port", 8788)
            if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
                raise UIError("INVALID_PORT", "Callback port must be an integer from 1024 to 65535.")
            icons_scope = value.get("icons_scope", False)
            if not isinstance(icons_scope, bool):
                raise UIError("INVALID_CONFIG", "Icon permission must be a checkbox value.")
            if root != self.config["root"] or workspace != self.config["workspace"]:
                self.albums = []
                self.job = None
            if (root, workspace, client_id) != (
                self.config["root"], self.config["workspace"], self.config["client_id"]
            ):
                self.last_upload = None
                self.last_inspection = None
                self.last_review = None
                if client_id != self.config["client_id"]:
                    self.job = None
            elif icons_scope != self.config["icons_scope"]:
                self.last_inspection = None
                self.last_review = None
            self.config = {
                "workspace": workspace, "root": root, "client_id": client_id,
                "oauth_port": port, "icons_scope": icons_scope,
            }
            self.work.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_private(self.config_file, self.config)
            self.config_present = True

    def require_current_config(self):
        if self.config_file.exists():
            saved = json.loads(self.config_file.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or saved.get("workspace") != self.config["workspace"]:
                raise UIError("ACCOUNT_CHANGED",
                              "The Yoto account changed in another window. Reopen this window.", 409)
            if any(saved.get(key) != self.config[key] for key in ("root", "client_id", "oauth_port", "icons_scope")):
                raise UIError("CONFIG_CHANGED",
                              "Settings changed in another window. Quit the app and reopen it.", 409)
        elif self.config_present:
            raise UIError("CONFIG_CHANGED", "Saved settings are missing. Quit the app and reopen it.", 409)

    def connection(self):
        path = self.work / "authorization.json"
        if not path.exists():
            return {"saved": False, "usable": False, "expiresAt": None, "scopes": []}
        try:
            token = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(token, dict) or not isinstance(token.get("scope", ""), str):
                raise ValueError("Invalid saved authorization")
            if not isinstance(token.get("access_token"), str) or not token["access_token"]:
                raise ValueError("Saved authorization has no access token")
            expires = token["obtained_at"] + token["expires_in"]
            scopes = token.get("scope", "").split()
            available = authorized_scopes(token)
            required = {"user:content:view", "user:content:manage"}
            if self.config["icons_scope"]:
                required.add("user:icons:manage")
            key = token.get("yotolio_account_key")
            verified = (
                isinstance(key, str) and accounts.KEY_PATTERN.fullmatch(key) is not None
                and accounts.load_bindings(self.base).get(key) == self.config["workspace"]
            )
            usable = (
                expires > time.time() + 60
                and required.issubset(available)
                and token.get("client_id") == self.config["client_id"]
                and verified
            )
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
            return {"saved": True, "usable": False, "expiresAt": None, "scopes": [],
                    "error": f"Saved authorization cannot be read: {type(error).__name__}"}
        return {"saved": True, "usable": usable, "expiresAt": expires, "scopes": scopes,
                "accountVerified": verified}

    def snapshot(self):
        with self.lock:
            albums = copy.deepcopy(self.albums)
            evidence_error = None
            state_path = self.work / "upload-state.json"
            if state_path.exists():
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    if not isinstance(state, dict) or not isinstance(state.get("albums"), dict):
                        raise ValueError("Invalid album evidence")
                    for album in albums:
                        recorded = state["albums"].get(album["title"], {})
                        if not isinstance(recorded, dict):
                            raise ValueError("Invalid album evidence")
                        ids = recorded.get("iconIds")
                        details = album["trackDetails"]
                        verified = recorded.get("status") == "verified"
                        if verified and (
                            not isinstance(ids, list) or len(ids) != len(details)
                            or any(icon is not None and (not isinstance(icon, str) or not icon) for icon in ids)
                        ):
                            raise ValueError("Invalid saved icon evidence")
                        album["hasEvidence"] = verified
                        album["uploadedPlaceholder"] = verified and bool(recorded.get("coverPlaceholder"))
                        album["pendingCoverUpdate"] = bool(recorded.get("pendingCoverUpdate"))
                        album["coverNeedsUpload"] = bool(
                            verified and recorded.get("coverPlaceholder") and album["cover"]
                        )
                        album["iconsNeedUpload"] = sum(
                            detail["hasIcon"] and not icon
                            for detail, icon in zip(details, ids, strict=True)
                        ) if verified and isinstance(ids, list) and len(ids) == len(details) else 0
                except (OSError, ValueError, TypeError) as error:
                    evidence_error = f"Saved upload evidence cannot be read: {type(error).__name__}"
            migration = None
            if self.pending_login:
                candidate = self.pending_login["candidates"][0]
                migration = {"id": self.pending_login["id"], "name": candidate["workspace"],
                             "remaining": len(self.pending_login["candidates"])}
            return {
                "config": copy.deepcopy(self.config), "connection": self.connection(),
                "job": copy.deepcopy(self.job), "albums": albums, "assetStatusError": evidence_error,
                "accountMigration": migration,
                "lastUpload": self.report(self.last_upload),
                "inspection": self.report(self.last_inspection),
                "reviewInspection": self.report(self.last_review),
                "scanRevision": self.scan_revision,
                "python": ".".join(map(str, sys.version_info[:3])),
                "platform": sys.platform,
            }

    @staticmethod
    def report(job):
        if not job:
            return None
        keys = ("id", "operation", "state", "requested", "completed", "skipped", "reviewSkipped", "error", "errorCode",
                "results", "sourceUploadId", "startedAt", "finishedAt", "scanRevision", "purpose")
        return copy.deepcopy({key: job[key] for key in keys if key in job})

    def commands(self, operation, album, albums=None):
        work = str(self.work)
        account_operations = {"upload", "existing", "verify", "inspect", "recreate"}
        if operation not in account_operations and (album is not None or albums is not None):
            raise UIError("INVALID_ALBUM", "Album selection is only available for upload or verification.")
        if operation == "auth":
            if not self.config["client_id"]:
                raise UIError("INVALID_CLIENT", "Enter your Yoto public client ID first.")
            command = [
                sys.executable, "-u", str(PROJECT / "yoto_authorize.py"),
                "--client-id", self.config["client_id"], "--work-dir", work,
                "--port", str(self.config["oauth_port"]), "--replace", "--identify-account",
            ]
            if self.config["icons_scope"]:
                command.append("--icons-scope")
            return [command]
        if not self.config["root"]:
            raise UIError("INVALID_ROOT", "Choose and save your collection folder first.")
        if operation in account_operations:
            if not self.connection()["usable"]:
                raise UIError("AUTH_REQUIRED", "Connect Yoto again; authorization is missing or expired.")
            if not self.albums:
                raise UIError("SCAN_REQUIRED", "Scan your collection before starting this task.")
        command = [
            sys.executable, "-u", str(PROJECT / "yoto_upload.py"),
            "--root", self.config["root"], "--work-dir", work,
        ]
        if album is not None and albums is not None:
            raise UIError("INVALID_ALBUM", "Choose either one album or a selection, not both.")
        if album is not None:
            albums = [album]
        if albums is not None:
            available = {item["title"] for item in self.albums}
            if (operation not in account_operations or not isinstance(albums, list)
                    or not albums or any(not isinstance(title, str) or title not in available for title in albums)
                    or len(set(albums)) != len(albums)):
                raise UIError("INVALID_ALBUM", "Choose one or more distinct scanned albums from the list.")
            if operation == "recreate" and len(albums) != 1:
                raise UIError("INVALID_ALBUM", "Recreate exactly one card at a time.")
            for title in albums:
                command += ["--album", title]
        elif operation in {"recreate", "inspect"}:
            raise UIError("INVALID_ALBUM", "Choose the cards to check or recreate.")
        if operation in account_operations:
            command += ["--stop-file", str(self.work / "ui-stop")]
        flags = {"prepare": "--plan", "scan": "--plan", "upload": "--create-only",
                 "existing": "--update-existing", "verify": "--verify-only",
                 "inspect": "--inspect-only", "recreate": "--recreate-missing"}
        if operation == "prepare":
            return [command + ["--plan"]]
        return [command + [flags[operation]]] if operation in flags else [command]

    def start(self, operation, album=None, albums=None, confirmed=False, recovery_job_id=None,
              switch_account=False, purpose=None, review_id=None, overwrite_titles=None, skipped_titles=None):
        with self.lock, work_lock(self.base, "account-bindings.lock"):
            self.require_idle()
            self.require_resolved_account()
            self.require_current_config()
            if operation not in OPERATIONS:
                raise UIError("INVALID_OPERATION", "Unknown task.")
            if not isinstance(switch_account, bool) or (switch_account and operation != "auth"):
                raise UIError("INVALID_OPERATION", "Account selection is only available when connecting.")
            if purpose not in (None, "review") or (purpose and operation != "inspect"):
                raise UIError("INVALID_OPERATION", "A review check is only available when inspecting cards.")
            if review_id is not None and (operation != "upload" or not isinstance(review_id, str)):
                raise UIError("INVALID_REVIEW", "Check the cards again before uploading.", 409)
            if operation == "upload" and review_id is None:
                raise UIError("INVALID_REVIEW", "Check the cards again before uploading.", 409)
            if overwrite_titles is None:
                overwrite_titles = []
            if (not isinstance(overwrite_titles, list) or any(not isinstance(title, str) for title in overwrite_titles)
                    or len(set(overwrite_titles)) != len(overwrite_titles)
                    or (overwrite_titles and (operation != "upload" or review_id is None))):
                raise UIError("INVALID_REVIEW", "Choose cards from the current review before uploading.", 409)
            if skipped_titles is None:
                skipped_titles = []
            if (not isinstance(skipped_titles, list) or any(not isinstance(title, str) for title in skipped_titles)
                    or len(set(skipped_titles)) != len(skipped_titles)
                    or (skipped_titles and (operation != "upload" or review_id is None))):
                raise UIError("INVALID_REVIEW", "Choose cards from the current review before uploading.", 409)
            if operation == "recreate":
                previous = self.job or {}
                recovery = previous.get("recovery") or {}
                if (confirmed is not True or albums is not None or not isinstance(album, str)
                        or not isinstance(recovery_job_id, str) or previous.get("id") != recovery_job_id
                        or previous.get("operation") != "upload" or previous.get("state") != "failed"
                        or recovery.get("type") != "missing_prior_card" or recovery.get("title") != album
                        or recovery.get("clientId") != self.config["client_id"]):
                    raise UIError("RECREATE_CONFIRMATION_REQUIRED",
                                  "Confirm the current missing card after checking the same Yoto account.", 409)
            if (operation in {"inspect", "verify", "scan", "prepare"}
                    and self.job and self.job.get("operation") == "upload"
                    and self.job.get("state") == "failed" and self.job.get("recovery")):
                raise UIError("RECOVERY_REQUIRED",
                              "Resolve the missing-card confirmation in Review & upload first.", 409)
            commands = self.commands(operation, album) if albums is None else self.commands(operation, album, albums)
            if operation == "upload" and review_id is not None:
                review = self.last_review or {}
                selected = albums if albums is not None else ([album] if album is not None else None)
                try:
                    token = json.loads((self.work / "authorization.json").read_text(encoding="utf-8"))
                    identity = (self.config["workspace"], token.get("yotolio_account_key"))
                except (OSError, ValueError, TypeError, AttributeError):
                    raise UIError("INVALID_REVIEW", "The saved account changed. Check the cards again.", 409) from None
                if (not isinstance(selected, list) or not selected or review.get("id") != review_id
                        or review.get("state") != "completed" or review.get("scanRevision") != self.scan_revision
                        or self.review_identity != identity or not set(overwrite_titles).issubset(selected)
                        or (overwrite_titles and confirmed is not True)):
                    raise UIError("INVALID_REVIEW",
                                  "The review or account changed. Check the cards again before uploading.", 409)
                results = {item["title"]: item for item in review.get("results", [])}
                if set(skipped_titles) & set(selected) or not set(skipped_titles).issubset(results):
                    raise UIError("INVALID_REVIEW", "The skipped cards are not from the current review.", 409)
                plan = []
                for title in selected:
                    result = results.get(title)
                    if not result or result.get("error") is not None or result.get("presence") not in {"missing", "present"}:
                        raise UIError("INVALID_REVIEW", "A selected card could not be checked. Check it again.", 409)
                    if result["presence"] == "present":
                        if title not in overwrite_titles:
                            raise UIError("INVALID_REVIEW", "Existing cards must be skipped or explicitly selected.", 409)
                        action = "resume" if result.get("pendingCreate") else "replace"
                    else:
                        if title in overwrite_titles or result.get("pendingOverwrite"):
                            raise UIError("INVALID_REVIEW", "The selected card no longer has a matching Yoto card.", 409)
                        action = "create"
                    plan.append({
                        "title": title, "action": action, "localFingerprint": result["localFingerprint"],
                        "remoteFingerprint": result.get("remoteFingerprint"),
                    })
                commands[0] += ["--review-plan", json.dumps(plan, ensure_ascii=False, separators=(",", ":"))]
            self.work.mkdir(mode=0o700, parents=True, exist_ok=True)
            if operation in {"upload", "existing", "verify", "inspect", "recreate"}:
                with work_lock(self.work):
                    (self.work / "ui-stop").unlink(missing_ok=True)
            auth_stage = None
            if operation == "auth":
                auth_stage = tempfile.TemporaryDirectory(prefix=".yotolio-login-", dir=self.base)
                if "--work-dir" in commands[0]:
                    commands[0][commands[0].index("--work-dir") + 1] = auth_stage.name
                if switch_account:
                    commands[0].append("--select-account")
            self.stop_requested = False
            self.job = {
                "id": secrets.token_hex(8), "operation": operation, "state": "running",
                "logs": [], "error": None, "loginUrl": None, "phase": operation,
                "completed": [], "skipped": [], "currentAlbum": "", "audioDone": 0, "audioTotal": 0,
                "total": len(albums) if albums is not None else (1 if album is not None else len(self.albums)),
                "startedAt": time.time(),
            }
            if operation in {"upload", "existing", "recreate", "inspect"}:
                self.job["requested"] = list(albums) if albums is not None else (
                    [album] if album is not None else [item["title"] for item in self.albums]
                )
            if operation in {"upload", "existing", "recreate"}:
                if operation == "upload":
                    self.job["reviewSkipped"] = list(skipped_titles)
                self.last_upload = self.job
                self.last_inspection = None
                self.last_review = None
            if operation == "inspect":
                self.job["results"] = []
                upload_titles = (set(self.last_upload.get("requested", []))
                                 | set(self.last_upload.get("reviewSkipped", []))) if self.last_upload else set()
                self.job["sourceUploadId"] = (
                    self.last_upload["id"] if self.last_upload
                    and set(self.job["requested"]).issubset(upload_titles) else None
                )
                if purpose == "review":
                    token = json.loads((self.work / "authorization.json").read_text(encoding="utf-8"))
                    self.review_identity = (self.config["workspace"], token["yotolio_account_key"])
                    self.job.update(purpose="review", scanRevision=self.scan_revision, sourceUploadId=None)
                    self.last_review = self.job
                else:
                    self.last_inspection = self.job
            if operation in {"prepare", "scan"}:
                self.albums = []
                self.last_inspection = None
                self.last_review = None
                self.scan_revision += 1
            if operation == "auth":
                self.last_review = None
            self.thread = threading.Thread(target=self.run, args=(commands, auth_stage), daemon=True)
            self.thread.start()
            return copy.deepcopy(self.job)

    def record(self, line):
        with self.lock:
            self.job["logs"].append(line)
            self.job["logs"] = self.job["logs"][-500:]
            if line.startswith("RECOVERY_REQUIRED "):
                recovery = json.loads(line.removeprefix("RECOVERY_REQUIRED "))
                if (self.job["operation"] != "upload" or not isinstance(recovery, dict)
                        or recovery.get("type") != "missing_prior_card"
                        or not isinstance(recovery.get("priorStatus"), str)
                        or recovery.get("priorStatus") not in {"pending_create", "created", "verified"}
                        or not isinstance(recovery.get("title"), str)
                        or recovery.get("title") not in {album["title"] for album in self.albums}):
                    raise ValueError("Invalid card recovery evidence")
                recovery["clientId"] = self.config["client_id"]
                self.job["recovery"] = recovery
            if line.startswith("{") and (self.job["operation"] in {"scan", "prepare"}
                                          or (self.job["operation"] == "auth"
                                              and self.job["phase"] == "account-scan")):
                album = json.loads(line)
                if not isinstance(album, dict) or not all(key in album for key in (
                    "title", "tracks", "icons", "cover", "coverStatus", "coverCandidates",
                    "source", "audioIssue", "trackDetails", "unmatchedIcons",
                )):
                    raise ValueError("Invalid collection scan output")
                if (
                    not isinstance(album["title"], str) or not album["title"]
                    or (album["cover"] is not None and not isinstance(album["cover"], str))
                    or type(album["tracks"]) is not int or type(album["icons"]) is not int
                    or album["tracks"] < 0 or not 0 <= album["icons"] <= album["tracks"]
                    or album["coverStatus"] not in ("provided", "missing", "ambiguous", "selected_missing")
                    or (album["coverStatus"] == "provided") != bool(album["cover"])
                    or not isinstance(album["source"], str)
                    or not isinstance(album["coverCandidates"], list)
                    or not all(isinstance(item, str) for item in album["coverCandidates"])
                    or not isinstance(album["unmatchedIcons"], list)
                    or not all(isinstance(item, str) for item in album["unmatchedIcons"])
                    or album["audioIssue"] not in (None, "missing", "too_many", "untagged_in_mixed_folder")
                    or not isinstance(album["trackDetails"], list)
                    or len(album["trackDetails"]) != album["tracks"]
                ):
                    raise ValueError("Invalid collection counts")
                for track in album["trackDetails"]:
                    if (not isinstance(track, dict) or not isinstance(track.get("name"), str)
                            or not isinstance(track.get("source"), str) or type(track.get("hasIcon")) is not bool
                            or (track.get("icon") is not None and not isinstance(track.get("icon"), str))
                            or track.get("issue") not in (None, "missing", "ambiguous", "selected_missing")
                            or not isinstance(track.get("candidates"), list)
                            or not all(isinstance(item, str) for item in track["candidates"])
                            or track["hasIcon"] != (track["issue"] is None)):
                        raise ValueError("Invalid track scan output")
                if sum(track["hasIcon"] for track in album["trackDetails"]) != album["icons"]:
                    raise ValueError("Invalid icon scan count")
                self.albums.append(album)
            if line.startswith("INSPECTED "):
                result = json.loads(line.removeprefix("INSPECTED "))
                if (self.job["operation"] != "inspect" or not isinstance(result, dict)
                        or result.get("title") not in self.job["requested"]
                        or result.get("title") in {item["title"] for item in self.job["results"]}
                        or result.get("presence") not in {"present", "missing", "duplicate"}
                        or result.get("cover") not in {"matched", "placeholder", "different", "missing", "unverified"}
                        or (result.get("remoteTracks") is not None
                            and (type(result["remoteTracks"]) is not int or result["remoteTracks"] < 0))
                        or not isinstance(result.get("tracks"), list)
                        or (result.get("error") is not None and not isinstance(result["error"], str))):
                    raise ValueError("Invalid read-only card result")
                if self.job.get("purpose") == "review" and result.get("error") is None:
                    if (not isinstance(result.get("localFingerprint"), str)
                            or not re.fullmatch(r"[a-f0-9]{64}", result["localFingerprint"])
                            or type(result.get("pendingCreate")) is not bool
                            or type(result.get("pendingOverwrite")) is not bool):
                        raise ValueError("Invalid review source evidence")
                    if result["presence"] == "present" and (
                        any(type(result.get(key)) is not int or result[key] < 0
                            for key in ("remoteTracks", "remoteIcons", "remoteCovers"))
                        or result["remoteIcons"] > result["remoteTracks"] or result["remoteCovers"] > 1
                        or not isinstance(result.get("remoteFingerprint"), str)
                        or not re.fullmatch(r"[a-f0-9]{64}", result["remoteFingerprint"])
                    ):
                        raise ValueError("Invalid review card evidence")
                for track in result["tracks"]:
                    if (not isinstance(track, dict) or not isinstance(track.get("name"), str)
                            or (track.get("remoteTitle") is not None and not isinstance(track["remoteTitle"], str))
                            or track.get("audio") not in {"matched", "different", "missing", "extra", "name_only",
                                                           "name_mismatch", "unverified"}
                            or track.get("icon") not in {"matched", "different", "missing", "extra", "partial",
                                                         "remote_only", "song_mismatch", "unverified"}):
                        raise ValueError("Invalid read-only track result")
                self.job["results"].append(result)
                self.job["completed"].append(result["title"])
                self.job.update(currentAlbum=result["title"], phase="inspected")
            if line.startswith("LOGIN_URL "):
                expected = f"http://127.0.0.1:{self.config['oauth_port']}/start"
                if line.removeprefix("LOGIN_URL ") != expected:
                    raise ValueError("Unexpected login URL")
                self.job["loginUrl"] = expected
            start = re.match(r"START (.*): (\d+) tracks$", line)
            audio = re.match(r"AUDIO (.*): (\d+)/(\d+)$", line)
            done = re.match(r"(?:VERIFIED|EXISTING_ICONS_VERIFIED|AUDITED) (.*): (?:card=|\d+)", line)
            if start:
                self.job.update(currentAlbum=start[1], audioDone=0, audioTotal=int(start[2]), phase="audio")
            if audio:
                self.job.update(currentAlbum=audio[1], audioDone=int(audio[2]), audioTotal=int(audio[3]), phase="audio")
            if line.startswith("COVER_UPLOADED "):
                self.job["phase"] = "cover"
            if line.startswith("ICONS_UPLOADED "):
                self.job["phase"] = "icons"
            if line.startswith("SAVING "):
                self.job["phase"] = "saving"
            if done and done[1] not in self.job["completed"]:
                self.job["completed"].append(done[1])
                self.job.update(currentAlbum=done[1], phase="verified")
            final = re.match(r"FINISHED: (\d+) requested albums", line)
            if final and self.job["operation"] == "verify":
                self.job["total"] = int(final[1])
            selected = re.match(r"SELECTED: (\d+) cards$", line)
            if selected and self.job["operation"] == "verify":
                self.job["total"] = int(selected[1])
            skipped = re.match(
                r"SKIPPED (.*): (missing|too_many|untagged_in_mixed_folder|not_existing|already_existing)$", line
            )
            if skipped:
                self.job["skipped"].append({"title": skipped[1], "reason": skipped[2]})

    def run_commands(self, commands):
        for command in commands:
            with self.lock:
                if self.stop_requested:
                    break
                environment = dict(os.environ, PYTHONIOENCODING="utf-8")
                self.process = subprocess.Popen(
                    command, cwd=PROJECT, env=environment, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", bufsize=1,
                )
                process = self.process
            for line in process.stdout:
                self.record(line.rstrip())
            process.stdout.close()
            code = process.wait()
            with self.lock:
                self.process = None
                if code and not self.stop_requested:
                    detail = next((line for line in reversed(self.job["logs"]) if line.strip()), "Task failed")
                    raise RuntimeError(detail)

    def staged_login(self, stage):
        try:
            token = json.loads((Path(stage.name) / "authorization.json").read_text(encoding="utf-8"))
            if not isinstance(token, dict):
                raise ValueError("Invalid authorization")
            key = token.get("yotolio_account_key")
            required = {"user:content:view", "user:content:manage"}
            if self.config["icons_scope"]:
                required.add("user:icons:manage")
            obtained, lifetime = token.get("obtained_at"), token.get("expires_in")
            validate_authorization_response(token, required)
            if (not finite_number(obtained) or not finite_number(obtained + lifetime)
                    or obtained + lifetime <= time.time() + 60):
                raise AuthorizationResponseError("ACCOUNT_AUTH_EXPIRY_INVALID")
            if (not isinstance(key, str)
                    or not accounts.KEY_PATTERN.fullmatch(key)
                    or token.get("client_id") != self.config["client_id"]):
                raise ValueError("Invalid authorization")
            return token
        except AuthorizationResponseError as error:
            raise accounts.AccountError(error.code, str(error)) from None
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise accounts.AccountError(
                "ACCOUNT_AUTHORIZATION_UNUSABLE",
                "Login could not be saved. Check the client ID, permissions and expiry; existing data was not changed.",
            ) from None

    def activate_login(self, token, target, expected_fingerprint):
        key = token["yotolio_account_key"]
        with self.lock:
            if self.stop_requested:
                return []
            if token["obtained_at"] + token["expires_in"] <= time.time() + 60:
                raise RuntimeError("The new login expired before it could be saved. Connect again.")
            with work_lock(self.base, "account-bindings.lock"):
                self.require_current_config()
                bindings = accounts.load_bindings(self.base)
                if target is None:
                    target = accounts.fresh_workspace(self.base, bindings)
                work = accounts.workspace_path(self.base, target)
                if key in bindings and bindings[key] != target:
                    raise RuntimeError("The account changed in another window. Connect again.")
                if key not in bindings and target in bindings.values():
                    raise RuntimeError("An older history was assigned in another window. Connect again.")
                if key in bindings and not work.is_dir():
                    raise accounts.AccountError("ACCOUNT_HISTORY_MISSING",
                                                "This account's saved history folder is missing. Restore it before connecting.")
                work.mkdir(mode=0o700, parents=True, exist_ok=True)
                with work_lock(work):
                    if key in bindings:
                        existing = accounts.saved_token(work)
                        if (work / "authorization.json").exists() and (
                            not existing or existing.get("yotolio_account_key") != key
                        ):
                            raise accounts.AccountError(
                                "ACCOUNT_LOGIN_CHANGED",
                                "The saved login changed outside Yotolio. Existing account data was not changed."
                            )
                    elif expected_fingerprint is not None:
                        if accounts.fingerprint(work) != expected_fingerprint:
                            raise accounts.AccountError("ACCOUNT_HISTORY_CHANGED",
                                                        "The older history changed while awaiting confirmation. Connect again.")
                    elif any((work / name).exists() for name in accounts.PRIVATE_FILES):
                        raise RuntimeError("The new account folder is no longer empty. Connect again.")
                    save_token(work / "authorization.json", token, replace=True)
                    bindings[key] = target
                    write_private(self.base / "account-bindings.json", {"version": 1, "accounts": bindings})
                    config = dict(self.config, workspace=target)
                    write_private(self.config_file, config)
                    self.config_present = True
                    changed = target != self.config["workspace"]
                    self.config = config
            if changed:
                self.last_upload = None
                self.last_inspection = None
                self.last_review = None
            if changed and self.albums:
                self.albums = []
                self.scan_revision += 1
                self.job["phase"] = "account-scan"
                return self.commands("scan", None)
            return []

    def prepare_login(self, token):
        plan = accounts.account_plan(self.base, token["yotolio_account_key"], self.config["workspace"])
        with self.lock:
            if self.stop_requested:
                return []
            if plan["candidates"]:
                self.pending_login = {"token": token, "candidates": plan["candidates"],
                                      "id": secrets.token_urlsafe(24)}
                self.job["phase"] = "account-confirmation"
                return []
        return self.activate_login(token, plan["target"], plan["fingerprint"])

    def resolve_account(self, choice_id, choice):
        with self.lock:
            self.require_idle()
            pending = self.pending_login
            if (not pending or not isinstance(choice_id, str) or choice_id != pending["id"]
                    or choice not in {"same", "different", "cancel"}):
                raise UIError("ACCOUNT_CONFIRMATION_EXPIRED", "The account confirmation changed. Connect again.", 409)
            if choice == "cancel":
                self.pending_login = None
                self.job.update(state="stopped", phase="auth", finishedAt=time.time())
                return
            candidate = pending["candidates"].pop(0)
            if choice == "different" and pending["candidates"]:
                pending["id"] = secrets.token_urlsafe(24)
                return
            target = candidate["workspace"] if choice == "same" else None
            expected = candidate["fingerprint"] if choice == "same" else None
            token = pending["token"]
            self.pending_login = None
            self.stop_requested = False
            self.job.update(state="running", phase="auth", error=None)
            self.thread = threading.Thread(target=self.run, args=([], None, (token, target, expected)), daemon=True)
            self.thread.start()

    def run(self, commands, auth_stage=None, continuation=None):
        terminal = "failed"
        detail = None
        error_code = None
        try:
            self.run_commands(commands)
            if not self.stop_requested and self.job["operation"] == "auth":
                with self.lock:
                    self.job["phase"] = "account-identifying"
                scan = (self.activate_login(*continuation) if continuation
                        else self.prepare_login(self.staged_login(auth_stage)))
                self.run_commands(scan)
            with self.lock:
                if (not self.stop_requested and self.job["operation"] == "auth"
                        and not self.pending_login and not self.connection()["usable"]):
                    raise RuntimeError("Login ended without usable saved authorization. Connect again.")
                if not self.stop_requested and self.job["operation"] in {"upload", "existing", "recreate", "inspect"}:
                    finished = set(self.job["completed"]) | {item["title"] for item in self.job["skipped"]}
                    if finished != set(self.job["requested"]):
                        raise RuntimeError("The task ended without results for every selected card.")
                terminal = "stopped" if self.stop_requested else "completed"
                if not self.stop_requested and self.job["operation"] == "auth" and not self.pending_login:
                    self.job["phase"] = "connected"
                if not self.stop_requested and self.job["operation"] in {"prepare", "scan"}:
                    self.job["phase"] = "prepared"
        except (OSError, RuntimeError, ValueError, UIError) as error:
            process = self.process
            if process:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                process.stdout.close()
            detail = str(error)
            error_code = getattr(error, "code", None)
            if detail in {*accounts.IDENTITY_ERRORS, "ACCOUNT_AUTHORIZATION_UNUSABLE",
                          *AUTHORIZATION_RESPONSE_ERRORS}:
                error_code = detail
            elif detail.startswith("RuntimeError: OVERWRITE_UNCONFIRMED: "):
                error_code = "OVERWRITE_UNCONFIRMED"
            elif detail.startswith("RuntimeError: OVERWRITE_CHANGED: "):
                error_code = "OVERWRITE_CHANGED"
            print(f"UI task failed: {error}", file=sys.stderr, flush=True)
        finally:
            if auth_stage:
                try:
                    auth_stage.cleanup()
                except OSError as error:
                    terminal = "failed"
                    detail = f"Could not remove temporary login data: {type(error).__name__}"
            with self.lock:
                self.job.update(state=terminal, error=detail, errorCode=error_code)
                if detail:
                    self.job["logs"].append(f"UI_TASK_FAILED: {detail}")
                if (self.job["operation"] in {"scan", "prepare"}
                        or (self.job["operation"] == "auth" and self.job["phase"] == "account-scan")) and terminal != "completed":
                    self.albums = []
                self.job["finishedAt"] = time.time()
                self.process = None

    def stop(self):
        with self.lock:
            if not self.busy():
                raise UIError("NOT_RUNNING", "No task is running.", 409)
            if self.job["operation"] in {"upload", "existing", "verify", "inspect", "recreate"}:
                (self.work / "ui-stop").touch(mode=0o600)
            elif self.process and self.process.poll() is None:
                self.process.terminate()
            self.stop_requested = True
            self.job["state"] = "stopping"

    def disconnect(self):
        with self.lock, work_lock(self.base, "account-bindings.lock"):
            self.require_idle()
            self.require_resolved_account()
            self.require_current_config()
            with work_lock(self.work):
                (self.work / "authorization.json").unlink(missing_ok=True)
            self.last_upload = None
            self.last_inspection = None
            self.last_review = None

    def select_asset(self, title, kind, track_index, selected_path=None):
        with self.lock, work_lock(self.base, "account-bindings.lock"):
            self.require_idle()
            self.require_resolved_account()
            self.require_current_config()
            if (self.job and self.job.get("operation") == "upload"
                    and self.job.get("state") == "failed" and self.job.get("recovery")):
                raise UIError("RECOVERY_REQUIRED",
                              "Resolve the missing-card confirmation in Review & upload first.", 409)
            matches = [album for album in self.albums if album["title"] == title]
            if len(matches) != 1 or kind not in {"cover", "icon"}:
                raise UIError("INVALID_ASSET", "Choose an asset from the current scan.")
            if kind == "icon":
                if type(track_index) is not int or not 0 <= track_index < matches[0]["tracks"]:
                    raise UIError("INVALID_ASSET", "Choose a track from the current scan.")
                source = matches[0]["trackDetails"][track_index]["source"]
            else:
                source = None
            root, work = self.config["root"], self.work
        if selected_path is not None and not isinstance(selected_path, str):
            raise UIError("INVALID_ASSET", "Choose a supported image.")
        if selected_path is not None and not selected_path.strip():
            raise UIError("INVALID_ASSET", "Choose a supported image.")
        chosen = selected_path if selected_path is not None else choose_image()
        if not chosen:
            return False
        path = Path(chosen).expanduser().resolve()
        allowed = COVER_EXTENSIONS if kind == "cover" else IMAGE_EXTENSIONS
        if not path.is_file() or path.suffix.casefold() not in allowed or path.stat().st_size > 20_000_000:
            raise UIError("INVALID_ASSET", "Choose a supported image under 20 MB.")
        with self.lock, work_lock(self.base, "account-bindings.lock"):
            self.require_idle()
            self.require_resolved_account()
            self.require_current_config()
            if (self.job and self.job.get("operation") == "upload"
                    and self.job.get("state") == "failed" and self.job.get("recovery")):
                raise UIError("RECOVERY_REQUIRED",
                              "Resolve the missing-card confirmation in Review & upload first.", 409)
            if self.config["root"] != root or self.work != work:
                raise UIError("INVALID_ASSET", "The collection changed while the picker was open; try again.")
            matches = [album for album in self.albums if album["title"] == title]
            if (len(matches) != 1 or (kind == "icon"
                    and (track_index >= len(matches[0]["trackDetails"])
                         or matches[0]["trackDetails"][track_index]["source"] != source))):
                raise UIError("INVALID_ASSET", "The scan changed while the picker was open; try again.")
            with work_lock(work):
                store = selection_store(self.work)
                selections = store["collections"].setdefault(root, {})
                if not isinstance(selections, dict):
                    raise UIError("INVALID_ASSET", "Saved collection selections are invalid.")
                selection = selections.setdefault(title, {})
                if not isinstance(selection, dict):
                    raise UIError("INVALID_ASSET", "Saved asset selections are invalid.")
                if kind == "cover":
                    selection["cover"] = str(path)
                else:
                    icons = selection.setdefault("icons", {})
                    if not isinstance(icons, dict):
                        raise UIError("INVALID_ASSET", "Saved icon selections are invalid.")
                    icons[source] = str(path)
                write_private(self.work / "asset-selections.json", store)
                self.last_review = None
        return True

    def close(self):
        with self.lock:
            self.stop_requested = True
            process = self.process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if self.thread:
            self.thread.join(timeout=10)


def handler_for(app):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def valid_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def send(self, code, data, mime="application/json; charset=utf-8"):
            if isinstance(data, dict):
                data = json.dumps(data, ensure_ascii=False).encode()
            elif isinstance(data, str):
                data = data.encode()
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self'; style-src 'self'; "
                             "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self.valid_host():
                self.send(403, {"error": "Invalid local host"})
                return
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/status":
                self.send(200, app.snapshot())
            elif parsed.path == "/api/bootstrap":
                self.send(200, {"csrf": app.csrf})
            elif parsed.path == "/api/health":
                self.send(200, {"app": "yoto-myo-ui", "ready": True,
                                "workspace": workspace_identity(app.base), "apiVersion": UI_API_VERSION})
            elif parsed.path == "/api/cover":
                try:
                    index = int(urllib.parse.parse_qs(parsed.query).get("index", ["-1"])[0])
                    with app.lock:
                        if not 0 <= index < len(app.albums):
                            raise ValueError("Unknown album")
                        album = copy.deepcopy(app.albums[index])
                        root = Path(app.config["root"]).resolve()
                        if album["cover"] is None:
                            self.send(200, placeholder_cover(album["title"]), "image/png")
                            return
                        path = Path(album["cover"]).resolve()
                        if not path.is_relative_to(root):
                            selections = load_selections(app.work, root)
                            chosen = selections.get(album["title"], {}).get("cover")
                            if not isinstance(chosen, str) or Path(chosen).resolve() != path:
                                raise ValueError("Cover was not selected")
                    mime = mimetypes.guess_type(path.name)[0]
                    if mime not in {"image/jpeg", "image/png"} or path.stat().st_size > 20_000_000:
                        raise ValueError("Unsupported cover image")
                    self.send(200, path.read_bytes(), mime)
                except (ValueError, OSError, RuntimeError, TypeError):
                    self.send(404, {"error": "Cover is unavailable"})
            elif parsed.path == "/api/icon":
                try:
                    query = urllib.parse.parse_qs(parsed.query)
                    index = int(query.get("index", ["-1"])[0])
                    track_index = int(query.get("track", ["-1"])[0])
                    with app.lock:
                        if not 0 <= index < len(app.albums):
                            raise ValueError("Unknown album")
                        album = copy.deepcopy(app.albums[index])
                        if not 0 <= track_index < len(album["trackDetails"]):
                            raise ValueError("Unknown track")
                        track = album["trackDetails"][track_index]
                        if not isinstance(track.get("icon"), str):
                            raise ValueError("No matching icon")
                        path = Path(track["icon"]).resolve()
                        root = Path(app.config["root"]).resolve()
                        if not path.is_relative_to(root):
                            selections = load_selections(app.work, root)
                            chosen = selections.get(album["title"], {}).get("icons", {}).get(track["source"])
                            if not isinstance(chosen, str) or Path(chosen).resolve() != path:
                                raise ValueError("Icon was not selected for this track")
                    mime = mimetypes.guess_type(path.name)[0]
                    if mime not in {"image/jpeg", "image/png", "image/gif"} or path.stat().st_size > 20_000_000:
                        raise ValueError("Unsupported icon image")
                    self.send(200, path.read_bytes(), mime)
                except (KeyError, ValueError, OSError, RuntimeError, TypeError):
                    self.send(404, {"error": "Icon is unavailable"})
            elif parsed.path in {"/", "/app.js", "/i18n.mjs", "/styles.css", "/icon.svg",
                                 "/myo-placeholder.svg", "/setup-create.png",
                                 "/setup-callback.png", "/setup-scopes.png"}:
                name = "index.html" if parsed.path == "/" else parsed.path[1:]
                mime = {"index.html": "text/html", "app.js": "text/javascript",
                        "i18n.mjs": "text/javascript", "styles.css": "text/css",
                        "icon.svg": "image/svg+xml", "myo-placeholder.svg": "image/svg+xml",
                        "setup-create.png": "image/png",
                        "setup-callback.png": "image/png", "setup-scopes.png": "image/png"}[name]
                self.send(200, (WEB / name).read_bytes(), mime if mime == "image/png" else mime + "; charset=utf-8")
            else:
                self.send(404, {"error": "Not found"})

        def do_POST(self):
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if (
                not self.valid_host()
                or self.headers.get("X-Yoto-CSRF") != app.csrf
                or self.headers.get("Origin") not in {None, origin}
            ):
                self.send(403, {"code": "FORBIDDEN", "error": "Request must come from this local UI"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384:
                    raise UIError("INVALID_REQUEST", "Request body must be 1..16384 bytes.")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise UIError("INVALID_REQUEST", "Request must be a JSON object.")
                if self.path == "/api/config":
                    app.save_config(body)
                elif self.path == "/api/folder":
                    with app.lock:
                        app.require_idle()
                        app.require_resolved_account()
                    self.send(200, {"path": choose_folder()})
                    return
                elif self.path == "/api/asset":
                    self.send(200, {"chosen": app.select_asset(
                        body.get("album"), body.get("kind"), body.get("track"), body.get("path"),
                    )})
                    return
                elif self.path == "/api/jobs":
                    self.send(202, {"job": app.start(
                        body.get("operation"), body.get("album"), body.get("albums"),
                        body.get("confirmed", False), body.get("recoveryJobId"), body.get("switchAccount", False),
                        body.get("purpose"), body.get("reviewId"), body.get("overwriteTitles"),
                        body.get("skippedTitles"),
                    )})
                    return
                elif self.path == "/api/account-migration":
                    app.resolve_account(body.get("id"), body.get("choice"))
                elif self.path == "/api/stop":
                    app.stop()
                elif self.path == "/api/disconnect":
                    app.disconnect()
                elif self.path == "/api/quit":
                    with app.lock:
                        app.require_idle()
                        app.closing = True
                    try:
                        self.send(200, {"ok": True})
                    finally:
                        threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                else:
                    raise UIError("NOT_FOUND", "Unknown action.", 404)
                self.send(200, {"ok": True})
            except UIError as error:
                self.send(error.status, {"code": error.code, "error": str(error)})
            except (OSError, RuntimeError, ValueError, TypeError, subprocess.TimeoutExpired) as error:
                print(f"UI request failed: {error}", file=sys.stderr, flush=True)
                self.send(400, {"code": "REQUEST_FAILED", "error": str(error)})

    return Handler


def make_server(app, port=0):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler_for(app))
    server.daemon_threads = True
    return server


def workspace_identity(base):
    return hashlib.sha256(os.fsencode(str(Path(base).resolve()))).hexdigest()


def recorded_ui_server(base):
    log_path = Path(base) / "ui-launch.log"
    try:
        with log_path.open(encoding="utf-8", errors="replace") as log:
            ports = []
            for line in log:
                match = re.fullmatch(r"YOTO_UI_READY http://127\.0\.0\.1:(\d{1,5})", line.strip())
                if match and 0 < int(match.group(1)) <= 65535:
                    ports.append(int(match.group(1)))
    except FileNotFoundError:
        return None

    expected = workspace_identity(base)
    for port in dict.fromkeys(reversed(ports)):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
        try:
            connection.request("GET", "/api/health")
            response = connection.getresponse()
            if response.status != 200:
                continue
            health = json.loads(response.read(4096))
            if (isinstance(health, dict) and health.get("app") == "yoto-myo-ui"
                    and health.get("ready") is True
                    and health.get("workspace") in (None, expected)):
                return f"http://127.0.0.1:{port}", health
        except (ConnectionError, TimeoutError, http.client.HTTPException, ValueError):
            continue
        finally:
            connection.close()
    return None


def local_ui_request(url, route, body=None, csrf=None):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("Invalid local service address.")
    connection = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=3)
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json", "X-Yoto-CSRF": csrf}
    try:
        connection.request("POST" if body is not None else "GET", route, body=data, headers=headers)
        response = connection.getresponse()
        data = response.read(8_000_001)
        if len(data) > 8_000_000:
            raise ValueError("Local service response is too large.")
        return response.status, json.loads(data)
    finally:
        connection.close()


def stop_idle_ui_server(url):
    try:
        status, snapshot = local_ui_request(url, "/api/status")
    except ConnectionRefusedError:
        return
    if status != 200 or not isinstance(snapshot, dict) or "job" not in snapshot:
        raise RuntimeError("The previous local service did not report its task status.")
    job = snapshot["job"]
    if job is not None and (not isinstance(job, dict) or job.get("state") not in {"completed", "failed", "stopped"}):
        raise RuntimeError(
            "The previous service has a running task or unknown status. Finish or pause it, then reopen Yotolio. "
            "旧版服务仍有任务运行，或状态不明确；请先完成或暂停，再重新打开。"
        )
    status, bootstrap = local_ui_request(url, "/api/bootstrap")
    if status != 200 or not isinstance(bootstrap, dict) or not isinstance(bootstrap.get("csrf"), str):
        raise RuntimeError("The previous local service did not provide a shutdown token.")
    status, result = local_ui_request(url, "/api/quit", {}, bootstrap["csrf"])
    if status == 409:
        raise RuntimeError("The previous service is busy. Finish or pause its task, then reopen Yotolio.")
    if status != 200 or not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("The previous local service refused to shut down.")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            local_ui_request(url, "/api/health")
        except ConnectionRefusedError:
            return
        except (ConnectionResetError, http.client.RemoteDisconnected):
            pass
        time.sleep(0.1)
    raise RuntimeError("The previous local service did not stop; no new service was started.")


def ensure_ui_server(work_dir, replace_idle=False):
    import fcntl

    base = Path(work_dir).resolve()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(base / "ui-launch.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            recorded = recorded_ui_server(base)
            if recorded:
                url, health = recorded
                version = health.get("apiVersion")
                if version == UI_API_VERSION:
                    return url
                if type(version) is int and version > UI_API_VERSION:
                    raise RuntimeError("A newer Yotolio service is running. Close it before opening this version.")
                if not replace_idle:
                    raise OutdatedServiceError("An older local Yoto service must be restarted.")
                stop_idle_ui_server(url)
            log_fd = os.open(base / "ui-launch.log", os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            with os.fdopen(log_fd, "a", encoding="utf-8") as log:
                process = subprocess.Popen(
                    [sys.executable, "-u", str(PROJECT / "yoto_ui.py"),
                     "--no-open", "--work-dir", str(base)],
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                )
            url = None
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    recorded = recorded_ui_server(base)
                    if recorded and recorded[1].get("apiVersion") == UI_API_VERSION:
                        url = recorded[0]
                        return url
                    if process.poll() is not None:
                        raise RuntimeError(f"Local service exited with code {process.returncode}; see ui-launch.log.")
                    time.sleep(0.1)
                raise RuntimeError("Local service did not become ready; see ui-launch.log.")
            finally:
                if url is None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--work-dir", type=Path, default=PROJECT / ".yoto-myo")
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--native-launch", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--replace-idle-service", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error("Python 3.11 or newer is required.")
    if args.replace_idle_service and not args.native_launch:
        parser.error("--replace-idle-service requires --native-launch")
    if args.native_launch:
        try:
            url = ensure_ui_server(args.work_dir, replace_idle=args.replace_idle_service)
        except OutdatedServiceError:
            print("YOTO_UI_RESTART_REQUIRED", flush=True)
            return 42
        print(f"YOTO_UI_NATIVE {url}", flush=True)
        return
    app = App(args.work_dir)
    try:
        server = make_server(app, args.port)
        url = f"http://127.0.0.1:{server.server_port}"
        print(f"YOTO_UI_READY {url}", flush=True)
        if not args.no_open and not webbrowser.open(url):
            print(f"Open this address in your browser: {url}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping local Yoto UI.", flush=True)
    finally:
        app.close()
        if "server" in locals():
            server.server_close()


if __name__ == "__main__":
    sys.exit(main())
