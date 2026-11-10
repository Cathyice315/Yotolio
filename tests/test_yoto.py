import argparse
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

import yoto_authorize as auth
import yoto_upload as upload


def png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def rgba_png():
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 6, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress((b"\0" + b"\x10\x20\x30\xff" * 16) * 16))
        + png_chunk(b"IEND", b"")
    )


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "collection"
        self.root.mkdir()
        self.work = Path(self.temporary.name) / "private"
        self.work.mkdir()
        self.state = {"audio": {}, "covers": {}, "icons": {}, "albums": {}}
        self.globals = patch.multiple(
            upload, ROOT=self.root, WORK=self.work,
            STATE_FILE=self.work / "upload-state.json", STATE=self.state,
        )
        self.globals.start()
        self.addCleanup(self.globals.stop)

    def make_album(self, count=2, folder_name="01 - Example Songs", parent=None):
        folder = (parent or self.root) / folder_name
        folder.mkdir(parents=True)
        (folder / "cover.jpg").write_bytes(b"synthetic cover, not a licensed image")
        for number in range(1, count + 1):
            (folder / f"{number} - Song.mp3").write_bytes(f"synthetic audio {number}".encode())
            (folder / f"Icon {number}.png").write_bytes(rgba_png())
        return folder

    def album_and_payload(self):
        self.make_album()
        upload.prepare_covers()
        album = upload.inventory()[0]
        media = [
            {"transcodedSha256": f"synthetic-audio-{i}",
             "info": {"duration": 10, "fileSize": 100, "channels": "stereo", "format": "opus"}}
            for i in range(len(album["audio"]))
        ]
        icon_key, _ = upload.icon_cache_key(rgba_png())
        icons = ["synthetic-icon"] * len(media)
        self.state["icons"][icon_key] = icons[0]
        for path, data in zip(album["audio"], media, strict=True):
            self.state["audio"][upload.digest_file(path)] = data
        cover = "https://example.invalid/synthetic-cover.jpg"
        self.state["covers"][upload.digest_file(album["cover"])] = cover
        payload = upload.build_payload(album, media, cover, icons)
        card = {**copy.deepcopy(payload), "cardId": "synthetic-card", "updatedAt": "synthetic-version"}
        self.state["albums"][album["title"]] = {
            "status": "verified", "cardId": card["cardId"], "payload": copy.deepcopy(payload),
            "iconIds": icons, "iconsVerified": True, "sourceUploaded": True,
        }
        return album, payload, card, icons

    def test_nested_inventory_natural_order_and_icon_pairing(self):
        self.make_album(count=12, parent=self.root / "collection group")
        upload.prepare_covers()
        albums = upload.inventory()
        self.assertEqual(len(albums), 1)
        self.assertEqual(albums[0]["title"], "Example Songs")
        self.assertEqual([p.name for p in albums[0]["audio"]],
                         [f"{i} - Song.mp3" for i in range(1, 13)])
        self.assertEqual([p.name for p in albums[0]["icons"]],
                         [f"Icon {i}.png" for i in range(1, 13)])

    def test_missing_and_duplicate_numbered_icons_fail(self):
        folder = self.make_album()
        upload.prepare_covers()
        icon = folder / "Icon 2.png"
        icon.unlink()
        with self.assertRaisesRegex(RuntimeError, "numbering mismatch"):
            upload.inventory()
        icon.write_bytes(rgba_png())
        (folder / "01.png").write_bytes(rgba_png())
        with self.assertRaisesRegex(RuntimeError, "Duplicate icon"):
            upload.inventory()

    def test_cover_fallback_copies_exact_bytes_and_never_overwrites(self):
        folder = self.make_album()
        original = folder / "cover.jpg"
        renamed = folder / "unlabelled-photo.jpg"
        original.rename(renamed)
        before = upload.digest_file(renamed)
        upload.prepare_covers()
        target = self.root / "Covers" / (folder.name + ".jpg")
        self.assertEqual(upload.digest_file(target), before)
        self.assertEqual(upload.digest_file(renamed), before)
        renamed.write_bytes(b"changed source")
        upload.prepare_covers()
        self.assertEqual(upload.digest_file(target), before)

    def test_ambiguous_cover_stops_before_copying_other_albums(self):
        self.make_album(folder_name="01 - Good")
        bad = self.make_album(folder_name="02 - Ambiguous")
        (bad / "second-cover.jpg").write_bytes(b"synthetic second cover")
        with self.assertRaisesRegex(RuntimeError, "Cannot choose one cover"):
            upload.prepare_covers()
        self.assertFalse((self.root / "Covers").exists())

    def test_payload_shuffle_both_icons_and_server_rounding(self):
        album, payload, card, icons = self.album_and_payload()
        upload.check_icons(card, icons)
        self.assertEqual(card["content"]["config"]["shuffle"], [{"start": 0, "end": 1, "limit": 2}])
        track = card["content"]["chapters"][0]["tracks"][0]
        track["duration"] += 1
        upload.check_card(card, album, payload)
        track["duration"] += 1
        with self.assertRaisesRegex(RuntimeError, "duration verification"):
            upload.check_card(card, album, payload)
        track["display"]["icon16x16"] = "yoto:#wrong-icon"
        with self.assertRaisesRegex(RuntimeError, "Icon verification"):
            upload.check_icons(card, icons)

    def test_icon_upload_preserves_native_png_and_converts_other_formats(self):
        folder = self.make_album()
        native = folder / "Icon 1.png"
        other = folder / "palette.gif"
        other.write_bytes(b"GIF89a synthetic")
        calls = []

        def fake_api(route, **kwargs):
            calls.append(kwargs)
            return {"displayIcon": {"mediaId": f"synthetic-{len(calls)}"}}

        with patch.object(upload, "request_json", side_effect=fake_api):
            first = upload.upload_icon(native)
            self.assertEqual(upload.upload_icon(native), first)
            upload.upload_icon(other)
        self.assertEqual([c["query"]["autoConvert"] for c in calls], ["false", "true"])
        self.assertEqual(calls[0]["body"], native.read_bytes())

    def test_fill_icons_preserves_all_other_content_and_saves_snapshot(self):
        album, _, card, icons = self.album_and_payload()
        card["content"] = upload.without_icons(card["content"])
        card["content"]["chapters"][0]["key"] = "keep-random-key"
        card["content"]["chapters"][0]["display"] = {"otherDisplayField": "keep"}
        before = copy.deepcopy(card)
        remote = copy.deepcopy(card)

        def fake_api(route, method="GET", body=None, **_):
            nonlocal remote
            if method == "POST":
                self.assertEqual(route, "/content")
                remote = {**copy.deepcopy(body), "updatedAt": "new-version"}
            return {"card": copy.deepcopy(remote)}

        with patch.object(upload, "request_json", side_effect=fake_api):
            upload.fill_existing_icons(card, album, icons)
        self.assertEqual(card, before)
        self.assertEqual(upload.without_icons(remote["content"]), upload.without_icons(before["content"]))
        self.assertEqual(remote["metadata"], before["metadata"])
        backup = self.work / "before-icons" / "synthetic-card.json"
        self.assertEqual(json.loads(backup.read_text()), before)
        upload.check_icons(remote, icons)

    def test_concurrent_change_and_nonempty_icon_are_never_overwritten(self):
        album, _, card, icons = self.album_and_payload()
        card["content"] = upload.without_icons(card["content"])
        changed = {**copy.deepcopy(card), "title": "Concurrent human edit"}
        with patch.object(upload, "request_json", return_value={"card": changed}) as api:
            with self.assertRaisesRegex(RuntimeError, "changed during icon upload"):
                upload.fill_existing_icons(card, album, icons)
            self.assertEqual(api.call_count, 1)
        card["content"]["chapters"][0]["display"] = {"icon16x16": "yoto:#keep-human-icon"}
        with patch.object(upload, "request_json") as api:
            with self.assertRaisesRegex(RuntimeError, "Refusing to overwrite"):
                upload.fill_existing_icons(card, album, icons)
            api.assert_not_called()

    def test_verify_only_is_get_only_and_leaves_state_unchanged(self):
        album, _, card, _ = self.album_and_payload()
        before = copy.deepcopy(self.state)
        upload.save_state()
        disk_before = upload.STATE_FILE.read_bytes()
        args = argparse.Namespace(existing_only=False, verify_only=True, album=None)

        def fake_api(route, method="GET", **_):
            self.assertEqual(method, "GET")
            return {"cards": [card]} if route == "/content/mine" else {"card": card}

        with patch.object(upload, "full_read_enabled", return_value=True), \
                patch.object(upload, "request_json", side_effect=fake_api), \
                patch.object(upload, "upload_icons", side_effect=AssertionError("Must not upload")):
            upload.run_selected(args, [album])
            album["audio"][0].write_bytes(b"changed local audio")
            with self.assertRaisesRegex(RuntimeError, "Local audio differs"):
                upload.run_selected(args, [album])
        self.assertEqual(self.state, before)
        self.assertEqual(upload.STATE_FILE.read_bytes(), disk_before)

    def test_baseline_only_cards_detect_non_icon_edits(self):
        album, _, card, _ = self.album_and_payload()
        self.state["albums"][album["title"]]["sourceUploaded"] = False
        self.state["audio"].clear()
        with patch.object(upload, "request_json", return_value={"card": card}):
            upload.verify_album(album, [card])
            card["content"]["config"]["onlineOnly"] = True
            with self.assertRaisesRegex(RuntimeError, "retained baseline"):
                upload.verify_album(album, [card])

    def test_ambiguous_pending_create_cannot_create_duplicate(self):
        album, _, _, _ = self.album_and_payload()
        self.state["albums"][album["title"]]["status"] = "pending_create"
        with patch.object(upload, "request_json") as api:
            with self.assertRaisesRegex(RuntimeError, "Unreconciled prior create"):
                upload.run_album(album, [])
            api.assert_not_called()

    def test_process_lock_rejects_another_writer(self):
        with upload.work_lock():
            with self.assertRaisesRegex(RuntimeError, "Another uploader"):
                with upload.work_lock():
                    self.fail("A second writer acquired the work directory")


class AuthorizationTests(unittest.TestCase):
    def test_pkce_query_mode_and_explicit_view_manage_scopes(self):
        verifier = "synthetic-pkce-verifier"
        params = auth.authorization_parameters("SYNTHETIC_CLIENT", "http://127.0.0.1:8788/callback",
                                               verifier, "synthetic-state")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(params["code_challenge"], challenge)
        self.assertEqual(params["code_challenge_method"], "S256")
        self.assertEqual(params["response_mode"], "query")
        self.assertTrue({"openid", "user:content:view", "user:content:manage"}.issubset(params["scope"].split()))

    def test_token_is_private_and_replacement_requires_explicit_flag(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            path = Path(directory) / "authorization.json"
            auth.save_token(path, {"access_token": "synthetic-not-a-real-token"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                auth.save_token(path, {"access_token": "must-not-overwrite"})
            auth.save_token(path, {"access_token": "synthetic-replacement"}, replace=True)
            self.assertEqual(json.loads(path.read_text())["access_token"], "synthetic-replacement")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
