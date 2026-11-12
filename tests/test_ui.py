import argparse
import http.client
import http.server
import json
import os
from pathlib import Path
import sys
import selectors
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from test_yoto import rgba_png
import yoto_ui as ui
import yoto_upload as upload


class UITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.collection = self.directory / "synthetic collection"
        self.collection.mkdir()
        folder = self.collection / "01 - Synthetic Album"
        folder.mkdir()
        (folder / "cover.png").write_bytes(rgba_png())
        (folder / "01 - Synthetic.mp3").write_bytes(b"synthetic audio; never sent to Yoto")
        (folder / "01.png").write_bytes(rgba_png())
        self.app = ui.App(self.directory / "private")
        self.addCleanup(self.app.close)
        self.server = ui.make_server(self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, route, body=None, csrf=True, origin=None):
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
            if csrf:
                headers["X-Yoto-CSRF"] = self.app.csrf
            if origin:
                headers["Origin"] = origin
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url + route, data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.headers, response.read()

    def config(self, **values):
        result = {
            "root": str(self.collection), "workspace": "default",
            "client_id": "SYNTHETIC_CLIENT_ID", "oauth_port": 8788, "icons_scope": False,
        }
        result.update(values)
        return result

    def wait(self):
        self.app.thread.join(timeout=15)
        self.assertFalse(self.app.thread.is_alive(), "Worker did not finish")
        return self.app.snapshot()

    def add_saved_login(self, work=None):
        work = work or self.app.work
        key = "a" * 64
        ui.write_private(work / "authorization.json", {
            "access_token": "synthetic-not-a-real-token",
            "client_id": "SYNTHETIC_CLIENT_ID", "scope": "user:content:view user:content:manage user:icons:manage",
            "obtained_at": time.time(), "expires_in": 3600, "yotolio_account_key": key,
        })
        workspace = "default" if work == self.app.base else work.name
        ui.write_private(self.app.base / "account-bindings.json", {"version": 1, "accounts": {key: workspace}})

    @staticmethod
    def worker(*lines):
        return [[sys.executable, "-u", "-c", f"print({chr(10).join(lines)!r}, flush=True)"]]

    @staticmethod
    def review_result(title, presence="missing", pending_create=False, pending_overwrite=False):
        return {
            "title": title, "presence": presence, "cover": "unverified",
            "remoteTracks": 1 if presence == "present" else None,
            "remoteIcons": 0 if presence == "present" else None,
            "remoteCovers": 1 if presence == "present" else None,
            "localFingerprint": "1" * 64, "remoteFingerprint": "2" * 64 if presence == "present" else None,
            "pendingCreate": pending_create, "pendingOverwrite": pending_overwrite,
            "tracks": [], "error": None,
        }

    def complete_review(self, *results):
        lines = ["INSPECTED " + json.dumps(result) for result in results]
        with patch.object(self.app, "commands", return_value=self.worker(*lines)):
            job = self.app.start("inspect", albums=[result["title"] for result in results], purpose="review")
            snapshot = self.wait()
        self.assertEqual(snapshot["reviewInspection"]["state"], "completed")
        return job

    def fake_login(self, key="a" * 64, scope="openid user:content:view user:content:manage", token_changes=None):
        token = {
            "access_token": "synthetic-token-" + key[:1], "client_id": "SYNTHETIC_CLIENT_ID",
            "obtained_at": time.time(), "expires_in": 3600, "scope": scope, "yotolio_account_key": key,
        }
        if token_changes:
            token.update(token_changes)
        original = self.app.commands

        def commands(operation, album, albums=None):
            if operation == "auth":
                script = ("import pathlib,sys; p=pathlib.Path(sys.argv[sys.argv.index('--work-dir')+1]); "
                          "(p/'authorization.json').write_text(" + repr(json.dumps(token)) + ")")
                return [[sys.executable, "-c", script, "--work-dir", str(self.app.work)]]
            return original(operation, album, albums)

        return patch.object(self.app, "commands", side_effect=commands)

    def legacy_history(self, work=None, expired=True):
        work = work or self.app.base
        work.mkdir(parents=True, exist_ok=True)
        ui.write_private(work / "authorization.json", {
            "access_token": "synthetic-legacy-token", "client_id": "SYNTHETIC_CLIENT_ID",
            "scope": "openid user:content:view user:content:manage",
            "obtained_at": time.time() - (7200 if expired else 0), "expires_in": 3600,
        })
        ui.write_private(work / "upload-state.json", {"albums": {"Earlier Card": {"status": "verified"}}})
        return (work / "authorization.json").read_bytes(), (work / "upload-state.json").read_bytes()

    def start_migration(self):
        with self.fake_login():
            self.app.start("auth")
            result = self.wait()
        self.assertEqual(result["job"]["phase"], "account-confirmation")
        self.assertIsNotNone(result["accountMigration"])
        return result["accountMigration"]["id"]

    def stop_launched_service(self, url):
        try:
            status, bootstrap = ui.local_ui_request(url, "/api/bootstrap")
        except ConnectionRefusedError:
            return
        self.assertEqual(status, 200)
        status, result = ui.local_ui_request(url, "/api/quit", {}, bootstrap["csrf"])
        self.assertEqual((status, result), (200, {"ok": True}))
        for _ in range(50):
            try:
                ui.local_ui_request(url, "/api/health")
            except ConnectionRefusedError:
                return
            except (ConnectionResetError, http.client.RemoteDisconnected):
                pass
            time.sleep(0.1)
        self.fail("Synthetic native-launch server did not stop")

    def test_static_app_and_both_locale_modules_are_served(self):
        code, headers, body = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn(b'<script type="module" src="/app.js">', body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        for route in ("/app.js", "/i18n.mjs", "/styles.css", "/icon.svg", "/myo-placeholder.svg",
                      "/setup-create.png", "/setup-callback.png", "/setup-scopes.png"):
            self.assertEqual(self.request(route)[0], 200)
        _, image_headers, image = self.request("/setup-create.png")
        self.assertEqual(image_headers["Content-Type"], "image/png")
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
        health = json.loads(self.request("/api/health")[2])
        self.assertEqual((health["ready"], health["apiVersion"]), (True, ui.UI_API_VERSION))
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/.yoto-myo/authorization.json")
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

    def test_recreation_requires_current_failed_job_and_explicit_single_card_confirmation(self):
        self.app.save_config(self.config())
        self.add_saved_login()
        self.app.albums = [{"title": "Synthetic Album"}, {"title": "Another Album"}]
        self.app.job = {"id": "failed-job", "operation": "upload", "state": "running", "logs": []}
        self.app.record("RECOVERY_REQUIRED " + json.dumps({
            "type": "missing_prior_card", "title": "Synthetic Album", "priorStatus": "verified",
        }))
        self.app.job["state"] = "failed"
        request = {"operation": "recreate", "album": "Synthetic Album",
                   "confirmed": True, "recoveryJobId": "failed-job"}
        invalid = (
            {**request, "confirmed": False},
            {**request, "confirmed": "true"},
            {**request, "recoveryJobId": "stale-job"},
            {**request, "album": "Another Album"},
            {**request, "albums": ["Synthetic Album", "Another Album"]},
        )
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/jobs", body)
            self.assertEqual(error.exception.code, 409)
            self.assertEqual(json.loads(error.exception.read())["code"], "RECREATE_CONFIRMATION_REQUIRED")
            error.exception.close()
        self.assertEqual(self.app.job["id"], "failed-job")

        for operation in ("scan", "prepare", "verify", "inspect"):
            body = {"operation": operation}
            if operation == "inspect":
                body["albums"] = ["Synthetic Album"]
            with self.subTest(operation=operation), self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/jobs", body)
            self.assertEqual((error.exception.code, json.loads(error.exception.read())["code"]),
                             (409, "RECOVERY_REQUIRED"))
            error.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/asset", {"album": "Synthetic Album", "kind": "cover", "track": None})
        self.assertEqual((error.exception.code, json.loads(error.exception.read())["code"]),
                         (409, "RECOVERY_REQUIRED"))
        error.exception.close()
        self.assertEqual(self.app.job["id"], "failed-job")

        with patch.object(self.app, "run") as worker:
            code, _, data = self.request("/api/jobs", request)
            self.assertEqual(code, 202)
            result = json.loads(data)["job"]
            self.app.thread.join(timeout=5)
            self.assertEqual(result["operation"], "recreate")
            command = worker.call_args.args[0][0]
            self.assertIn("--recreate-missing", command)
            self.assertEqual(command[command.index("--album") + 1], "Synthetic Album")
            self.assertEqual(command.count("--album"), 1)

        self.app.job["state"] = "failed"
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/jobs", request)
        self.assertEqual(error.exception.code, 409)
        error.exception.close()
        self.assertEqual(worker.call_count, 1, "The original confirmation cannot be replayed")

    def test_recovery_output_must_match_the_failed_upload_and_scanned_card(self):
        self.app.albums = [{"title": "Synthetic Album"}]
        for operation, title, prior_status in (
            ("verify", "Synthetic Album", "verified"),
            ("upload", "Other Album", "verified"),
            ("upload", "Synthetic Album", "unexpected"),
        ):
            self.app.job = {"id": "job", "operation": operation, "state": "running", "logs": []}
            line = "RECOVERY_REQUIRED " + json.dumps({
                "type": "missing_prior_card", "title": title, "priorStatus": prior_status,
            })
            with self.subTest(operation=operation, title=title, status=prior_status), \
                    self.assertRaisesRegex(ValueError, "Invalid card recovery evidence"):
                self.app.record(line)
            self.assertNotIn("recovery", self.app.job)
        self.app.job = None

    def test_missing_csrf_foreign_origin_and_wrong_host_cannot_change_state(self):
        for kwargs in ({"csrf": False}, {"origin": "https://example.invalid"}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/config", self.config(), **kwargs)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request("GET", "/api/status", headers={"Host": "example.invalid"})
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        connection.close()
        self.assertEqual(self.app.config["root"], "")

    def test_scan_discovers_original_cover_without_copying_or_login(self):
        self.request("/api/config", self.config())
        self.assertEqual(self.request("/api/jobs", {"operation": "prepare"})[0], 202)
        result = self.wait()
        self.assertEqual(result["job"]["state"], "completed")
        self.assertEqual(result["job"]["phase"], "prepared")
        self.assertEqual(len(result["albums"]), 1)
        album = result["albums"][0]
        self.assertEqual((album["title"], album["tracks"], album["icons"]), ("Synthetic Album", 1, 1))
        self.assertEqual(album["cover"], str((self.collection / "01 - Synthetic Album" / "cover.png").resolve()))
        self.assertEqual(album["coverStatus"], "provided")
        self.assertIsNone(album["audioIssue"])
        self.assertEqual(self.request("/api/cover?index=0")[2], rgba_png())
        self.assertFalse((self.collection / "Covers").exists())
        self.assertFalse((self.app.work / "authorization.json").exists())
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/jobs", {"operation": "upload"})
        self.assertEqual(json.loads(error.exception.read())["code"], "INVALID_REVIEW")
        error.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/jobs", {"operation": "inspect", "albums": ["Synthetic Album"], "purpose": "review"})
        self.assertEqual(json.loads(error.exception.read())["code"], "AUTH_REQUIRED")
        error.exception.close()

    def test_scan_reports_separate_cards_with_named_asset_subfolders(self):
        first = self.collection / "01 - Synthetic Album"
        audio, images = first / "audio_files", first / "image_files"
        audio.mkdir()
        images.mkdir()
        (first / "01 - Synthetic.mp3").replace(audio / "01 - Synthetic.mp3")
        for name in ("cover.png", "01.png"):
            (first / name).replace(images / name)
        second = self.collection / "02 - Another Album"
        (second / "audio_files").mkdir(parents=True)
        (second / "image_files").mkdir()
        (second / "audio_files" / "01 - Another.mp3").write_bytes(b"synthetic audio")
        (second / "image_files" / "01.png").write_bytes(rgba_png())
        (second / "image_files" / "cover.png").write_bytes(rgba_png())

        self.app.save_config(self.config())
        self.app.start("scan")
        result = self.wait()
        self.assertEqual(result["job"]["state"], "completed")
        self.assertEqual([(a["title"], a["tracks"], a["icons"], a["coverStatus"]) for a in result["albums"]],
                         [("Another Album", 1, 1, "provided"), ("Synthetic Album", 1, 1, "provided")])
        self.assertFalse((self.app.work / "authorization.json").exists())

    def test_missing_assets_preview_manual_selection_and_collection_scope(self):
        folder = self.collection / "01 - Synthetic Album"
        (folder / "cover.png").unlink()
        (folder / "01.png").unlink()
        self.app.save_config(self.config())
        self.app.start("scan")
        result = self.wait()
        album = result["albums"][0]
        self.assertEqual((album["coverStatus"], album["icons"]), ("missing", 0))
        self.assertEqual(album["trackDetails"][0]["issue"], "missing")
        self.assertEqual(self.request("/api/cover?index=0")[2], ui.placeholder_cover("Synthetic Album"))

        cover = self.directory / "chosen-cover.png"
        icon = self.directory / "chosen-icon.png"
        cover.write_bytes(rgba_png())
        icon.write_bytes(rgba_png())
        for kind, path in (("cover", cover), ("icon", icon)):
            with patch.object(ui, "choose_image", return_value=str(path)):
                code, _, body = self.request("/api/asset", {
                    "album": "Synthetic Album", "kind": kind, "track": 0 if kind == "icon" else None,
                })
            self.assertEqual(code, 200)
            self.assertTrue(json.loads(body)["chosen"])
        self.app.start("scan")
        album = self.wait()["albums"][0]
        self.assertEqual(album["cover"], str(cover.resolve()))
        self.assertEqual(album["icons"], 1)
        self.assertEqual(self.request("/api/cover?index=0")[2], cover.read_bytes())

        other = self.directory / "other collection"
        other_folder = other / "01 - Synthetic Album"
        other_folder.mkdir(parents=True)
        (other_folder / "01 - Synthetic.mp3").write_bytes(b"other synthetic audio")
        self.app.save_config(self.config(root=str(other)))
        self.app.start("scan")
        album = self.wait()["albums"][0]
        self.assertEqual((album["coverStatus"], album["icons"]), ("missing", 0))
        self.app.albums[0]["cover"] = str(cover.resolve())
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/cover?index=0")
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

    def test_overview_reports_new_assets_only_against_verified_history(self):
        self.app.save_config(self.config())
        self.app.start("scan")
        self.wait()
        state = {"albums": {"Synthetic Album": {
            "status": "verified", "coverPlaceholder": True, "iconIds": [None],
        }}}
        ui.write_private(self.app.work / "upload-state.json", state)
        album = self.app.snapshot()["albums"][0]
        self.assertTrue(album["coverNeedsUpload"])
        self.assertEqual(album["iconsNeedUpload"], 1)
        state["albums"]["Synthetic Album"]["status"] = "pending_create"
        ui.write_private(self.app.work / "upload-state.json", state)
        album = self.app.snapshot()["albums"][0]
        self.assertFalse(album["coverNeedsUpload"])
        self.assertEqual(album["iconsNeedUpload"], 0)

    def test_overview_reports_invalid_saved_evidence(self):
        self.app.save_config(self.config())
        self.app.start("scan")
        self.wait()
        ui.write_private(self.app.work / "upload-state.json", {"albums": {
            "Synthetic Album": {"status": "verified", "iconIds": []},
        }})
        snapshot = self.app.snapshot()
        self.assertIn("Saved upload evidence cannot be read", snapshot["assetStatusError"])
        self.assertEqual(snapshot["albums"][0]["title"], "Synthetic Album")

    def test_saved_authorization_stays_private_and_accounts_are_automatically_separate(self):
        self.app.save_config(self.config())
        self.add_saved_login()
        status = json.loads(self.request("/api/status")[2])
        self.assertTrue(status["connection"]["usable"])
        self.assertNotIn("access_token", json.dumps(status))
        self.assertNotIn("synthetic-not-a-real-token", json.dumps(status))
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/config", self.config(workspace="another-account"))
        self.assertEqual(error.exception.code, 409)
        error.exception.close()
        with self.fake_login("b" * 64):
            self.app.start("auth", switch_account=True)
            self.assertEqual(self.wait()["job"]["state"], "completed")
        self.assertNotEqual(self.app.work, self.app.base)
        self.assertTrue(self.app.connection()["usable"])
        self.assertTrue((self.app.base / "authorization.json").exists())
        self.request("/api/disconnect", {})
        self.assertFalse((self.app.work / "authorization.json").exists())
        self.assertTrue((self.app.base / "authorization.json").exists())

    def test_account_switch_reloads_its_own_images_and_preserves_each_history(self):
        self.app.save_config(self.config())
        self.app.start("scan")
        self.wait()
        with self.fake_login("a" * 64):
            self.app.start("auth")
            self.assertEqual(self.wait()["job"]["state"], "completed")
        work_a = self.app.work
        cover_a = self.directory / "chosen-a.png"
        cover_a.write_bytes(rgba_png())
        self.request("/api/asset", {
            "album": "Synthetic Album", "kind": "cover", "track": None, "path": str(cover_a),
        })
        self.app.start("scan")
        self.assertEqual(self.wait()["albums"][0]["cover"], str(cover_a.resolve()))
        ui.write_private(work_a / "upload-state.json", {"albums": {}, "marker": "account-a"})
        history_a = (work_a / "upload-state.json").read_bytes()
        self.app.last_upload = {"id": "account-a-batch"}
        self.app.last_inspection = {"id": "account-a-check"}

        with self.fake_login("b" * 64):
            self.app.start("auth", switch_account=True)
            switched = self.wait()
        work_b = self.app.work
        self.assertNotEqual(work_a, work_b)
        self.assertEqual(switched["job"]["state"], "completed")
        self.assertEqual(switched["albums"][0]["cover"],
                         str((self.collection / "01 - Synthetic Album" / "cover.png").resolve()))
        self.assertIsNone(switched["lastUpload"])
        self.assertIsNone(switched["inspection"])
        self.assertEqual((work_a / "upload-state.json").read_bytes(), history_a)
        self.assertFalse((work_b / "upload-state.json").exists())

        cover_b = self.directory / "chosen-b.png"
        cover_b.write_bytes(rgba_png())
        self.request("/api/asset", {
            "album": "Synthetic Album", "kind": "cover", "track": None, "path": str(cover_b),
        })
        self.app.start("scan")
        self.assertEqual(self.wait()["albums"][0]["cover"], str(cover_b.resolve()))
        ui.write_private(work_b / "upload-state.json", {"albums": {}, "marker": "account-b"})
        history_b = (work_b / "upload-state.json").read_bytes()
        with self.fake_login("a" * 64):
            self.app.start("auth", switch_account=True)
            restored = self.wait()
        self.assertEqual(self.app.work, work_a)
        self.assertEqual(restored["albums"][0]["cover"], str(cover_a.resolve()))
        self.assertEqual((work_a / "upload-state.json").read_bytes(), history_a)
        self.assertEqual((work_b / "upload-state.json").read_bytes(), history_b)
        self.assertNotIn("synthetic-token", json.dumps(restored))
        self.assertNotIn("a" * 64, json.dumps(restored))

    def test_unverified_legacy_history_needs_one_time_confirmation_to_restore(self):
        self.app.save_config(self.config())
        _, history = self.legacy_history()
        choice_id = self.start_migration()
        with self.assertRaises(ui.UIError) as error:
            self.app.start("upload")
        self.assertEqual(error.exception.code, "ACCOUNT_CONFIRMATION_REQUIRED")
        self.request("/api/account-migration", {"id": choice_id, "choice": "same"})
        result = self.wait()
        self.assertEqual(result["job"]["state"], "completed")
        self.assertTrue(result["connection"]["usable"])
        self.assertEqual(self.app.work, self.app.base)
        self.assertEqual((self.app.base / "upload-state.json").read_bytes(), history)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/account-migration", {"id": choice_id, "choice": "same"})
        self.assertEqual((error.exception.code, json.loads(error.exception.read())["code"]),
                         (409, "ACCOUNT_CONFIRMATION_EXPIRED"))
        error.exception.close()

    def test_unverified_legacy_history_can_be_kept_separate_or_cancelled(self):
        self.app.save_config(self.config())
        old_token, old_history = self.legacy_history()
        self.app.last_upload = {"id": "earlier-batch"}
        choice_id = self.start_migration()
        self.request("/api/account-migration", {"id": choice_id, "choice": "cancel"})
        self.assertEqual(self.app.snapshot()["job"]["state"], "stopped")
        self.assertEqual(self.app.snapshot()["lastUpload"]["id"], "earlier-batch")
        self.assertEqual((self.app.base / "authorization.json").read_bytes(), old_token)
        self.assertEqual((self.app.base / "upload-state.json").read_bytes(), old_history)

        choice_id = self.start_migration()
        self.request("/api/account-migration", {"id": choice_id, "choice": "different"})
        result = self.wait()
        self.assertEqual(result["job"]["state"], "completed")
        self.assertNotEqual(self.app.work, self.app.base)
        self.assertEqual((self.app.base / "authorization.json").read_bytes(), old_token)
        self.assertEqual((self.app.base / "upload-state.json").read_bytes(), old_history)
        self.assertFalse((self.app.work / "upload-state.json").exists())

    def test_legacy_history_without_a_token_can_be_kept_separate_after_login(self):
        self.app.save_config(self.config())
        self.app.start("scan")
        self.assertEqual(len(self.wait()["albums"]), 1)
        history_file = self.app.base / "upload-state.json"
        ui.write_private(history_file, {"albums": {"Earlier Card": {"status": "verified"}}})
        history = history_file.read_bytes()
        self.assertFalse((self.app.base / "authorization.json").exists())

        choice_id = self.start_migration()
        pending = self.app.snapshot()
        self.assertFalse(pending["connection"]["usable"])
        self.assertEqual(pending["accountMigration"]["id"], choice_id)
        self.assertFalse((self.app.base / "account-bindings.json").exists())
        self.request("/api/account-migration", {"id": choice_id, "choice": "different"})
        result = self.wait()
        self.assertEqual((result["job"]["state"], result["job"]["phase"]), ("completed", "connected"))
        self.assertTrue(result["connection"]["usable"])
        self.assertEqual(len(result["albums"]), 1, "The collection is rescanned for the newly bound account")
        self.assertNotEqual(self.app.work, self.app.base)
        self.assertEqual(history_file.read_bytes(), history)
        self.assertFalse((self.app.base / "authorization.json").exists())
        self.assertFalse((self.app.work / "upload-state.json").exists())

    def test_changed_legacy_history_cannot_be_restored_from_stale_confirmation(self):
        self.app.save_config(self.config())
        old_token, _ = self.legacy_history()
        choice_id = self.start_migration()
        ui.write_private(self.app.base / "upload-state.json", {"albums": {}, "marker": "changed"})
        changed = (self.app.base / "upload-state.json").read_bytes()
        self.request("/api/account-migration", {"id": choice_id, "choice": "same"})
        result = self.wait()
        self.assertEqual((result["job"]["state"], result["job"]["errorCode"]),
                         ("failed", "ACCOUNT_HISTORY_CHANGED"))
        self.assertEqual((self.app.base / "authorization.json").read_bytes(), old_token)
        self.assertEqual((self.app.base / "upload-state.json").read_bytes(), changed)
        self.assertFalse((self.app.base / "account-bindings.json").exists())

    def test_multiple_unknown_histories_require_fresh_individual_confirmations(self):
        self.app.save_config(self.config())
        first_token, first_history = self.legacy_history()
        other = self.app.base / "profiles" / "old-profile"
        second_token, second_history = self.legacy_history(other)
        first_id = self.start_migration()
        self.request("/api/account-migration", {"id": first_id, "choice": "different"})
        next_question = self.app.snapshot()["accountMigration"]
        self.assertEqual(next_question["name"], "old-profile")
        self.assertNotEqual(next_question["id"], first_id)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/account-migration", {"id": first_id, "choice": "same"})
        self.assertEqual((error.exception.code, json.loads(error.exception.read())["code"]),
                         (409, "ACCOUNT_CONFIRMATION_EXPIRED"))
        error.exception.close()
        self.request("/api/account-migration", {"id": next_question["id"], "choice": "different"})
        self.assertEqual(self.wait()["job"]["state"], "completed")
        self.assertEqual((self.app.base / "authorization.json").read_bytes(), first_token)
        self.assertEqual((self.app.base / "upload-state.json").read_bytes(), first_history)
        self.assertEqual((other / "authorization.json").read_bytes(), second_token)
        self.assertEqual((other / "upload-state.json").read_bytes(), second_history)

    def test_verified_legacy_account_resumes_without_a_guess(self):
        self.app.save_config(self.config())
        _, history = self.legacy_history(expired=False)
        with patch.object(ui.accounts, "identify_account", return_value="a" * 64), self.fake_login():
            self.app.start("auth")
            result = self.wait()
        self.assertEqual(result["job"]["state"], "completed")
        self.assertIsNone(result["accountMigration"])
        self.assertEqual(self.app.work, self.app.base)
        self.assertEqual((self.app.base / "upload-state.json").read_bytes(), history)

    def test_verified_other_legacy_account_is_not_mistaken_for_current_login(self):
        self.app.save_config(self.config())
        old_token, old_history = self.legacy_history(expired=False)
        with patch.object(ui.accounts, "identify_account", return_value="b" * 64), self.fake_login():
            self.app.start("auth")
            result = self.wait()
        self.assertEqual(result["job"]["state"], "completed")
        self.assertIsNone(result["accountMigration"])
        self.assertNotEqual(self.app.work, self.app.base)
        self.assertEqual((self.app.base / "authorization.json").read_bytes(), old_token)
        self.assertEqual((self.app.base / "upload-state.json").read_bytes(), old_history)

    def test_multiple_verified_legacy_histories_are_preserved_not_merged(self):
        self.app.save_config(self.config())
        token, history = self.legacy_history(expired=False)
        other = self.app.base / "profiles" / "old-profile"
        other_token, other_history = self.legacy_history(other, expired=False)
        with patch.object(ui.accounts, "identify_account", return_value="a" * 64), self.fake_login():
            self.app.start("auth")
            result = self.wait()
        self.assertEqual((result["job"]["state"], result["job"]["errorCode"]),
                         ("failed", "ACCOUNT_MULTIPLE_HISTORIES"))
        self.assertEqual((self.app.base / "authorization.json").read_bytes(), token)
        self.assertEqual((self.app.base / "upload-state.json").read_bytes(), history)
        self.assertEqual((other / "authorization.json").read_bytes(), other_token)
        self.assertEqual((other / "upload-state.json").read_bytes(), other_history)
        self.assertFalse((self.app.base / "account-bindings.json").exists())

    def test_missing_optional_icon_permission_does_not_replace_saved_login(self):
        self.app.save_config(self.config(icons_scope=True))
        self.add_saved_login()
        previous = (self.app.work / "authorization.json").read_bytes()
        bindings = (self.app.base / "account-bindings.json").read_bytes()
        with self.fake_login(scope="openid user:content:view user:content:manage"):
            self.app.start("auth")
            result = self.wait()
        self.assertEqual((result["job"]["state"], result["job"]["errorCode"]),
                         ("failed", "ACCOUNT_AUTH_SCOPES_INSUFFICIENT"))
        self.assertEqual((self.app.work / "authorization.json").read_bytes(), previous)
        self.assertEqual((self.app.base / "account-bindings.json").read_bytes(), bindings)
        self.assertTrue(result["connection"]["usable"])

    def test_verified_parent_scope_activates_without_explicit_openid(self):
        self.app.save_config(self.config(icons_scope=True))
        self.add_saved_login()
        with self.fake_login(scope="user:content:manage", token_changes={
            "yotolio_verified_scopes": ["user:content:view", "user:icons:manage"],
        }):
            self.app.start("auth")
            result = self.wait()
        self.assertEqual(result["job"]["state"], "completed")
        self.assertTrue(result["connection"]["usable"])
        self.assertEqual(result["connection"]["scopes"], ["user:content:manage"])
        saved = json.loads((self.app.work / "authorization.json").read_text())
        self.assertEqual(set(saved["yotolio_verified_scopes"]), {"user:content:view", "user:icons:manage"})

    def test_invalid_staged_authorization_preserves_previous_login(self):
        self.app.save_config(self.config())
        self.add_saved_login()
        previous = (self.app.work / "authorization.json").read_bytes()
        bindings = (self.app.base / "account-bindings.json").read_bytes()
        cases = [
            ({"access_token": ""}, "ACCOUNT_AUTH_TOKEN_INVALID"),
            ({"expires_in": 60}, "ACCOUNT_AUTH_EXPIRY_INVALID"),
            ({"obtained_at": time.time() - 7200}, "ACCOUNT_AUTH_EXPIRY_INVALID"),
            ({"scope": None}, "ACCOUNT_AUTH_RESPONSE_INVALID"),
            ({"scope": "openid user:content:view"}, "ACCOUNT_AUTH_SCOPES_INSUFFICIENT"),
        ]
        for changes, code in cases:
            with self.subTest(code=code), self.fake_login(token_changes=changes):
                self.app.start("auth")
                result = self.wait()
                self.assertEqual((result["job"]["state"], result["job"]["errorCode"]), ("failed", code))
                self.assertNotIn("synthetic-token", result["job"]["error"])
                self.assertEqual((self.app.work / "authorization.json").read_bytes(), previous)
                self.assertEqual((self.app.base / "account-bindings.json").read_bytes(), bindings)
                self.assertTrue(result["connection"]["usable"])

    def test_authorization_worker_error_codes_reach_the_ui_without_changing_login(self):
        self.app.save_config(self.config())
        self.add_saved_login()
        previous = (self.app.work / "authorization.json").read_bytes()
        for code in ui.AUTHORIZATION_RESPONSE_ERRORS:
            with self.subTest(code=code):
                fake = [sys.executable, "-c", f"import sys; sys.exit({code!r})"]
                with patch.object(self.app, "commands", return_value=[fake]):
                    self.app.start("auth")
                    result = self.wait()
                self.assertEqual((result["job"]["state"], result["job"]["errorCode"]), ("failed", code))
                self.assertEqual((self.app.work / "authorization.json").read_bytes(), previous)
                self.assertTrue(result["connection"]["usable"])

    def test_busy_account_history_is_never_overwritten(self):
        self.app.save_config(self.config())
        self.add_saved_login()
        previous = (self.app.work / "authorization.json").read_bytes()
        with upload.work_lock(self.app.work), self.fake_login():
            self.app.start("auth")
            result = self.wait()
        self.assertEqual(result["job"]["state"], "failed")
        self.assertIn("Another uploader", result["job"]["error"])
        self.assertEqual((self.app.work / "authorization.json").read_bytes(), previous)

    def test_failed_identity_verification_keeps_previous_login_and_reports_code(self):
        self.app.save_config(self.config())
        self.add_saved_login()
        previous = (self.app.work / "authorization.json").read_bytes()
        bindings = (self.app.base / "account-bindings.json").read_bytes()
        ui.write_private(self.app.work / "upload-state.json", {"albums": {"Synthetic Card": {"status": "verified"}}})
        progress = (self.app.work / "upload-state.json").read_bytes()
        for code in ui.accounts.IDENTITY_ERRORS:
            with self.subTest(code=code):
                fake = [sys.executable, "-c", f"import sys; sys.exit({code!r})"]
                with patch.object(self.app, "commands", return_value=[fake]):
                    self.app.start("auth")
                    result = self.wait()
                self.assertEqual((result["job"]["state"], result["job"]["errorCode"]), ("failed", code))
                self.assertEqual((self.app.work / "authorization.json").read_bytes(), previous)
                self.assertEqual((self.app.base / "account-bindings.json").read_bytes(), bindings)
                self.assertEqual((self.app.work / "upload-state.json").read_bytes(), progress)
                self.assertTrue(result["connection"]["usable"])

    def test_another_window_cannot_silently_change_the_active_settings(self):
        self.app.save_config(self.config())
        self.add_saved_login()
        self.app.start("scan")
        self.wait()
        previous = (self.app.work / "authorization.json").read_bytes()
        other_collection = self.directory / "another-collection"
        other_collection.mkdir()
        other = ui.App(self.app.base)
        self.addCleanup(other.close)
        other.save_config(self.config(root=str(other_collection)))
        for action in (lambda: self.app.start("scan"),
                       self.app.disconnect,
                       lambda: self.app.select_asset("Synthetic Album", "cover", None,
                                                     str(self.collection / "01 - Synthetic Album" / "cover.png"))):
            with self.subTest(action=action), self.assertRaises(ui.UIError) as error:
                action()
            self.assertEqual(error.exception.code, "CONFIG_CHANGED")
        self.assertEqual((self.app.work / "authorization.json").read_bytes(), previous)
        self.assertEqual(json.loads((self.app.base / "ui-config.json").read_text())["root"],
                         str(other_collection.resolve()))

    def test_malformed_saved_authorization_is_reported_without_breaking_the_ui(self):
        self.app.save_config(self.config())
        for token in ([], {"scope": []}, {"scope": "user:content:view"}):
            ui.write_private(self.app.work / "authorization.json", token)
            status = json.loads(self.request("/api/status")[2])
            self.assertTrue(status["connection"]["saved"])
            self.assertFalse(status["connection"]["usable"])
            self.assertIn("cannot be read", status["connection"]["error"])

    def test_invalid_settings_and_running_job_are_explicit_errors(self):
        for value in (self.config(workspace="../elsewhere"), self.config(oauth_port=1),
                      self.config(root=str(self.directory / "missing"))):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/config", value)
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
        self.app.job = {"state": "running"}
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/config", self.config())
        self.assertEqual(error.exception.code, 409)
        error.exception.close()
        self.app.job = None

    def test_ui_reuses_exact_workers_for_upload_auth_and_read_only_verification(self):
        self.app.save_config(self.config(icons_scope=True))
        self.add_saved_login()
        self.app.albums = [{"title": "Synthetic Album", "tracks": 1, "icons": 1, "cover": "cover.png"}]
        command = self.app.commands("verify", None)[0]
        self.assertEqual(command[2], str(ui.PROJECT / "yoto_upload.py"))
        self.assertIn("--verify-only", command)
        self.assertIn("--stop-file", command)
        self.assertNotIn("--existing-only", command)
        command = self.app.commands("auth", None)[0]
        self.assertEqual(command[2], str(ui.PROJECT / "yoto_authorize.py"))
        self.assertIn("--icons-scope", command)
        self.assertIn("--replace", command)
        with self.assertRaisesRegex(ui.UIError, "scanned albums"):
            self.app.commands("upload", "not a scanned album")

    def test_selected_batch_is_validated_and_passed_without_a_shell(self):
        self.app.save_config(self.config())
        self.add_saved_login()
        self.app.albums = [{"title": "First"}, {"title": "Second"}, {"title": "Third"}]
        command = self.app.commands("upload", None, ["First", "Third"])[0]
        self.assertEqual(command.count("--album"), 2)
        self.assertEqual([command[index + 1] for index, value in enumerate(command) if value == "--album"],
                         ["First", "Third"])
        for selection in ([], ["First", "First"], ["First", "Missing"], "First"):
            with self.subTest(selection=selection), self.assertRaises(ui.UIError):
                self.app.commands("upload", None, selection)
        with self.assertRaises(ui.UIError):
            self.app.commands("upload", "First", ["Second"])
        with self.assertRaises(ui.UIError):
            self.app.commands("auth", None, ["First"])

    def test_native_image_selection_uses_the_same_collection_scoped_validation(self):
        self.app.save_config(self.config())
        self.app.start("scan")
        self.wait()
        image = self.directory / "chosen.png"
        image.write_bytes(rgba_png())
        with patch.object(ui, "choose_image", side_effect=AssertionError("Must use the native selection")):
            code, _, body = self.request("/api/asset", {
                "album": "Synthetic Album", "kind": "cover", "track": None, "path": str(image),
            })
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["chosen"])
        self.app.start("scan")
        self.assertEqual(self.wait()["albums"][0]["cover"], str(image.resolve()))

    def test_icon_preview_only_serves_current_track_source_or_explicit_selection(self):
        self.app.save_config(self.config())
        self.app.start("scan")
        self.wait()
        self.assertEqual(self.request("/api/icon?index=0&track=0")[2], rgba_png())
        for route in ("/api/icon?index=9&track=0", "/api/icon?index=0&track=1",
                      "/api/icon?index=0&track=-1"):
            with self.subTest(route=route), self.assertRaises(urllib.error.HTTPError) as error:
                self.request(route)
            self.assertEqual(error.exception.code, 404)
            error.exception.close()

        chosen = self.directory / "selected-icon.png"
        chosen.write_bytes(rgba_png() + b"selected synthetic icon")
        self.assertTrue(self.app.select_asset("Synthetic Album", "icon", 0, str(chosen)))
        self.app.start("scan")
        self.wait()
        self.assertEqual(self.request("/api/icon?index=0&track=0")[2], chosen.read_bytes())

        unrelated = self.directory / "unrelated.png"
        unrelated.write_bytes(rgba_png() + b"unrelated")
        self.app.albums[0]["trackDetails"][0]["icon"] = str(unrelated)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/icon?index=0&track=0")
        self.assertEqual(error.exception.code, 404)
        error.exception.close()
        self.app.albums[0]["trackDetails"][0]["icon"] = str(chosen)
        self.app.albums[0]["trackDetails"][0]["source"] = "/synthetic/other-track.mp3"
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/icon?index=0&track=0")
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

    def test_upload_and_inspection_reports_keep_batch_scope_and_reject_incomplete_output(self):
        self.app.save_config(self.config())
        self.add_saved_login()
        self.app.albums = [{"title": "Synthetic Album"}, {"title": "Other Album"}, {"title": "Outside Batch"}]
        review_job = self.complete_review(self.review_result("Synthetic Album"),
                                          self.review_result("Other Album", "present"))

        with patch.object(self.app, "commands", return_value=self.worker(
                "VERIFIED Synthetic Album: card=synthetic; tracks=1; icons=1")):
            upload_job = self.app.start("upload", albums=["Synthetic Album"], review_id=review_job["id"],
                                        skipped_titles=["Other Album"])
            uploaded = self.wait()
        self.assertEqual(uploaded["lastUpload"]["requested"], ["Synthetic Album"])
        self.assertEqual(uploaded["lastUpload"]["completed"], ["Synthetic Album"])
        self.assertEqual(uploaded["lastUpload"]["reviewSkipped"], ["Other Album"])
        self.assertEqual(uploaded["lastUpload"]["state"], "completed")

        result = {
            "title": "Synthetic Album", "presence": "present", "cover": "matched", "remoteTracks": 1,
            "tracks": [{"name": "Song.mp3", "remoteTitle": "Renamed",
                        "audio": "name_mismatch", "icon": "song_mismatch"}],
            "error": None,
        }
        line = "INSPECTED " + json.dumps(result)
        other = {**result, "title": "Other Album"}
        with patch.object(self.app, "commands", return_value=self.worker(line, "INSPECTED " + json.dumps(other))):
            self.app.start("inspect", albums=["Synthetic Album", "Other Album"])
            inspected = self.wait()
        self.assertEqual(inspected["inspection"]["sourceUploadId"], upload_job["id"])
        self.assertEqual(inspected["inspection"]["results"], [result, other])
        self.assertEqual(inspected["inspection"]["state"], "completed")
        self.assertEqual(inspected["lastUpload"]["id"], upload_job["id"])

        for lines, expected in (([], "without results for every selected card"),
                                ([line, line], "Invalid read-only card result")):
            with self.subTest(lines=lines), patch.object(self.app, "commands", return_value=self.worker(*lines)):
                self.app.start("inspect", albums=["Synthetic Album"])
                failed = self.wait()
            self.assertEqual(failed["inspection"]["state"], "failed")
            self.assertIn(expected, failed["inspection"]["error"])
            self.assertEqual(failed["lastUpload"]["id"], upload_job["id"])

        outside = {**result, "title": "Outside Batch"}
        with patch.object(self.app, "commands", return_value=self.worker("INSPECTED " + json.dumps(outside))):
            self.app.start("inspect", albums=["Outside Batch"])
            unbound = self.wait()
        self.assertIsNone(unbound["inspection"]["sourceUploadId"])

    def test_review_upload_requires_current_account_explicit_replace_and_valid_batch(self):
        self.app.save_config(self.config())
        self.add_saved_login()
        titles = ["New", "Existing", "Interrupted", "Duplicate"]
        self.app.albums = [{"title": title} for title in titles]
        review = self.complete_review(
            self.review_result("New"),
            self.review_result("Existing", "present"),
            self.review_result("Interrupted", "present", pending_create=True),
            self.review_result("Duplicate", "duplicate"),
        )
        base = {"operation": "upload", "albums": ["Existing"], "review_id": review["id"]}
        invalid = (
            {**base, "review_id": "stale"},
            base,
            {**base, "overwrite_titles": ["Existing"]},
            {**base, "overwrite_titles": ["Existing"], "confirmed": "true"},
            {**base, "albums": ["Duplicate"], "confirmed": True},
            {**base, "albums": ["New"], "overwrite_titles": ["New"], "confirmed": True},
            {**base, "albums": ["New"], "skipped_titles": ["New"]},
            {**base, "albums": ["New"], "skipped_titles": ["Outside"]},
        )
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(ui.UIError) as error:
                self.app.start(**request)
            self.assertEqual(error.exception.code, "INVALID_REVIEW")
            self.assertEqual(self.app.job["id"], review["id"])

        self.app.scan_revision += 1
        with self.assertRaises(ui.UIError) as error:
            self.app.start(**{**base, "albums": ["New"]})
        self.assertEqual(error.exception.code, "INVALID_REVIEW")
        self.app.scan_revision -= 1
        self.app.review_identity = ("default", "b" * 64)
        with self.assertRaises(ui.UIError) as error:
            self.app.start(**{**base, "albums": ["New"]})
        self.assertEqual(error.exception.code, "INVALID_REVIEW")
        self.app.review_identity = ("default", "a" * 64)

        lines = [f"VERIFIED {title}: card=synthetic; tracks=1; icons=0" for title in titles[:3]]
        with patch.object(self.app, "commands", return_value=self.worker(*lines)), \
                patch.object(self.app, "run_commands", wraps=self.app.run_commands) as run_commands:
            upload_job = self.app.start("upload", albums=titles[:3], review_id=review["id"],
                                        overwrite_titles=["Existing", "Interrupted"], confirmed=True,
                                        skipped_titles=["Duplicate"])
            snapshot = self.wait()
        command = run_commands.call_args.args[0][0]
        plan = json.loads(command[command.index("--review-plan") + 1])
        self.assertEqual([(item["title"], item["action"]) for item in plan],
                         [("New", "create"), ("Existing", "replace"), ("Interrupted", "resume")])
        self.assertTrue(all(item["localFingerprint"] == "1" * 64 for item in plan))
        self.assertEqual(snapshot["lastUpload"]["reviewSkipped"], ["Duplicate"])
        self.assertIsNone(snapshot["reviewInspection"])
        with self.assertRaises(ui.UIError) as error:
            self.app.start("upload", albums=["New"], review_id=review["id"])
        self.assertEqual(error.exception.code, "INVALID_REVIEW")
        self.assertEqual(self.app.job["id"], upload_job["id"])

    def test_rescan_disconnect_and_changed_sources_invalidate_review(self):
        self.app.save_config(self.config())
        self.add_saved_login()
        self.app.start("scan")
        self.wait()
        review = self.complete_review(self.review_result("Synthetic Album"))
        revision = self.app.scan_revision
        self.app.start("scan")
        after_scan = self.wait()
        self.assertEqual(after_scan["scanRevision"], revision + 1)
        self.assertIsNone(after_scan["reviewInspection"])
        with self.assertRaises(ui.UIError) as error:
            self.app.start("upload", albums=["Synthetic Album"], review_id=review["id"])
        self.assertEqual(error.exception.code, "INVALID_REVIEW")

        review = self.complete_review(self.review_result("Synthetic Album"))
        selected = self.collection / "01 - Synthetic Album" / "cover.png"
        self.assertTrue(self.app.select_asset("Synthetic Album", "cover", None, str(selected)))
        self.assertIsNone(self.app.snapshot()["reviewInspection"])
        with self.assertRaises(ui.UIError) as error:
            self.app.start("upload", albums=["Synthetic Album"], review_id=review["id"])
        self.assertEqual(error.exception.code, "INVALID_REVIEW")

        self.complete_review(self.review_result("Synthetic Album"))
        self.app.disconnect()
        self.assertIsNone(self.app.snapshot()["reviewInspection"])
        self.assertFalse(self.app.connection()["usable"])

    def test_unconfirmed_replacement_error_codes_are_preserved_in_result(self):
        self.app.save_config(self.config())
        self.add_saved_login()
        self.app.albums = [{"title": "Synthetic Album"}]
        for code in ("OVERWRITE_UNCONFIRMED", "OVERWRITE_CHANGED"):
            with self.subTest(code=code):
                review = self.complete_review(self.review_result("Synthetic Album", "present", pending_overwrite=True))
                script = (f"import sys; print('RuntimeError: {code}: Synthetic Album. "
                          "No new update was sent.', flush=True); sys.exit(1)")
                with patch.object(self.app, "commands", return_value=[[sys.executable, "-c", script]]):
                    self.app.start("upload", albums=["Synthetic Album"], review_id=review["id"],
                                   overwrite_titles=["Synthetic Album"], confirmed=True)
                    result = self.wait()
                self.assertEqual(result["job"]["state"], "failed")
                self.assertEqual(result["job"]["errorCode"], code)
                self.assertEqual(result["lastUpload"]["errorCode"], code)
                self.assertIn("No new update was sent", result["lastUpload"]["error"])

    def test_worker_failure_is_visible_and_invalid_scan_process_is_reaped(self):
        self.app.save_config(self.config())
        self.app.start("scan")
        valid = self.wait()["albums"][0]
        fake = [sys.executable, "-u", "-c",
                f"import time; print({json.dumps(valid)!r},flush=True); "
                "print('{\"invalid\":\"scan\"}',flush=True); time.sleep(30)"]
        with patch.object(self.app, "commands", return_value=[fake]):
            self.app.start("scan")
            result = self.wait()
        self.assertEqual(result["job"]["state"], "failed")
        self.assertIn("Invalid collection scan output", result["job"]["error"])
        self.assertEqual(result["albums"], [], "An incomplete scan must not be used for uploads")
        self.assertIsNone(self.app.process)
        self.assertFalse(self.app.busy())

    def test_auth_cancellation_reaps_the_callback_worker(self):
        self.app.save_config(self.config())
        fake = [sys.executable, "-u", "-c",
                "import time; print('LOGIN_URL http://127.0.0.1:8788/start',flush=True); time.sleep(30)"]
        with patch.object(self.app, "commands", return_value=[fake]):
            self.app.start("auth")
            deadline = time.monotonic() + 5
            while not self.app.snapshot()["job"]["loginUrl"] and time.monotonic() < deadline:
                time.sleep(.01)
            self.app.stop()
            result = self.wait()
        self.assertEqual(result["job"]["state"], "stopped")
        self.assertFalse(self.app.connection()["usable"])
        self.assertIsNone(self.app.process)

    def test_false_authorization_success_is_rejected(self):
        self.app.save_config(self.config())
        fake = [sys.executable, "-c", "print('no authorization saved')"]
        with patch.object(self.app, "commands", return_value=[fake]):
            self.app.start("auth")
            result = self.wait()
        self.assertEqual(result["job"]["state"], "failed")
        self.assertEqual(result["job"]["errorCode"], "ACCOUNT_AUTHORIZATION_UNUSABLE")

    def test_pause_signal_does_not_interrupt_a_card(self):
        self.app.save_config(self.config())
        self.add_saved_login()
        title = "Synthetic: 10 tiny tunes"
        self.app.albums = [{"title": title, "tracks": 1, "icons": 1, "cover": "cover.png"}]
        review = self.complete_review(self.review_result(title))
        script = (
            "import pathlib,time; "
            f"print('START {title}: 1 tracks',flush=True); "
            f"p=pathlib.Path({str(self.app.work / 'ui-stop')!r}); "
            "\nwhile not p.exists(): time.sleep(.01)\n"
            f"print('VERIFIED {title}: card=synthetic; tracks=1; icons=1',flush=True)\n"
            "print('PAUSED: current card finished',flush=True)\n"
        )
        with patch.object(self.app, "commands", return_value=[[sys.executable, "-u", "-c", script]]):
            self.app.start("upload", albums=[title], review_id=review["id"])
            deadline = time.monotonic() + 5
            while not self.app.snapshot()["job"]["currentAlbum"] and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(self.app.snapshot()["job"]["currentAlbum"], title)
            self.app.stop()
            result = self.wait()
        self.assertEqual(result["job"]["state"], "stopped")
        self.assertEqual(result["job"]["completed"], [title])

    def test_quit_rejects_an_active_task(self):
        self.app.job = {"state": "running"}
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/quit", {})
            self.assertEqual(error.exception.code, 409)
            error.exception.close()
            self.assertFalse(self.app.closing)
            self.assertEqual(self.request("/api/health")[0], 200)
        finally:
            self.app.job = None

    def test_quit_prevents_new_tasks_before_the_server_stops(self):
        response_ready = threading.Event()
        release_response = threading.Event()
        handler = self.server.RequestHandlerClass
        original_send = handler.send
        results = []

        def delayed_send(request, code, data, mime="application/json; charset=utf-8"):
            if request.path == "/api/quit" and code == 200:
                response_ready.set()
                if not release_response.wait(timeout=5):
                    raise RuntimeError("Timed out waiting to finish the synthetic quit")
            return original_send(request, code, data, mime)

        def quit_server():
            try:
                results.append(ui.local_ui_request(self.url, "/api/quit", {}, self.app.csrf))
            except Exception as error:
                results.append(error)

        with patch.object(handler, "send", delayed_send):
            worker = threading.Thread(target=quit_server)
            worker.start()
            try:
                self.assertTrue(response_ready.wait(timeout=5))
                status, result = ui.local_ui_request(
                    self.url, "/api/jobs", {"operation": "scan"}, self.app.csrf,
                )
                self.assertEqual((status, result["code"]), (409, "SHUTTING_DOWN"))
            finally:
                release_response.set()
                worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [(200, {"ok": True})])

    def test_failed_pause_request_does_not_claim_to_be_pausing(self):
        self.app.job = {"state": "running", "operation": "upload"}
        with patch.object(Path, "touch", side_effect=OSError("Synthetic disk failure")):
            with self.assertRaisesRegex(OSError, "Synthetic disk failure"):
                self.app.stop()
        self.assertEqual(self.app.job["state"], "running")
        self.assertFalse(self.app.stop_requested)
        self.app.job = None

    def test_cli_pause_finishes_only_one_card_and_preserves_saved_state(self):
        stop_file = self.directory / "pause"
        albums = [{"title": "One", "audio": [Path("one.mp3")]},
                  {"title": "Two", "audio": [Path("two.mp3")]}]
        args = argparse.Namespace(existing_only=False, verify_only=False, album=None, stop_file=stop_file)
        library = [{"title": "One"}, {"title": "Two"}]

        def finish_card(album, _):
            stop_file.touch()

        with patch.object(upload, "full_read_enabled", return_value=True), \
                patch.object(upload, "request_json", return_value={"cards": library}), \
                patch.object(upload, "run_album", side_effect=finish_card) as run:
            upload.run_selected(args, albums)
        self.assertEqual(run.call_count, 1)

    @unittest.skipUnless(os.name == "posix", "The native launcher uses a POSIX file lock")
    def test_native_launch_reuses_the_running_workspace_without_opening_a_browser(self):
        (self.app.base / "ui-launch.log").write_text(f"YOTO_UI_READY {self.url}\n", encoding="utf-8")
        with patch.object(ui.subprocess, "Popen") as start, patch.object(ui.webbrowser, "open") as browser:
            self.assertEqual(ui.ensure_ui_server(self.app.base), self.url)
        start.assert_not_called()
        browser.assert_not_called()

        other = self.directory / "another-workspace"
        other.mkdir()
        (other / "ui-launch.log").write_text(f"YOTO_UI_READY {self.url}\n", encoding="utf-8")
        self.assertIsNone(ui.recorded_ui_server(other))

    @unittest.skipUnless(os.name == "posix", "The native launcher uses a POSIX file lock")
    def test_previous_service_needs_confirmation_and_never_stops_a_running_task(self):
        base_handler = ui.handler_for(self.app)

        class LegacyHandler(base_handler):
            def do_GET(self):
                if self.path == "/api/health":
                    self.send(200, {"app": "yoto-myo-ui", "ready": True, "apiVersion": 10})
                else:
                    super().do_GET()

        legacy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), LegacyHandler)
        legacy.daemon_threads = True

        def serve_legacy():
            try:
                legacy.serve_forever()
            finally:
                legacy.server_close()

        thread = threading.Thread(target=serve_legacy, daemon=True)
        thread.start()

        def close_legacy():
            if thread.is_alive():
                legacy.shutdown()
            legacy.server_close()
            thread.join(timeout=5)

        self.addCleanup(close_legacy)
        url = f"http://127.0.0.1:{legacy.server_port}"
        (self.app.base / "ui-launch.log").write_text(f"YOTO_UI_READY {url}\n", encoding="utf-8")
        command = [
            sys.executable, str(ui.PROJECT / "yoto_ui.py"), "--native-launch",
            "--work-dir", str(self.app.base),
        ]
        first = subprocess.run(command, capture_output=True, text=True, timeout=25)
        self.assertEqual((first.returncode, first.stdout.strip()), (42, "YOTO_UI_RESTART_REQUIRED"))
        with self.assertRaises(ui.OutdatedServiceError):
            ui.ensure_ui_server(self.app.base)
        self.assertEqual(ui.local_ui_request(url, "/api/health")[0], 200)

        self.app.job = {"state": "running"}
        try:
            with patch.object(ui.subprocess, "Popen") as start:
                with self.assertRaisesRegex(RuntimeError, "running task"):
                    ui.ensure_ui_server(self.app.base, replace_idle=True)
            start.assert_not_called()
            self.assertEqual(ui.local_ui_request(url, "/api/health")[0], 200)
        finally:
            self.app.job = None

        restarted = subprocess.run(command + ["--replace-idle-service"], capture_output=True, text=True, timeout=25)
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertTrue(restarted.stdout.startswith("YOTO_UI_NATIVE "), restarted.stdout)
        new_url = restarted.stdout.strip().split()[1]
        self.addCleanup(self.stop_launched_service, new_url)
        self.assertNotEqual(new_url, url)
        status, health = ui.local_ui_request(new_url, "/api/health")
        self.assertEqual((status, health["apiVersion"]), (200, ui.UI_API_VERSION))

    @unittest.skipUnless(os.name == "posix", "The native launcher uses a POSIX file lock")
    def test_native_launch_starts_one_server_and_reopens_it(self):
        base = self.directory / "native-launch"
        command = [sys.executable, str(ui.PROJECT / "yoto_ui.py"), "--native-launch", "--work-dir", str(base)]
        url = None
        try:
            first = subprocess.run(command, capture_output=True, text=True, timeout=25)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertTrue(first.stdout.startswith("YOTO_UI_NATIVE "), first.stdout)
            url = first.stdout.strip().split()[1]
            with urllib.request.urlopen(url + "/api/bootstrap", timeout=5) as response:
                csrf = json.load(response)["csrf"]
            request = urllib.request.Request(
                url + "/api/config", data=json.dumps(self.config()).encode(),
                headers={"X-Yoto-CSRF": csrf, "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)

            second = subprocess.run(command, capture_output=True, text=True, timeout=25)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(second.stdout.strip(), f"YOTO_UI_NATIVE {url}")
            with urllib.request.urlopen(url + "/api/status", timeout=5) as response:
                self.assertEqual(json.load(response)["config"]["root"], str(self.collection.resolve()))
            log = (base / "ui-launch.log").read_text(encoding="utf-8")
            self.assertEqual(log.count("YOTO_UI_READY "), 1)
        finally:
            if url is None and (base / "ui-launch.log").exists():
                for line in (base / "ui-launch.log").read_text(encoding="utf-8").splitlines():
                    if line.startswith("YOTO_UI_READY "):
                        url = line.split()[1]
            if url:
                self.stop_launched_service(url)

    def test_actual_entrypoint_serves_and_quits_cleanly_without_opening_a_browser(self):
        process = subprocess.Popen(
            [sys.executable, "-u", str(ui.PROJECT / "yoto_ui.py"), "--no-open",
             "--work-dir", str(self.directory / "entrypoint-private")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        )
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=10), "Entrypoint did not become ready")
            line = process.stdout.readline()
            self.assertTrue(line.startswith("YOTO_UI_READY http://127.0.0.1:"), line)
            url = line.strip().split()[1]
            with urllib.request.urlopen(url + "/api/bootstrap", timeout=5) as response:
                csrf = json.load(response)["csrf"]
            request = urllib.request.Request(
                url + "/api/quit", data=b"{}", headers={"X-Yoto-CSRF": csrf, "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertTrue(json.load(response)["ok"])
            output, _ = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, output)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()


@unittest.skipUnless(os.name == "posix", "The macOS build script uses POSIX shell commands")
class MacOSBuildTests(unittest.TestCase):
    def test_build_refuses_to_change_separate_app_with_finder_customization(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as temporary:
            directory = Path(temporary)
            tools = directory / "tools"
            tools.mkdir()
            xattr = tools / "xattr"
            xattr.write_text("#!/bin/sh\nprintf '%s\\n' com.apple.FinderInfo\n", encoding="utf-8")
            xattr.chmod(0o755)
            compiler = tools / "xcrun"
            compiler.write_text("#!/bin/sh\nprintf 'unexpected compiler invocation\\n' >&2\nexit 1\n", encoding="utf-8")
            compiler.chmod(0o755)
            environment = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ.get("PATH", ""))
            app = directory / "Yotolio Native.app"
            macos = app / "Contents" / "MacOS"
            macos.mkdir(parents=True)
            output = macos / "Yotolio"
            output.write_bytes(b"existing executable")
            command = ["/bin/sh", str(ui.PROJECT / "build_macos_app.sh"), str(output)]

            result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=10)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Finder customization", result.stderr)
            self.assertEqual(output.read_bytes(), b"existing executable")

            output.unlink()
            macos.rmdir()
            result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Finder customization", result.stderr)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
