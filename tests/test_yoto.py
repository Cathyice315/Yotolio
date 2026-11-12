import argparse
import base64
import copy
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import zlib

import yoto_accounts as accounts
import yoto_authorize as auth
import yoto_discovery as discovery
import yoto_tls as tls
import yoto_upload as upload


class TLSTests(unittest.TestCase):
    def test_macos_uses_system_bundle_only_when_no_store_is_configured(self):
        context = Mock()
        context.cert_store_stats.return_value = {"x509_ca": 128}
        with patch.object(tls.sys, "platform", "darwin"), patch.dict(os.environ, {}, clear=True), \
                patch.object(tls.ssl, "get_default_verify_paths",
                             return_value=SimpleNamespace(cafile=None, capath=None)), \
                patch.object(tls.Path, "is_file", return_value=True), \
                patch.object(tls.ssl, "create_default_context", return_value=context) as create:
            self.assertTrue(tls.configure_macos_trust())
            self.assertEqual(os.environ["SSL_CERT_FILE"], "/etc/ssl/cert.pem")
            create.assert_called_once_with(cafile="/etc/ssl/cert.pem")

    def test_explicit_and_existing_trust_stores_are_not_replaced(self):
        for key in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
            with self.subTest(key=key), patch.object(tls.sys, "platform", "darwin"), \
                    patch.dict(os.environ, {key: "/synthetic/custom-trust"}, clear=True), \
                    patch.object(tls.ssl, "create_default_context") as create:
                self.assertFalse(tls.configure_macos_trust())
                self.assertEqual(os.environ[key], "/synthetic/custom-trust")
                create.assert_not_called()
        for paths in (SimpleNamespace(cafile="/existing/cert.pem", capath=None),
                      SimpleNamespace(cafile=None, capath="/existing/certs")):
            with self.subTest(paths=paths), patch.object(tls.sys, "platform", "darwin"), \
                    patch.dict(os.environ, {}, clear=True), \
                    patch.object(tls.ssl, "get_default_verify_paths", return_value=paths), \
                    patch.object(tls.ssl, "create_default_context") as create:
                self.assertFalse(tls.configure_macos_trust())
                create.assert_not_called()

    def test_missing_or_invalid_system_bundle_is_reported(self):
        with patch.object(tls.sys, "platform", "darwin"), patch.dict(os.environ, {}, clear=True), \
                patch.object(tls.ssl, "get_default_verify_paths",
                             return_value=SimpleNamespace(cafile=None, capath=None)), \
                patch.object(tls.Path, "is_file", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "no trusted certificate store"):
                tls.configure_macos_trust()
            self.assertNotIn("SSL_CERT_FILE", os.environ)

        for invalid in (ssl.SSLError("synthetic invalid bundle"), None):
            context = Mock()
            context.cert_store_stats.return_value = {"x509_ca": 0}
            with self.subTest(invalid=invalid), patch.object(tls.sys, "platform", "darwin"), \
                    patch.dict(os.environ, {}, clear=True), \
                    patch.object(tls.ssl, "get_default_verify_paths",
                                 return_value=SimpleNamespace(cafile=None, capath=None)), \
                    patch.object(tls.Path, "is_file", return_value=True), \
                    patch.object(tls.ssl, "create_default_context", return_value=context, side_effect=invalid):
                with self.assertRaisesRegex(RuntimeError, "certificate bundle"):
                    tls.configure_macos_trust()
                self.assertNotIn("SSL_CERT_FILE", os.environ)

    def test_certificate_error_is_distinguished_from_other_network_errors(self):
        self.assertTrue(tls.certificate_error(urllib.error.URLError(ssl.SSLCertVerificationError("synthetic"))))
        self.assertFalse(tls.certificate_error(urllib.error.URLError("synthetic network failure")))


def png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def rgba_png():
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 6, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress((b"\0" + b"\x10\x20\x30\xff" * 16) * 16))
        + png_chunk(b"IEND", b"")
    )


def jpeg_header(width, height):
    return (b"\xff\xd8\xff\xe0\x00\x04xx"
            + b"\xff\xc0\x00\x0b\x08" + struct.pack(">HH", height, width)
            + b"\x01\x01\x11\x00\xff\xd9")


def tagged_mp3(album, title, track):
    frames = []
    for identifier, value in ((b"TALB", album), (b"TIT2", title), (b"TRCK", str(track))):
        body = b"\x03" + value.encode("utf-8")
        frames.append(identifier + struct.pack(">I", len(body)) + b"\0\0" + body)
    data = b"".join(frames)
    size = len(data)
    syncsafe = bytes(((size >> 21) & 127, (size >> 14) & 127, (size >> 7) & 127, size & 127))
    return b"ID3\x03\x00\x00" + syncsafe + data + b"synthetic audio"


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

    def test_certificate_failure_is_actionable_and_not_retried(self):
        error = urllib.error.URLError(ssl.SSLCertVerificationError("synthetic"))
        with patch.object(upload, "token_data", return_value={"access_token": "synthetic"}), \
                patch.object(upload.urllib.request, "urlopen", side_effect=error) as request, \
                patch.object(upload.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "secure connection could not be verified"):
                upload.request_json("/synthetic")
            self.assertEqual(request.call_count, 1)
            sleep.assert_not_called()

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

    def test_generic_audio_and_image_subfolders_preserve_each_card(self):
        examples = (
            ("01 - First Stories", "audio_files", "image_files"),
            ("02 - Second Stories", "Audio-Files", "Image Files"),
            ("03 - Third Stories", "audio files", "image-assets"),
        )
        for folder_name, audio_name, image_name in examples:
            album = self.root / folder_name
            audio, images = album / audio_name, album / image_name
            audio.mkdir(parents=True)
            images.mkdir()
            (images / "cover_image.jpg").write_bytes(jpeg_header(638, 1011))
            for number in (1, 2):
                (audio / f"{number}. Story.mp3").write_bytes(b"synthetic audio")
                (images / f"Icon {number:02}.png").write_bytes(rgba_png())

        albums = upload.inventory()
        self.assertEqual([album["title"] for album in albums],
                         ["First Stories", "Second Stories", "Third Stories"])
        for album, (folder_name, audio_name, image_name) in zip(albums, examples, strict=True):
            folder = (self.root / folder_name).resolve()
            self.assertEqual(album["folder"], folder)
            self.assertEqual(album["cover"], folder / image_name / "cover_image.jpg")
            self.assertEqual([path.parent for path in album["audio"]], [folder / audio_name] * 2)
            self.assertEqual([path.name for path in album["icons"]], ["Icon 01.png", "Icon 02.png"])
            self.assertEqual(album["unmatched_icons"], [])

    def test_duplicate_card_names_still_stop_before_upload(self):
        for folder_name in ("01 - Same Stories", "02 - Same Stories"):
            audio = self.root / folder_name / "audio_files"
            audio.mkdir(parents=True)
            (audio / "1. Story.mp3").write_bytes(b"synthetic audio")

        with self.assertRaisesRegex(RuntimeError, r"Same Stories.*Nothing was uploaded"):
            upload.inventory()

    def test_missing_and_ambiguous_icons_are_reported_without_guessing(self):
        folder = self.make_album()
        upload.prepare_covers()
        icon = folder / "Icon 2.png"
        icon.unlink()
        album = upload.inventory()[0]
        self.assertIsNone(album["icons"][1])
        self.assertEqual(album["icon_details"][1]["reason"], "missing")
        icon.write_bytes(rgba_png())
        (folder / "01.png").write_bytes(rgba_png())
        album = upload.inventory()[0]
        self.assertIsNone(album["icons"][0])
        self.assertEqual(album["icon_details"][0]["reason"], "ambiguous")
        self.assertEqual(len(album["icon_details"][0]["candidates"]), 2)

    def test_jpeg_dimensions_distinguish_cover_from_unmatched_icons(self):
        folder = self.root / "Album"
        folder.mkdir()
        (folder / "01 - Song.mp3").write_bytes(b"synthetic audio")
        (folder / "01 - Song.jpg").write_bytes(jpeg_header(600, 800))
        (folder / "unmatched.jpg").write_bytes(jpeg_header(16, 16))
        (folder / "broken.jpg").write_bytes(b"\xff\xd8\xff\xe0\x00")
        album = upload.inventory()[0]
        self.assertEqual(discovery._image_size(folder / "01 - Song.jpg"), (600, 800))
        self.assertIsNone(discovery._image_size(folder / "broken.jpg"))
        self.assertEqual(album["cover"].name, "01 - Song.jpg")
        self.assertIsNone(album["icons"][0])
        self.assertEqual({path.name for path in album["unmatched_icons"]}, {"broken.jpg", "unmatched.jpg"})

    def test_multiple_large_unnamed_covers_remain_ambiguous(self):
        folder = self.root / "Album"
        folder.mkdir()
        (folder / "song.mp3").write_bytes(b"synthetic audio")
        (folder / "photo-a.jpg").write_bytes(jpeg_header(500, 500))
        (folder / "song.jpg").write_bytes(jpeg_header(600, 800))
        album = upload.inventory()[0]
        self.assertIsNone(album["cover"])
        self.assertEqual(album["cover_status"], "ambiguous")
        self.assertEqual(len(album["cover_candidates"]), 2)
        self.assertIsNone(album["icons"][0], "An ambiguous cover candidate must not become a song icon")

    def test_large_unnamed_cover_without_audio_is_a_blocked_card(self):
        folder = self.root / "Artwork Only"
        folder.mkdir()
        (folder / "art.jpg").write_bytes(jpeg_header(600, 800))
        album = upload.inventory()[0]
        self.assertEqual((album["title"], album["cover_status"]), ("Artwork Only", "provided"))
        self.assertEqual(album["audio"], [])

    def test_cover_only_card_is_named_for_parent_of_image_files(self):
        images = self.root / "Artwork Only" / "image_files"
        images.mkdir(parents=True)
        (images / "cover.jpg").write_bytes(jpeg_header(600, 800))
        album = upload.inventory()[0]
        self.assertEqual((album["title"], album["cover_status"]), ("Artwork Only", "provided"))
        self.assertEqual(album["audio"], [])

    def test_audio_is_discovered_even_in_a_folder_named_covers(self):
        folder = self.root / "Covers"
        folder.mkdir()
        (folder / "song.mp3").write_bytes(b"synthetic audio")
        (folder / "cover.jpg").write_bytes(jpeg_header(600, 800))
        (folder / "song.png").write_bytes(rgba_png())
        (folder / "unmatched.png").write_bytes(rgba_png())
        album = upload.inventory()[0]
        self.assertEqual([path.name for path in album["audio"]], ["song.mp3"])
        self.assertEqual(album["cover"].name, "cover.jpg")
        self.assertEqual(album["icons"][0].name, "song.png")
        self.assertEqual([path.name for path in album["unmatched_icons"]], ["unmatched.png"])

    def test_loose_audio_can_use_a_cover_inside_covers_folder(self):
        (self.root / "song.mp3").write_bytes(b"synthetic audio")
        folder = self.root / "Covers"
        folder.mkdir()
        (folder / "cover.jpg").write_bytes(jpeg_header(600, 800))
        self.assertEqual(upload.inventory()[0]["cover"].name, "cover.jpg")

    def test_loose_tagged_audio_is_grouped_ordered_and_matched_by_track_tags(self):
        (self.root / "zebra.mp3").write_bytes(tagged_mp3("Ocean", "Waves", 2))
        (self.root / "apple.mp3").write_bytes(tagged_mp3("Ocean", "Shells", 1))
        (self.root / "tree.mp3").write_bytes(tagged_mp3("Forest", "Bird", 1))
        for name in ("Waves.png", "Shells.png", "Bird.png"):
            (self.root / name).write_bytes(rgba_png())
        for name in ("Ocean.jpg", "Forest.jpg"):
            (self.root / name).write_bytes(b"synthetic cover")
        albums = {album["title"]: album for album in upload.inventory()}
        self.assertEqual(set(albums), {"Ocean", "Forest"})
        self.assertEqual([path.name for path in albums["Ocean"]["audio"]], ["apple.mp3", "zebra.mp3"])
        self.assertEqual([path.name for path in albums["Ocean"]["icons"]], ["Shells.png", "Waves.png"])
        self.assertEqual(albums["Ocean"]["cover"].name, "Ocean.jpg")
        self.assertEqual(albums["Forest"]["cover"].name, "Forest.jpg")

    def test_placeholder_and_optional_icon_payload_are_stable(self):
        (self.root / "One.mp3").write_bytes(b"synthetic audio")
        album = upload.inventory()[0]
        self.assertIsNone(album["cover"])
        self.assertEqual(album["cover_status"], "missing")
        self.assertEqual(album["icons"], [None])
        first = discovery.cover_data(album)
        self.assertEqual(first, discovery.cover_data(album))
        self.assertTrue(first[0].startswith(b"\x89PNG\r\n\x1a\n"))
        audio = [{"transcodedSha256": "synthetic", "info": {
            "duration": 3, "fileSize": 10, "channels": "mono", "format": "opus",
        }}]
        payload = upload.build_payload(album, audio, "https://example.invalid/placeholder.png", [None])
        chapter = payload["content"]["chapters"][0]
        self.assertNotIn("display", chapter)
        self.assertNotIn("display", chapter["tracks"][0])
        upload.check_icons(payload, [None])

    def test_partial_card_can_be_completed_and_interrupted_cover_update_reconciled(self):
        folder = self.make_album()
        (folder / "cover.jpg").unlink()
        (folder / "Icon 2.png").unlink()
        album = upload.inventory()[0]
        remote = {"card": None, "posts": 0, "covers": 0, "reads": 0,
                  "concurrent": False, "interrupt": False}

        def fake_audio(path):
            number = path.name.split()[0]
            media = {"transcodedSha256": f"audio-{number}", "info": {
                "duration": 3, "fileSize": 10, "channels": "mono", "format": "opus",
            }}
            self.state["audio"][upload.digest_file(path)] = media
            return media

        def fake_api(route, method="GET", body=None, **_):
            if route == "/content/mine":
                return {"cards": [copy.deepcopy(remote["card"])] if remote["card"] else []}
            if route == "/media/coverImage/user/me/upload":
                remote["covers"] += 1
                return {"coverImage": {"mediaUrl": f"https://example.invalid/cover-{remote['covers']}"}}
            if route == "/media/displayIcons/user/me/upload":
                return {"displayIcon": {"mediaId": "synthetic-icon"}}
            if route == "/content" and method == "POST":
                remote["posts"] += 1
                remote["card"] = {
                    **copy.deepcopy(body), "cardId": "synthetic-card", "updatedAt": f"v{remote['posts']}",
                }
                if remote["interrupt"]:
                    remote["interrupt"] = False
                    raise RuntimeError("Synthetic interrupted response")
                return {"card": copy.deepcopy(remote["card"])}
            if route == "/content/synthetic-card":
                remote["reads"] += 1
                if remote["concurrent"] and remote["reads"] == 2:
                    remote["card"]["metadata"]["cover"]["imageL"] = "https://example.invalid/human-edit"
                    remote["card"]["updatedAt"] = "human-edit"
                return {"card": copy.deepcopy(remote["card"])}
            self.fail(f"Unexpected synthetic API request: {method} {route}")

        with patch.object(upload, "upload_audio", side_effect=fake_audio), \
                patch.object(upload, "request_json", side_effect=fake_api), \
                patch.object(upload, "full_read_enabled", return_value=True):
            upload.run_album(album, [])
            saved = self.state["albums"][album["title"]]
            self.assertTrue(saved["coverPlaceholder"])
            self.assertEqual(saved["iconIds"], ["synthetic-icon", None])
            self.assertNotIn("display", remote["card"]["content"]["chapters"][1])
            original = copy.deepcopy(remote["card"])

            (folder / "cover.jpg").write_bytes(b"new synthetic real cover")
            (folder / "Icon 2.png").write_bytes(rgba_png())
            completed = upload.inventory()[0]
            library = [remote["card"]]
            remote.update(reads=0, concurrent=True)
            with self.assertRaisesRegex(RuntimeError, "changed during icon upload"):
                upload.run_album(completed, library)
            self.assertEqual(remote["posts"], 1, "A concurrent change must not be overwritten")
            self.assertIn("pendingCoverUpdate", saved)
            with self.assertRaisesRegex(RuntimeError, "Cover changed during an unfinished update"):
                upload.run_album(completed, [remote["card"]])
            self.assertEqual(remote["posts"], 1)

            remote.update(card=original, reads=0, concurrent=False, interrupt=True)
            with self.assertRaisesRegex(RuntimeError, "interrupted response"):
                upload.run_album(completed, [original])
            self.assertEqual(remote["posts"], 2)
            self.assertIn("pendingCoverUpdate", self.state["albums"][album["title"]])
            upload.run_album(completed, [remote["card"]])
            self.assertEqual(remote["posts"], 2, "A completed update must not be posted twice")
            saved = self.state["albums"][album["title"]]
            self.assertFalse(saved["coverPlaceholder"])
            self.assertNotIn("pendingCoverUpdate", saved)
            self.assertEqual(saved["iconIds"], ["synthetic-icon", "synthetic-icon"])
            upload.verify_album(completed, [remote["card"]])

    def test_existing_unowned_cover_is_never_replaced(self):
        album, _, card, _ = self.album_and_payload()
        album["cover"].write_bytes(b"another synthetic cover")
        with patch.object(upload, "full_read_enabled", return_value=True), \
                patch.object(upload, "request_json", return_value={"card": card}), \
                patch.object(upload, "upload_cover", side_effect=AssertionError("Must not replace existing cover")):
            upload.run_album(album, [card])
        self.assertEqual(self.state["albums"][album["title"]]["payload"]["metadata"], card["metadata"])

    def test_ineligible_cards_are_skipped_and_existing_only_does_not_replace_covers(self):
        args = argparse.Namespace(existing_only=True, verify_only=False, album=None, stop_file=None)
        albums = [{"title": "Good", "audio": [Path("one.mp3")]},
                  {"title": "Empty", "audio": []}]
        library = [{"title": "Good"}, {"title": "Empty"}]
        with patch.object(upload, "full_read_enabled", return_value=True), \
                patch.object(upload, "request_json", return_value={"cards": library}), \
                patch.object(upload, "run_album") as run:
            upload.run_selected(args, albums)
        run.assert_called_once_with(albums[0], library, replace_placeholder=False)

    def test_verified_parent_grant_allows_review_but_unverified_grant_does_not(self):
        args = argparse.Namespace(album=None, existing_only=False, verify_only=False,
                                  inspect_only=True, stop_file=None)
        album = {"title": "Synthetic", "audio": [Path("one.mp3")]}
        token = {"scope": "user:content:manage", "yotolio_verified_scopes": ["user:content:view"]}
        with patch.object(upload, "token_data", return_value=token), \
                patch.object(upload, "request_json", return_value={"cards": []}) as request, \
                patch.object(upload, "inspect_album", return_value={"title": "Synthetic"}) as inspect:
            upload.run_selected(args, [album])
            request.assert_called_once_with("/content/mine")
            inspect.assert_called_once_with(album, [])
        with patch.object(upload, "token_data", return_value={"scope": "user:content:manage"}), \
                patch.object(upload, "request_json") as request:
            with self.assertRaisesRegex(RuntimeError, "user:content:view authorization"):
                upload.run_selected(args, [album])
            request.assert_not_called()

    def test_batch_selection_processes_only_selected_cards_and_rejects_stale_titles(self):
        args = argparse.Namespace(existing_only=False, verify_only=False, album=["One", "Three"], stop_file=None)
        albums = [{"title": name, "audio": [Path(f"{name}.mp3")]} for name in ("One", "Two", "Three")]
        library = [{"title": album["title"]} for album in albums]
        with patch.object(upload, "full_read_enabled", return_value=True), \
                patch.object(upload, "request_json", return_value={"cards": library}), \
                patch.object(upload, "run_album") as run:
            upload.run_selected(args, albums)
            self.assertEqual([call.args[0]["title"] for call in run.call_args_list], ["One", "Three"])
            args.album = ["One", "Not in scan"]
            with self.assertRaisesRegex(RuntimeError, "must match a discovered title"):
                upload.run_selected(args, albums)
            args.album = ["One", "One"]
            with self.assertRaisesRegex(RuntimeError, "only once"):
                upload.run_selected(args, albums)

    def test_existing_only_selected_batch_skips_cards_not_on_yoto(self):
        args = argparse.Namespace(existing_only=True, verify_only=False,
                                  album=["Existing", "Not Yet Created"], stop_file=None)
        albums = [{"title": name, "audio": [Path(f"{name}.mp3")]}
                  for name in ("Existing", "Not Yet Created", "Unselected")]
        library = [{"title": "Existing"}, {"title": "Unselected"}]
        with patch.object(upload, "full_read_enabled", return_value=True), \
                patch.object(upload, "request_json", return_value={"cards": library}), \
                patch.object(upload, "run_album") as run:
            upload.run_selected(args, albums)
        run.assert_called_once_with(albums[0], library, replace_placeholder=False)

    def test_create_only_skips_existing_cards_without_modifying_them(self):
        args = argparse.Namespace(existing_only=False, verify_only=False, create_only=True, album=None, stop_file=None)
        albums = [{"title": name, "audio": [Path(f"{name}.mp3")]} for name in ("Verified", "Unowned")]
        library = [{"title": album["title"], "cardId": f"id-{album['title']}"} for album in albums]
        self.state["albums"]["Verified"] = {"status": "verified", "cardId": "id-Verified"}
        output = io.StringIO()
        with patch.object(upload, "full_read_enabled", return_value=True), \
                patch.object(upload, "request_json", return_value={"cards": library}) as api, \
                patch.object(upload, "run_album") as run, patch("sys.stdout", output):
            upload.run_selected(args, albums)
        run.assert_not_called()
        api.assert_called_once_with("/content/mine")
        for album in albums:
            self.assertIn(f"SKIPPED {album['title']}: already_existing", output.getvalue())

    def test_create_only_reconciles_interrupted_creation_without_uploading_or_posting(self):
        album, payload, card, icons = self.album_and_payload()
        original = copy.deepcopy(self.state["albums"][album["title"]])
        library = [{"title": album["title"], "cardId": card["cardId"]}]
        args = argparse.Namespace(existing_only=False, verify_only=False, create_only=True,
                                  album=[album["title"]], stop_file=None)
        for status in ("pending_create", "created"):
            record = copy.deepcopy(original)
            record["status"] = status
            record.pop("iconIds")
            record.pop("iconsVerified")
            if status == "pending_create":
                record.pop("cardId")
            self.state["albums"][album["title"]] = record
            calls = []

            def fake_api(route, method="GET", **_):
                calls.append((route, method))
                if route == "/content/mine":
                    return {"cards": copy.deepcopy(library)}
                if route == "/content/synthetic-card":
                    return {"card": copy.deepcopy(card)}
                self.fail(f"Unexpected synthetic request: {method} {route}")

            with self.subTest(status=status), patch.object(upload, "full_read_enabled", return_value=True), \
                    patch.object(upload, "request_json", side_effect=fake_api), \
                    patch.object(upload, "upload_audio", side_effect=AssertionError("Audio must not be uploaded")), \
                    patch.object(upload, "upload_cover", side_effect=AssertionError("Cover must not be uploaded")), \
                    patch.object(upload, "upload_icons", side_effect=AssertionError("Icons must not be uploaded")):
                upload.run_selected(args, [album])
            self.assertTrue(all(method == "GET" for _, method in calls))
            saved = self.state["albums"][album["title"]]
            self.assertEqual((saved["status"], saved["cardId"], saved["iconIds"]),
                             ("verified", card["cardId"], icons))
            self.assertEqual(saved["payload"], payload)

    def test_create_only_does_not_reconcile_a_changed_or_wrong_card(self):
        album, _, card, _ = self.album_and_payload()
        original = copy.deepcopy(self.state["albums"][album["title"]])
        library = [{"title": album["title"], "cardId": card["cardId"]}]
        variants = []
        changed_audio = copy.deepcopy(card)
        changed_audio["content"]["chapters"][0]["tracks"][0]["trackUrl"] = "yoto:#other-audio"
        variants.append(changed_audio)
        changed_icon = copy.deepcopy(card)
        changed_icon["content"]["chapters"][0]["display"]["icon16x16"] = "yoto:#other-icon"
        variants.append(changed_icon)
        wrong_card = copy.deepcopy(card)
        wrong_card["cardId"] = "another-card"
        variants.append(wrong_card)
        for remote in variants:
            record = copy.deepcopy(original)
            record["status"] = "pending_create"
            record.pop("cardId")
            self.state["albums"][album["title"]] = record
            with self.subTest(remote=remote), patch.object(upload, "full_read_enabled", return_value=True), \
                    patch.object(upload, "request_json", return_value={"card": remote}) as api, \
                    patch.object(upload, "upload_icons", side_effect=AssertionError("Must not upload icons")):
                with self.assertRaises(RuntimeError):
                    upload.run_album(album, library, create_only=True, replace_placeholder=False)
                self.assertEqual(api.call_count, 1)
            self.assertEqual(self.state["albums"][album["title"]]["status"], "pending_create")

    def test_create_only_rechecks_library_before_posting(self):
        album, payload, _, icons = self.album_and_payload()
        self.state["albums"].clear()
        calls = []

        def fake_api(route, method="GET", **_):
            calls.append((route, method))
            if route == "/content/mine":
                return {"cards": [{"title": album["title"], "cardId": "concurrent-card"}]}
            self.fail(f"Unexpected synthetic request: {method} {route}")

        with patch.object(upload, "request_json", side_effect=fake_api), \
                patch.object(upload, "upload_audio",
                             side_effect=lambda path: self.state["audio"][upload.digest_file(path)]), \
                patch.object(upload, "upload_cover", return_value=payload["metadata"]["cover"]["imageL"]), \
                patch.object(upload, "upload_icons", return_value=icons):
            with self.assertRaisesRegex(RuntimeError, "still exists"):
                upload.run_album(album, [], create_only=True, replace_placeholder=False)
        self.assertEqual(calls, [("/content/mine", "GET")])
        self.assertNotIn(album["title"], self.state["albums"])

    def test_update_existing_only_selects_matching_cards(self):
        args = argparse.Namespace(existing_only=False, update_existing=True, verify_only=False,
                                  album=["Existing", "New"], stop_file=None)
        albums = [{"title": name, "audio": [Path(f"{name}.mp3")]} for name in ("Existing", "New")]
        library = [{"title": "Existing", "cardId": "existing-id"}]
        output = io.StringIO()
        with patch.object(upload, "full_read_enabled", return_value=True), \
                patch.object(upload, "request_json", return_value={"cards": library}), \
                patch.object(upload, "run_album") as run, patch("sys.stdout", output):
            upload.run_selected(args, albums)
        run.assert_called_once_with(albums[0], library)
        self.assertIn("SKIPPED New: not_existing", output.getvalue())

    def test_update_existing_reports_when_no_selected_cards_exist(self):
        args = argparse.Namespace(existing_only=False, update_existing=True, verify_only=False,
                                  album=["One", "Two"], stop_file=None)
        albums = [{"title": name, "audio": [Path(f"{name}.mp3")]} for name in ("One", "Two")]
        output = io.StringIO()
        with patch.object(upload, "full_read_enabled", return_value=True), \
                patch.object(upload, "request_json", return_value={"cards": []}), \
                patch.object(upload, "run_album") as run, patch("sys.stdout", output):
            upload.run_selected(args, albums)
        run.assert_not_called()
        self.assertIn("SKIPPED One: not_existing", output.getvalue())
        self.assertIn("SKIPPED Two: not_existing", output.getvalue())

    def test_update_existing_does_not_create_a_card_removed_during_the_batch(self):
        args = argparse.Namespace(existing_only=False, update_existing=True, verify_only=False,
                                  album=["One", "Two"], stop_file=None)
        albums = [{"title": name, "audio": [Path(f"{name}.mp3")]} for name in ("One", "Two")]
        first = [{"title": "One", "cardId": "id-one"}, {"title": "Two", "cardId": "id-two"}]
        later = [{"title": "One", "cardId": "id-one"}]
        output = io.StringIO()
        with patch.object(upload, "full_read_enabled", return_value=True), \
                patch.object(upload, "request_json", side_effect=[{"cards": first}, {"cards": later}]), \
                patch.object(upload, "run_album") as run, patch("sys.stdout", output):
            upload.run_selected(args, albums)
        run.assert_called_once_with(albums[0], first)
        self.assertIn("SKIPPED Two: not_existing", output.getvalue())

    def test_batch_modes_do_not_assume_absence_from_an_invalid_card_list(self):
        album = {"title": "Synthetic", "audio": [Path("one.mp3")]}
        for flag in ("create_only", "update_existing", "inspect_only"):
            args = argparse.Namespace(existing_only=False, verify_only=False, album=None, stop_file=None,
                                      **{flag: True})
            for library in (None, [{"title": "Other"}], [{"cardId": "id"}]):
                with self.subTest(flag=flag, library=library), \
                        patch.object(upload, "full_read_enabled", return_value=True), \
                        patch.object(upload, "request_json", return_value={"cards": library}), \
                        patch.object(upload, "run_album") as run:
                    with self.assertRaisesRegex(RuntimeError, "no result was assumed"):
                        upload.run_selected(args, [album])
                    run.assert_not_called()

    def test_read_only_inspection_distinguishes_evidence_and_song_names(self):
        album, _, card, _ = self.album_and_payload()
        library = [{"title": album["title"], "cardId": card["cardId"]}]
        original_state = copy.deepcopy(self.state)
        with patch.object(upload, "request_json", return_value={"card": copy.deepcopy(card)}) as api:
            result = upload.inspect_album(album, library)
        api.assert_called_once_with("/content/synthetic-card")
        self.assertEqual((result["presence"], result["cover"], result["remoteTracks"]), ("present", "matched", 2))
        self.assertEqual((result["remoteIcons"], result["remoteCovers"]), (2, 1))
        self.assertEqual(result["localFingerprint"], upload.album_fingerprint(album))
        self.assertEqual(result["remoteFingerprint"], upload.card_fingerprint(card))
        self.assertTrue(all(track["audio"] == "matched" and track["icon"] == "matched"
                            for track in result["tracks"]))
        self.assertEqual(self.state, original_state)

        renamed = copy.deepcopy(card)
        renamed["content"]["chapters"][0]["tracks"][0]["title"] = "Renamed on Yoto"
        with patch.object(upload, "request_json", return_value={"card": renamed}):
            result = upload.inspect_album(album, library)
            self.assertEqual(result["tracks"][0]["audio"], "matched")
            self.assertEqual(result["tracks"][0]["icon"], "song_mismatch")
            self.state["audio"].clear()
            result = upload.inspect_album(album, library)
            self.assertEqual(result["tracks"][0]["audio"], "name_mismatch")
            self.assertEqual(result["tracks"][1]["audio"], "name_only")

    def test_read_only_inspection_reports_missing_duplicate_and_detail_errors(self):
        album, _, card, _ = self.album_and_payload()
        entry = {"title": album["title"], "cardId": card["cardId"]}
        with patch.object(upload, "request_json") as api:
            self.assertEqual(upload.inspect_album(album, [])["presence"], "missing")
            self.assertEqual(upload.inspect_album(album, [entry, entry])["presence"], "duplicate")
            api.assert_not_called()
        with patch.object(upload, "request_json", side_effect=upload.YotoHTTPError(403, "Synthetic detail error")):
            result = upload.inspect_album(album, [entry])
        self.assertEqual(result["presence"], "present")
        self.assertEqual(result["cover"], "unverified")
        self.assertIn("Synthetic detail error", result["error"])
        self.assertEqual(result["tracks"], [])

        incomplete = copy.deepcopy(card)
        incomplete["content"]["chapters"].pop()
        with patch.object(upload, "request_json", return_value={"card": incomplete}):
            result = upload.inspect_album(album, [entry])
        self.assertEqual((result["tracks"][1]["audio"], result["tracks"][1]["icon"]), ("missing", "missing"))
        extra = copy.deepcopy(card)
        extra["content"]["chapters"].append(copy.deepcopy(extra["content"]["chapters"][0]))
        with patch.object(upload, "request_json", return_value={"card": extra}):
            result = upload.inspect_album(album, [entry])
        self.assertEqual((result["tracks"][2]["audio"], result["tracks"][2]["icon"]), ("extra", "extra"))

    def test_inspection_counts_inherited_and_url_icons_once_per_track(self):
        album, _, card, _ = self.album_and_payload()
        chapter = card["content"]["chapters"][0]
        chapter["tracks"].append(copy.deepcopy(chapter["tracks"][0]))
        chapter["display"] = {"iconUrl16x16": "https://example.invalid/icon.png"}
        for track in chapter["tracks"]:
            track.pop("display", None)
        card["content"]["chapters"][1].pop("display", None)
        card["content"]["chapters"][1]["tracks"][0].pop("display", None)
        card["metadata"]["cover"] = {}
        library = [{"title": album["title"], "cardId": card["cardId"]}]
        with patch.object(upload, "request_json", return_value={"card": card}):
            result = upload.inspect_album(album, library)
        self.assertEqual((result["remoteTracks"], result["remoteIcons"], result["remoteCovers"]), (3, 2, 0))

    def test_confirmed_overwrite_preserves_id_metadata_and_checks_again_before_post(self):
        album, payload, card, icons = self.album_and_payload()
        card["metadata"]["description"] = "Retain this description"
        card["content"]["config"]["autoadvance"] = "repeat"
        original = copy.deepcopy(card)
        library = [{"title": album["title"], "cardId": card["cardId"]}]
        remote = {"card": copy.deepcopy(card)}
        calls = []

        def fake_api(route, method="GET", body=None, **_):
            calls.append((route, method))
            if route == "/content/mine":
                return {"cards": copy.deepcopy(library)}
            if route == "/content/synthetic-card":
                return {"card": copy.deepcopy(remote["card"])}
            if route == "/content" and method == "POST":
                remote["card"] = {**copy.deepcopy(body), "updatedAt": "updated"}
                return {"card": copy.deepcopy(remote["card"])}
            self.fail(f"Unexpected synthetic request: {method} {route}")

        review = {"action": "replace", "localFingerprint": upload.album_fingerprint(album),
                  "remoteFingerprint": upload.card_fingerprint(card)}
        with patch.object(upload, "request_json", side_effect=fake_api), \
                patch.object(upload, "upload_audio",
                             side_effect=lambda path: self.state["audio"][upload.digest_file(path)]), \
                patch.object(upload, "upload_cover", return_value=payload["metadata"]["cover"]["imageL"]), \
                patch.object(upload, "upload_icons", return_value=icons):
            upload.overwrite_album(album, library, review)
        self.assertEqual(calls.count(("/content", "POST")), 1)
        self.assertEqual(calls[:4], [("/content/mine", "GET"), ("/content/synthetic-card", "GET"),
                                    ("/content/mine", "GET"), ("/content/synthetic-card", "GET")])
        self.assertEqual(remote["card"]["cardId"], card["cardId"])
        self.assertEqual(remote["card"]["metadata"]["description"], "Retain this description")
        self.assertEqual(remote["card"]["content"]["config"]["autoadvance"], "repeat")
        self.assertEqual(self.state["albums"][album["title"]]["status"], "verified")
        snapshots = list((self.work / "before-overwrite").glob("*.json"))
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(json.loads(snapshots[0].read_text()), original)
        self.assertEqual(snapshots[0].stat().st_mode & 0o777, 0o600)

    def test_overwrite_rejects_changed_remote_or_local_source_without_posting(self):
        album, payload, card, icons = self.album_and_payload()
        library = [{"title": album["title"], "cardId": card["cardId"]}]
        review = {"action": "replace", "localFingerprint": upload.album_fingerprint(album),
                  "remoteFingerprint": upload.card_fingerprint(card)}
        changed = copy.deepcopy(card)
        changed["metadata"]["description"] = "Changed while uploading"
        reads = 0

        def fake_api(route, method="GET", **_):
            nonlocal reads
            if route == "/content/mine":
                return {"cards": library}
            if route == "/content/synthetic-card":
                reads += 1
                return {"card": copy.deepcopy(card if reads == 1 else changed)}
            self.fail(f"No write is permitted: {method} {route}")

        with patch.object(upload, "request_json", side_effect=fake_api), \
                patch.object(upload, "upload_audio",
                             side_effect=lambda path: self.state["audio"][upload.digest_file(path)]), \
                patch.object(upload, "upload_cover", return_value=payload["metadata"]["cover"]["imageL"]), \
                patch.object(upload, "upload_icons", return_value=icons):
            with self.assertRaisesRegex(RuntimeError, "changed since review"):
                upload.overwrite_album(album, library, review)
        self.assertEqual(self.state["albums"][album["title"]]["status"], "verified")
        album["audio"][0].write_bytes(b"changed local audio")
        with patch.object(upload, "request_json", side_effect=lambda route, **_: (
                {"cards": library} if route == "/content/mine" else {"card": card})):
            with self.assertRaisesRegex(RuntimeError, "Local files changed"):
                upload.overwrite_album(album, library, review)

    def test_ambiguous_overwrite_response_is_reconciled_without_a_second_post(self):
        album, payload, card, icons = self.album_and_payload()
        library = [{"title": album["title"], "cardId": card["cardId"]}]
        remote = {"card": copy.deepcopy(card)}
        posts = []

        def fake_api(route, method="GET", body=None, **_):
            if route == "/content/mine":
                return {"cards": library}
            if route == "/content/synthetic-card":
                return {"card": copy.deepcopy(remote["card"])}
            if route == "/content" and method == "POST":
                posts.append(copy.deepcopy(body))
                remote["card"] = {**copy.deepcopy(body), "updatedAt": "updated"}
                raise RuntimeError("Synthetic timeout after server applied update")
            self.fail(f"Unexpected request: {method} {route}")

        review = {"action": "replace", "localFingerprint": upload.album_fingerprint(album),
                  "remoteFingerprint": upload.card_fingerprint(card)}
        with patch.object(upload, "request_json", side_effect=fake_api), \
                patch.object(upload, "upload_audio",
                             side_effect=lambda path: self.state["audio"][upload.digest_file(path)]), \
                patch.object(upload, "upload_cover", return_value=payload["metadata"]["cover"]["imageL"]), \
                patch.object(upload, "upload_icons", return_value=icons):
            with self.assertRaisesRegex(RuntimeError, "Synthetic timeout"):
                upload.overwrite_album(album, library, review)
        self.assertEqual(self.state["albums"][album["title"]]["status"], "pending_overwrite")
        review["remoteFingerprint"] = upload.card_fingerprint(remote["card"])
        with patch.object(upload, "request_json", side_effect=fake_api), \
                patch.object(upload, "upload_audio", side_effect=AssertionError("Must not re-upload")), \
                patch.object(upload, "upload_icons", side_effect=AssertionError("Must not re-upload")):
            upload.overwrite_album(album, library, review)
        self.assertEqual(len(posts), 1)
        self.assertEqual(self.state["albums"][album["title"]]["status"], "verified")

    def test_unconfirmed_replacement_never_reposts_even_if_previous_card_remains(self):
        album, payload, card, icons = self.album_and_payload()
        card["content"]["chapters"].pop()
        library = [{"title": album["title"], "cardId": card["cardId"]}]
        remote = {"card": copy.deepcopy(card)}
        posts = []

        def fake_api(route, method="GET", body=None, **_):
            if route == "/content/mine":
                return {"cards": library}
            if route == "/content/synthetic-card":
                return {"card": copy.deepcopy(remote["card"])}
            if route == "/content" and method == "POST":
                posts.append(copy.deepcopy(body))
                raise RuntimeError("Synthetic timeout before an observable response")
            self.fail(f"Unexpected request: {method} {route}")

        review = {"action": "replace", "localFingerprint": upload.album_fingerprint(album),
                  "remoteFingerprint": upload.card_fingerprint(card)}
        with patch.object(upload, "request_json", side_effect=fake_api), \
                patch.object(upload, "upload_audio",
                             side_effect=lambda path: self.state["audio"][upload.digest_file(path)]), \
                patch.object(upload, "upload_cover", return_value=payload["metadata"]["cover"]["imageL"]), \
                patch.object(upload, "upload_icons", return_value=icons):
            with self.assertRaisesRegex(RuntimeError, "Synthetic timeout"):
                upload.overwrite_album(album, library, review)
            saved = copy.deepcopy(self.state["albums"][album["title"]])
            self.assertEqual(saved["status"], "pending_overwrite")
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "OVERWRITE_UNCONFIRMED"):
                    upload.overwrite_album(album, library, review)
            self.assertEqual(len(posts), 1)
            self.assertEqual(self.state["albums"][album["title"]], saved)

            remote["card"]["metadata"]["description"] = "Changed independently"
            changed_review = {**review, "remoteFingerprint": upload.card_fingerprint(remote["card"])}
            with self.assertRaisesRegex(RuntimeError, "OVERWRITE_CHANGED:.*changed during an unfinished overwrite"):
                upload.overwrite_album(album, library, changed_review)
            self.assertEqual(len(posts), 1)
            self.assertEqual(self.state["albums"][album["title"]], saved)

    def test_legacy_update_paths_cannot_modify_an_unfinished_replacement(self):
        album, _, _, _ = self.album_and_payload()
        self.state["albums"][album["title"]] = {"status": "pending_overwrite", "cardId": "synthetic-card"}
        library = [{"title": album["title"], "cardId": "synthetic-card"}]
        for options in ({"replace_placeholder": False}, {"replace_placeholder": True}, {"create_only": True}):
            with self.subTest(options=options), patch.object(upload, "request_json") as request:
                with self.assertRaisesRegex(RuntimeError, "unfinished overwrite"):
                    upload.run_album(album, library, **options)
                request.assert_not_called()
                self.assertEqual(self.state["albums"][album["title"]]["status"], "pending_overwrite")

    def test_read_only_inspection_rejects_malformed_library_instead_of_claiming_missing(self):
        album, _, _, _ = self.album_and_payload()
        args = argparse.Namespace(existing_only=False, verify_only=False, inspect_only=True,
                                  album=[album["title"]], stop_file=None)
        for library in (None, {}, [None], [{}], [{"title": "Other"}], [{"cardId": "other"}],
                        [{"title": "", "cardId": "other"}], [{"title": "Other", "cardId": ""}]):
            with self.subTest(library=library), patch.object(upload, "full_read_enabled", return_value=True), \
                    patch.object(upload, "request_json", return_value={"cards": library}) as api:
                with self.assertRaisesRegex(RuntimeError, "no result was assumed"):
                    upload.run_selected(args, [album])
                api.assert_called_once_with("/content/mine")

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
            if route == "/content/mine":
                return {"cards": [copy.deepcopy(remote)]}
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

    def test_removed_or_ambiguous_card_is_not_updated_even_if_its_id_is_readable(self):
        album, _, card, icons = self.album_and_payload()
        card["content"] = upload.without_icons(card["content"])
        for library in ([], [{"title": card["title"], "cardId": "different-id"}],
                        [card, {**card, "cardId": "another-id"}], [{"title": card["title"]}]):
            calls = []

            def fake_api(route, method="GET", **_):
                calls.append((route, method))
                if route == "/content/mine":
                    return {"cards": library}
                if route == "/content/synthetic-card":
                    return {"card": copy.deepcopy(card)}
                self.fail(f"Unexpected synthetic request: {method} {route}")

            with self.subTest(library=library), patch.object(upload, "request_json", side_effect=fake_api):
                with self.assertRaisesRegex(RuntimeError, "no update sent"):
                    upload.fill_existing_icons(card, album, icons)
                self.assertTrue(all(method == "GET" for _, method in calls))

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
        output = io.StringIO()
        with patch.object(upload, "request_json") as api, patch("sys.stdout", output):
            with self.assertRaisesRegex(RuntimeError, "Unreconciled prior create"):
                upload.run_album(album, [])
            api.assert_not_called()
        event = json.loads(output.getvalue().split("RECOVERY_REQUIRED ", 1)[1])
        self.assertEqual(event, {"type": "missing_prior_card", "title": album["title"],
                                 "priorStatus": "pending_create"})

    def test_reviewed_deleted_verified_card_is_created_again_without_recovery_prompt(self):
        album, payload, old_card, icons = self.album_and_payload()
        previous = copy.deepcopy(self.state["albums"][album["title"]])
        created = {**copy.deepcopy(payload), "cardId": "new-synthetic-card", "updatedAt": "v2"}
        posts = []
        calls = []

        def fake_api(route, method="GET", body=None, **_):
            calls.append((route, method))
            if route == "/content/mine":
                return {"cards": [created] if posts else []}
            if route == "/content" and method == "POST":
                posts.append(copy.deepcopy(body))
                return {"card": created}
            if route == "/content/new-synthetic-card":
                return {"card": created}
            if route == "/content/synthetic-card":
                return {"card": old_card}
            self.fail(f"Unexpected synthetic request: {method} {route}")

        review = [{"title": album["title"], "action": "create",
                   "localFingerprint": upload.album_fingerprint(album)}]
        args = argparse.Namespace(existing_only=False, verify_only=False, create_only=True,
                                  album=[album["title"]], stop_file=None, review_plan=json.dumps(review))
        output = io.StringIO()
        with patch.object(upload, "request_json", side_effect=fake_api), \
                patch.object(upload, "upload_audio",
                             side_effect=lambda path: self.state["audio"][upload.digest_file(path)]), \
                patch.object(upload, "upload_cover", return_value=payload["metadata"]["cover"]["imageL"]), \
                patch.object(upload, "upload_icons", return_value=icons), \
                patch.object(upload, "full_read_enabled", return_value=True), patch("sys.stdout", output):
            upload.run_selected(args, [album])
            upload.run_selected(args, [album])
        saved = self.state["albums"][album["title"]]
        self.assertEqual((saved["status"], saved["cardId"]), ("verified", "new-synthetic-card"))
        self.assertEqual(saved["previousRecords"], [previous])
        self.assertEqual(len(posts), 1)
        self.assertNotIn("RECOVERY_REQUIRED", output.getvalue())
        self.assertNotIn(("/content/synthetic-card", "GET"), calls)
        self.assertGreaterEqual(calls.count(("/content/mine", "GET")), 3)

    def test_automatic_recreation_rechecks_library_and_preserves_history_on_failure(self):
        album, payload, _, icons = self.album_and_payload()
        previous = copy.deepcopy(self.state["albums"][album["title"]])
        review = {"title": album["title"], "action": "create",
                  "localFingerprint": upload.album_fingerprint(album)}
        for result in ({"cards": [{"title": album["title"], "cardId": "concurrent"}]},
                       {"cards": [{"title": "Renamed", "cardId": previous["cardId"]}]},
                       {"cards": [{}]},
                       upload.YotoHTTPError(403, "Synthetic library unavailable")):
            calls = []

            def fake_api(route, method="GET", **_):
                calls.append((route, method))
                if route == "/content/mine":
                    if isinstance(result, Exception):
                        raise result
                    return result
                self.fail(f"Unexpected synthetic request: {method} {route}")

            with self.subTest(result=result), patch.object(upload, "request_json", side_effect=fake_api), \
                    patch.object(upload, "upload_audio",
                                 side_effect=lambda path: self.state["audio"][upload.digest_file(path)]), \
                    patch.object(upload, "upload_cover", return_value=payload["metadata"]["cover"]["imageL"]), \
                    patch.object(upload, "upload_icons", return_value=icons):
                with self.assertRaises(RuntimeError):
                    upload.run_album(album, [], create_only=True, review=review)
            self.assertEqual(self.state["albums"][album["title"]], previous)
            self.assertNotIn(("/content", "POST"), calls)

    def test_automatic_recreation_requires_finished_verified_creation_with_known_id(self):
        album, _, _, _ = self.album_and_payload()
        original = copy.deepcopy(self.state["albums"][album["title"]])
        review = {"title": album["title"], "action": "create",
                  "localFingerprint": upload.album_fingerprint(album)}
        for changes in ({"status": "pending_create"}, {"status": "created"},
                        {"pendingCoverUpdate": {"previous": "uncertain"}}):
            self.state["albums"][album["title"]] = {**copy.deepcopy(original), **changes}
            with self.subTest(changes=changes), patch.object(upload, "request_json") as api, \
                    patch.object(upload, "upload_audio") as audio, patch("sys.stdout", new=io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "Unreconciled prior create"):
                    upload.run_album(album, [], create_only=True, review=review)
                api.assert_not_called()
                audio.assert_not_called()
        self.state["albums"][album["title"]] = {**original, "cardId": None}
        with patch.object(upload, "request_json") as api, patch.object(upload, "upload_audio") as audio:
            with self.assertRaisesRegex(RuntimeError, "Invalid previous verified card ID"):
                upload.run_album(album, [], create_only=True, review=review)
            api.assert_not_called()
            audio.assert_not_called()

    def test_review_detects_saved_card_renamed_in_yoto_library(self):
        album, _, old_card, _ = self.album_and_payload()
        library = [{"title": "Renamed on Yoto", "cardId": old_card["cardId"]}]
        with patch.object(upload, "request_json") as api:
            result = upload.inspect_album(album, library)
        self.assertEqual(result["presence"], "missing")
        self.assertEqual(result["error"], "PREVIOUS_CARD_RENAMED")
        api.assert_not_called()

    def test_confirmed_deleted_card_recreates_once_when_old_id_still_resolves(self):
        album, payload, old_card, icons = self.album_and_payload()
        previous = copy.deepcopy(self.state["albums"][album["title"]])
        new_card = {**copy.deepcopy(payload), "cardId": "new-synthetic-card", "updatedAt": "v2"}
        posts = []
        calls = []

        def fake_api(route, method="GET", body=None, **_):
            calls.append((route, method))
            if route == "/content/synthetic-card":
                return {"card": old_card}
            if route == "/content/mine":
                return {"cards": [new_card] if posts else []}
            if route == "/content" and method == "POST":
                posts.append(copy.deepcopy(body))
                return {"card": new_card}
            if route == "/content/new-synthetic-card":
                return {"card": new_card}
            self.fail(f"Unexpected synthetic request: {method} {route}")

        def cached_audio(path):
            return self.state["audio"][upload.digest_file(path)]

        with patch.object(upload, "request_json", side_effect=fake_api), \
                patch.object(upload, "upload_audio", side_effect=cached_audio), \
                patch.object(upload, "upload_cover", return_value=payload["metadata"]["cover"]["imageL"]), \
                patch.object(upload, "upload_icons", return_value=icons), \
                patch.object(upload, "full_read_enabled", return_value=True):
            args = argparse.Namespace(existing_only=False, verify_only=False, album=[album["title"]],
                                      recreate_missing=True, stop_file=None)
            upload.run_selected(args, [album])
            saved = self.state["albums"][album["title"]]
            self.assertEqual(saved["status"], "verified")
            self.assertEqual(saved["cardId"], "new-synthetic-card")
            self.assertEqual(saved["previousRecords"], [previous])
            self.assertEqual(len(posts), 1)
            with self.assertRaisesRegex(RuntimeError, "still exists"):
                upload.run_selected(args, [album])
            self.assertEqual(len(posts), 1, "A repeated confirmation must not create a duplicate")
            with self.assertRaisesRegex(RuntimeError, "Unreconciled prior create"):
                upload.run_album(album, [])
            self.assertEqual(len(posts), 1)
        self.assertNotIn(("/content/synthetic-card", "GET"), calls)

    def test_confirmed_pending_without_card_id_rechecks_list_before_creating(self):
        album, payload, _, icons = self.album_and_payload()
        self.state["albums"][album["title"]].update(status="pending_create")
        self.state["albums"][album["title"]].pop("cardId")
        previous = copy.deepcopy(self.state["albums"][album["title"]])
        created = {**copy.deepcopy(payload), "cardId": "new-synthetic-card", "updatedAt": "v2"}
        calls = []

        def fake_api(route, method="GET", **_):
            calls.append((route, method))
            if route == "/content/mine":
                return {"cards": []}
            if route == "/content" and method == "POST":
                return {"card": created}
            self.fail(f"Unexpected synthetic request: {method} {route}")

        with patch.object(upload, "request_json", side_effect=fake_api), \
                patch.object(upload, "upload_audio",
                             side_effect=lambda path: self.state["audio"][upload.digest_file(path)]), \
                patch.object(upload, "upload_cover", return_value=payload["metadata"]["cover"]["imageL"]), \
                patch.object(upload, "upload_icons", return_value=icons), \
                patch.object(upload, "full_read_enabled", return_value=False):
            upload.run_album(album, [], recreate_missing=True)
        self.assertEqual(calls, [("/content/mine", "GET"), ("/content", "POST")])
        self.assertEqual(self.state["albums"][album["title"]]["previousRecords"], [previous])

    def test_recreation_cli_requires_exactly_one_album(self):
        for titles in ([], ["One", "Two"]):
            arguments = ["yoto_upload.py", "--root", str(self.root), "--recreate-missing"]
            for title in titles:
                arguments.extend(["--album", title])
            with self.subTest(titles=titles), patch.object(sys, "argv", arguments), \
                    patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit) as error:
                upload.main()
            self.assertEqual(error.exception.code, 2)

    def test_recreation_stops_if_title_or_old_id_exists_in_library(self):
        album, _, card, _ = self.album_and_payload()
        for library in ([{"title": album["title"], "cardId": "another"}],
                        [{"title": "Renamed", "cardId": card["cardId"]}]):
            with self.subTest(library=library), patch.object(upload, "request_json") as api:
                with self.assertRaisesRegex(RuntimeError, "still exists"):
                    upload.run_album(album, library, recreate_missing=True)
                api.assert_not_called()

    def test_recreation_rejects_an_invalid_card_list(self):
        album, _, _, _ = self.album_and_payload()
        recorded = self.state["albums"][album["title"]]
        for library in (None, {}, [None], [{}], [{"title": "Other"}], [{"cardId": "other"}],
                        [{"title": "", "cardId": "other"}], [{"title": "Other", "cardId": ""}]):
            with self.subTest(library=library), patch.object(upload, "request_json") as api:
                with self.assertRaisesRegex(RuntimeError, "Cannot check Yoto's card list"):
                    upload.check_recreation_absent(album["title"], recorded, library)
                api.assert_not_called()

    def test_recreation_rechecks_before_post_and_keeps_old_evidence_on_failure(self):
        album, payload, _, icons = self.album_and_payload()
        previous = copy.deepcopy(self.state["albums"][album["title"]])
        for result in ({"cards": [{"title": album["title"], "cardId": "concurrent"}]},
                       {"cards": [{"title": "Renamed", "cardId": "synthetic-card"}]},
                       {"cards": [{}]},
                       upload.YotoHTTPError(403, "Synthetic library unavailable")):
            calls = []

            def fake_api(route, method="GET", **_):
                calls.append((route, method))
                if route == "/content/mine":
                    if isinstance(result, Exception):
                        raise result
                    return result
                self.fail(f"Unexpected synthetic request: {method} {route}")

            with self.subTest(result=result), patch.object(upload, "request_json", side_effect=fake_api), \
                    patch.object(upload, "upload_audio",
                                 side_effect=lambda path: self.state["audio"][upload.digest_file(path)]), \
                    patch.object(upload, "upload_cover", return_value=payload["metadata"]["cover"]["imageL"]), \
                    patch.object(upload, "upload_icons", return_value=icons):
                with self.assertRaises(RuntimeError):
                    upload.run_album(album, [], recreate_missing=True)
                self.assertEqual(self.state["albums"][album["title"]], previous)
                self.assertNotIn(("/content", "POST"), calls)

    def test_interrupted_recreation_keeps_history_and_blocks_automatic_retry(self):
        album, payload, _, icons = self.album_and_payload()
        previous = copy.deepcopy(self.state["albums"][album["title"]])
        posts = []

        def fake_api(route, method="GET", **_):
            if route == "/content/mine":
                return {"cards": []}
            if route == "/content" and method == "POST":
                posts.append(route)
                raise RuntimeError("Synthetic interrupted response")
            self.fail(f"Unexpected synthetic request: {method} {route}")

        with patch.object(upload, "request_json", side_effect=fake_api), \
                patch.object(upload, "upload_audio",
                             side_effect=lambda path: self.state["audio"][upload.digest_file(path)]), \
                patch.object(upload, "upload_cover", return_value=payload["metadata"]["cover"]["imageL"]), \
                patch.object(upload, "upload_icons", return_value=icons):
            with self.assertRaisesRegex(RuntimeError, "interrupted response"):
                upload.run_album(album, [], recreate_missing=True)
            pending = self.state["albums"][album["title"]]
            self.assertEqual(pending["status"], "pending_create")
            self.assertEqual(pending["previousRecords"], [previous])
            with self.assertRaisesRegex(RuntimeError, "Unreconciled prior create"):
                upload.run_album(album, [])
            self.assertEqual(len(posts), 1)

    def test_process_lock_rejects_another_writer(self):
        with upload.work_lock():
            with self.assertRaisesRegex(RuntimeError, "Another uploader"):
                with upload.work_lock():
                    self.fail("A second writer acquired the work directory")


class AuthorizationTests(unittest.TestCase):
    @staticmethod
    def permission_response(content_type="application/json; charset=utf-8", status=200):
        response = io.BytesIO(b"{}")
        response.status = status
        response.headers = {"Content-Type": content_type}
        return response

    def test_pkce_query_mode_and_explicit_card_and_icon_scopes(self):
        verifier = "synthetic-pkce-verifier"
        params = auth.authorization_parameters("SYNTHETIC_CLIENT", "http://127.0.0.1:8788/callback",
                                               verifier, "synthetic-state")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(params["code_challenge"], challenge)
        self.assertEqual(params["code_challenge_method"], "S256")
        self.assertEqual(params["response_mode"], "query")
        self.assertEqual(set(params["scope"].split()),
                         {"openid", "user:content:view", "user:content:manage", "user:icons:manage"})

    def test_scope_omission_and_invalid_authorization_responses(self):
        required = {"openid", "user:content:view", "user:content:manage"}
        base = {"access_token": "synthetic-token", "expires_in": 3600}
        token = auth.validate_authorization_response(dict(base), required, requested_scope=auth.DEFAULT_SCOPES)
        self.assertEqual(token["scope"], auth.DEFAULT_SCOPES)
        requested = auth.DEFAULT_SCOPES
        token = auth.validate_authorization_response(
            dict(base), required | {"user:icons:manage"}, requested_scope=requested
        )
        self.assertEqual(token["scope"], requested)
        cases = [
            ([], "ACCOUNT_AUTH_RESPONSE_INVALID"),
            ({**base, "scope": None}, "ACCOUNT_AUTH_RESPONSE_INVALID"),
            ({**base, "scope": []}, "ACCOUNT_AUTH_RESPONSE_INVALID"),
            ({**base, "scope": ""}, "ACCOUNT_AUTH_SCOPES_INSUFFICIENT"),
            ({**base, "scope": "openid user:content:view"}, "ACCOUNT_AUTH_SCOPES_INSUFFICIENT"),
            ({**base, "access_token": " ", "scope": auth.DEFAULT_SCOPES}, "ACCOUNT_AUTH_TOKEN_INVALID"),
            ({**base, "expires_in": "3600", "scope": auth.DEFAULT_SCOPES}, "ACCOUNT_AUTH_EXPIRY_INVALID"),
            ({**base, "expires_in": True, "scope": auth.DEFAULT_SCOPES}, "ACCOUNT_AUTH_EXPIRY_INVALID"),
            ({**base, "expires_in": float("inf"), "scope": auth.DEFAULT_SCOPES}, "ACCOUNT_AUTH_EXPIRY_INVALID"),
            ({**base, "expires_in": 60, "scope": auth.DEFAULT_SCOPES}, "ACCOUNT_AUTH_EXPIRY_INVALID"),
        ]
        for response, code in cases:
            with self.subTest(code=code, response=repr(response)[:80]):
                with self.assertRaises(auth.AuthorizationResponseError) as error:
                    auth.validate_authorization_response(response, required, requested_scope=auth.DEFAULT_SCOPES)
                self.assertEqual(error.exception.code, code)
                self.assertNotIn("synthetic-token", str(error.exception))
        with self.assertRaises(auth.AuthorizationResponseError) as error:
            auth.validate_authorization_response(dict(base), required)
        self.assertEqual(error.exception.code, "ACCOUNT_AUTH_RESPONSE_INVALID")

    def test_implicit_child_scopes_require_successful_read_only_checks(self):
        token = {"access_token": "synthetic-token", "expires_in": 3600, "scope": "user:content:manage"}
        required = {"user:content:manage", "user:content:view", "user:icons:manage"}
        with self.assertRaises(auth.AuthorizationResponseError):
            auth.validate_authorization_response(token, required)
        opener = Mock()
        opener.open.side_effect = lambda *args, **kwargs: self.permission_response()
        with patch.object(auth.urllib.request, "build_opener", return_value=opener) as build:
            auth.verify_implicit_scopes(token, required)
        self.assertEqual(token["scope"], "user:content:manage", "Preserve Yoto's original scope response")
        self.assertEqual(set(token["yotolio_verified_scopes"]), {"user:content:view", "user:icons:manage"})
        auth.validate_authorization_response(token, required)
        requests = [call.args[0] for call in opener.open.call_args_list]
        self.assertEqual({request.full_url for request in requests}, {
            upload.API + "/content/mine", upload.API + "/media/displayIcons/user/me",
        })
        for request, call in zip(requests, opener.open.call_args_list, strict=True):
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-token")
            self.assertEqual(call.kwargs["timeout"], 15)
        for call in build.call_args_list:
            self.assertIsInstance(call.args[0], accounts.NoRedirect)

    def test_permission_check_denials_and_network_failures_are_distinct(self):
        cases = [
            ("user:content:view", 401, "ACCOUNT_AUTH_CONTENT_VIEW_DENIED"),
            ("user:content:view", 403, "ACCOUNT_AUTH_CONTENT_VIEW_DENIED"),
            ("user:icons:manage", 403, "ACCOUNT_AUTH_ICON_ACCESS_DENIED"),
            ("user:content:view", 302, "ACCOUNT_AUTH_SCOPE_CHECK_UNAVAILABLE"),
            ("user:content:view", 503, "ACCOUNT_AUTH_SCOPE_CHECK_UNAVAILABLE"),
        ]
        for scope, status, code in cases:
            with self.subTest(scope=scope, status=status):
                token = {"access_token": "synthetic-token", "scope": "user:content:manage"}
                opener = Mock()
                opener.open.side_effect = urllib.error.HTTPError(
                    "https://api.yotoplay.com/synthetic", status, "synthetic", {}, io.BytesIO(b"private"))
                with patch.object(auth.urllib.request, "build_opener", return_value=opener), \
                        self.assertRaises(auth.AuthorizationResponseError) as error:
                    auth.verify_implicit_scopes(token, {"user:content:manage", scope})
                self.assertEqual(error.exception.code, code)
                self.assertNotIn("synthetic-token", str(error.exception))
                self.assertNotIn("yotolio_verified_scopes", token)
                opener.open.assert_called_once()
        for failure in (urllib.error.URLError("synthetic network failure"), TimeoutError("synthetic timeout")):
            with self.subTest(failure=type(failure).__name__):
                opener = Mock()
                opener.open.side_effect = failure
                with patch.object(auth.urllib.request, "build_opener", return_value=opener), \
                        self.assertRaises(auth.AuthorizationResponseError) as error:
                    auth.verify_implicit_scopes(
                        {"access_token": "synthetic-token", "scope": "user:content:manage"},
                        {"user:content:manage", "user:content:view"},
                    )
                self.assertEqual(error.exception.code, "ACCOUNT_AUTH_SCOPE_CHECK_UNAVAILABLE")
        for content_type, status in (("text/html", 200), ("application/json", 204)):
            with self.subTest(content_type=content_type, status=status):
                opener = Mock()
                opener.open.return_value = self.permission_response(content_type, status)
                with patch.object(auth.urllib.request, "build_opener", return_value=opener), \
                        self.assertRaises(auth.AuthorizationResponseError) as error:
                    auth.verify_implicit_scopes(
                        {"access_token": "synthetic-token", "scope": "user:content:manage"},
                        {"user:content:manage", "user:content:view"},
                    )
                self.assertEqual(error.exception.code, "ACCOUNT_AUTH_SCOPE_CHECK_UNAVAILABLE")

    def test_invalid_saved_permission_checks_are_not_accepted(self):
        base = {"access_token": "synthetic-token", "expires_in": 3600}
        cases = [
            {**base, "scope": "user:content:manage", "yotolio_verified_scopes": "user:content:view"},
            {**base, "scope": "user:content:manage", "yotolio_verified_scopes": ["openid"]},
            {**base, "scope": "user:content:view", "yotolio_verified_scopes": ["user:icons:manage"]},
        ]
        for token in cases:
            with self.subTest(token=token), self.assertRaises(auth.AuthorizationResponseError) as error:
                auth.validate_authorization_response(token, {"user:content:view"})
            self.assertEqual(error.exception.code, "ACCOUNT_AUTH_RESPONSE_INVALID")

    def test_callback_accepts_omitted_scope_after_identifying_account(self):
        local_urlopen = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
        token = {"access_token": "synthetic-token", "expires_in": 3600, "id_token": "synthetic-id-token"}
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            with socket.socket() as reserve:
                reserve.bind(("127.0.0.1", 0))
                port = reserve.getsockname()[1]
            args = ["yoto_authorize.py", "--client-id", "SYNTHETIC_CLIENT", "--work-dir", directory,
                    "--port", str(port), "--timeout", "3", "--identify-account"]
            outcomes = []

            def urlopen(request, *args, **kwargs):
                if isinstance(request, urllib.request.Request) and request.full_url == "https://login.yotoplay.com/oauth/token":
                    return io.BytesIO(json.dumps(token).encode())
                return local_urlopen(request, *args, **kwargs)

            def authorize():
                try:
                    auth.main()
                    outcomes.append(None)
                except (Exception, SystemExit) as error:
                    outcomes.append(error)

            with patch.object(sys, "argv", args), \
                    patch.object(auth.secrets, "token_urlsafe",
                                 side_effect=["synthetic-verifier", "synthetic-state", "synthetic-nonce"]), \
                    patch.object(auth, "configure_macos_trust"), \
                    patch.object(auth, "identify_account", return_value="a" * 64) as identify, \
                    patch.object(auth.urllib.request, "urlopen", side_effect=urlopen):
                worker = threading.Thread(target=authorize, daemon=True)
                worker.start()
                callback = f"http://127.0.0.1:{port}/callback?code=synthetic-code&state=synthetic-state"
                deadline = time.monotonic() + 2
                while True:
                    try:
                        with local_urlopen(callback, timeout=1) as response:
                            self.assertEqual(response.status, 200)
                            message = response.read().decode("utf-8")
                            self.assertIn("Return to Yotolio to finish connecting", message)
                            self.assertIn("可能需要确认以前的上传进度", message)
                        break
                    except urllib.error.URLError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.02)
                worker.join(timeout=6)
                self.assertFalse(worker.is_alive())
                identify.assert_called_once_with(
                    "synthetic-token", client_id="SYNTHETIC_CLIENT",
                    id_token="synthetic-id-token", nonce="synthetic-nonce",
                )
            self.assertEqual(outcomes, [None])
            saved = json.loads((Path(directory) / "authorization.json").read_text())
            self.assertEqual(saved["scope"], auth.DEFAULT_SCOPES)
            self.assertEqual(saved["yotolio_account_key"], "a" * 64)
            self.assertNotIn("id_token", saved)

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

    def test_account_identity_uses_verified_userinfo_and_does_not_follow_redirects(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(b'{"sub":"synthetic-subject"}')
        with patch.object(accounts, "configure_macos_trust"), \
                patch.object(accounts.urllib.request, "build_opener", return_value=opener) as build:
            key = accounts.identify_account("synthetic-access-token")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, accounts.USERINFO)
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-access-token")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 15)
        self.assertEqual(key, hashlib.sha256((accounts.ISSUER + "\0synthetic-subject").encode()).hexdigest())
        self.assertNotIn("synthetic-subject", key)
        redirect_handler = build.call_args.args[0]
        self.assertIsInstance(redirect_handler, accounts.NoRedirect)
        self.assertIsNone(redirect_handler.redirect_request(None, None, 302, "", {}, "https://example.invalid"))

    def test_invalid_userinfo_is_an_explicit_failure_without_exposing_credentials(self):
        responses = (b"not json", b"[]", b'{"sub":""}', b'{"sub":42}', b"x" * 65_537)
        for response in responses:
            opener = Mock()
            opener.open.return_value = io.BytesIO(response)
            with self.subTest(response=response[:20]), patch.object(accounts, "configure_macos_trust"), \
                    patch.object(accounts.urllib.request, "build_opener", return_value=opener), \
                    self.assertRaises(accounts.AccountIdentityError) as error:
                accounts.identify_account("synthetic-access-token")
            self.assertEqual(error.exception.code, "ACCOUNT_IDENTITY_UNAVAILABLE")
            self.assertNotIn("synthetic-access-token", str(error.exception))

    def test_failed_identity_or_missing_manage_does_not_replace_authorization(self):
        local_urlopen = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
        token = {
            "access_token": "synthetic-not-a-real-token", "expires_in": 3600,
            "scope": "openid user:content:view user:content:manage",
        }
        for missing_manage in (False, True):
            with self.subTest(missing_manage=missing_manage), \
                    tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
                token["scope"] = ("openid user:content:view" if missing_manage
                                  else "openid user:content:view user:content:manage")
                path = Path(directory) / "authorization.json"
                auth.save_token(path, {"access_token": "synthetic-existing-token"})
                previous = path.read_bytes()
                with socket.socket() as reserve:
                    reserve.bind(("127.0.0.1", 0))
                    port = reserve.getsockname()[1]
                args = ["yoto_authorize.py", "--client-id", "SYNTHETIC_CLIENT", "--work-dir", directory,
                        "--port", str(port), "--timeout", "3", "--identify-account", "--replace"]
                if missing_manage:
                    args.append("--icons-scope")
                outcomes = []

                def urlopen(request, *args, **kwargs):
                    if isinstance(request, urllib.request.Request) and request.full_url == "https://login.yotoplay.com/oauth/token":
                        return io.BytesIO(json.dumps(token).encode())
                    return local_urlopen(request, *args, **kwargs)

                def authorize():
                    try:
                        auth.main()
                        outcomes.append(None)
                    except (Exception, SystemExit) as error:
                        outcomes.append(error)

                with patch.object(sys, "argv", args), \
                        patch.object(auth.secrets, "token_urlsafe",
                                     side_effect=["synthetic-verifier", "synthetic-state", "synthetic-nonce"]), \
                        patch.object(auth, "configure_macos_trust"), \
                        patch.object(auth, "identify_account", side_effect=accounts.AccountIdentityError()) as identify, \
                        patch.object(auth.urllib.request, "urlopen", side_effect=urlopen):
                    worker = threading.Thread(target=authorize, daemon=True)
                    worker.start()
                    callback = f"http://127.0.0.1:{port}/callback?code=synthetic-code&state=synthetic-state"
                    deadline = time.monotonic() + 2
                    while True:
                        try:
                            with local_urlopen(callback, timeout=1) as response:
                                response_code = response.status
                            break
                        except urllib.error.HTTPError as error:
                            response_code = error.code
                            response_type = error.headers["Content-Type"]
                            cache_control = error.headers["Cache-Control"]
                            browser_message = error.read().decode("utf-8")
                            error.close()
                            break
                        except urllib.error.URLError:
                            if time.monotonic() >= deadline:
                                raise
                            time.sleep(0.02)
                    worker.join(timeout=6)
                    self.assertFalse(worker.is_alive())
                self.assertEqual(response_code, 502)
                self.assertEqual(response_type, "text/html; charset=utf-8")
                self.assertEqual(cache_control, "no-store")
                self.assertIn("Return to the Yotolio app", browser_message)
                self.assertIn("请返回 Yotolio 应用", browser_message)
                self.assertNotIn("synthetic-not-a-real-token", browser_message)
                self.assertNotIn("SYNTHETIC_CLIENT", browser_message)
                expected = "ACCOUNT_AUTH_SCOPES_INSUFFICIENT" if missing_manage else "ACCOUNT_IDENTITY_UNAVAILABLE"
                self.assertEqual(str(outcomes[0]), expected)
                self.assertEqual(path.read_bytes(), previous)
                if missing_manage:
                    identify.assert_not_called()
                else:
                    identify.assert_called_once_with(
                        token["access_token"], client_id="SYNTHETIC_CLIENT",
                        id_token=None, nonce="synthetic-nonce",
                    )

    def test_parent_only_callback_verifies_permissions_without_replacing_on_failure(self):
        local_urlopen = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
        for outcome in ("success", "denied", "forged", "invalid_scope"):
            with self.subTest(outcome=outcome), \
                    tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
                path = Path(directory) / "authorization.json"
                auth.save_token(path, {"access_token": "synthetic-existing-token"})
                previous = path.read_bytes()
                token = {
                    "access_token": "synthetic-token", "expires_in": 3600,
                    "scope": "user:content:manage",
                }
                if outcome == "forged":
                    token["yotolio_verified_scopes"] = ["user:content:view", "user:icons:manage"]
                opener = Mock()
                if outcome == "denied":
                    opener.open.side_effect = [
                        self.permission_response(),
                        urllib.error.HTTPError("https://api.yotoplay.com/synthetic", 403, "denied", {},
                                               io.BytesIO(b"private")),
                    ]
                else:
                    opener.open.side_effect = lambda *args, **kwargs: self.permission_response()
                with socket.socket() as reserve:
                    reserve.bind(("127.0.0.1", 0))
                    port = reserve.getsockname()[1]
                args = ["yoto_authorize.py", "--client-id", "SYNTHETIC_CLIENT", "--work-dir", directory,
                        "--port", str(port), "--timeout", "3", "--identify-account", "--replace", "--icons-scope"]
                results = []

                def urlopen(request, *positional, **kwargs):
                    if isinstance(request, urllib.request.Request) and request.full_url == "https://login.yotoplay.com/oauth/token":
                        return io.BytesIO(json.dumps(token).encode())
                    return local_urlopen(request, *positional, **kwargs)

                def authorize():
                    try:
                        auth.main()
                        results.append(None)
                    except (Exception, SystemExit) as error:
                        results.append(error)

                with patch.object(sys, "argv", args), \
                        patch.object(auth.secrets, "token_urlsafe",
                                     side_effect=["synthetic-verifier", "synthetic-state", "synthetic-nonce"]), \
                        patch.object(auth, "configure_macos_trust"), \
                        patch.object(auth, "identify_account", return_value="a" * 64) as identify, \
                        patch.object(auth.urllib.request, "urlopen", side_effect=urlopen), \
                        patch.object(auth.urllib.request, "build_opener", return_value=opener):
                    worker = threading.Thread(target=authorize, daemon=True)
                    worker.start()
                    query = "error=invalid_scope" if outcome == "invalid_scope" else "code=synthetic-code"
                    callback = f"http://127.0.0.1:{port}/callback?{query}&state=synthetic-state"
                    deadline = time.monotonic() + 2
                    while True:
                        try:
                            with local_urlopen(callback, timeout=1) as response:
                                response_code = response.status
                            break
                        except urllib.error.HTTPError as error:
                            response_code = error.code
                            error.close()
                            break
                        except urllib.error.URLError:
                            if time.monotonic() >= deadline:
                                raise
                            time.sleep(0.02)
                    worker.join(timeout=6)
                    self.assertFalse(worker.is_alive())
                if outcome == "success":
                    self.assertEqual((response_code, results), (200, [None]))
                    saved = json.loads(path.read_text())
                    self.assertEqual(saved["scope"], "user:content:manage")
                    self.assertEqual(set(saved["yotolio_verified_scopes"]),
                                     {"user:content:view", "user:icons:manage"})
                    self.assertEqual(saved["yotolio_account_key"], "a" * 64)
                    identify.assert_called_once_with(
                        "synthetic-token", client_id="SYNTHETIC_CLIENT",
                        id_token=None, nonce="synthetic-nonce",
                    )
                else:
                    expected = {
                        "denied": "ACCOUNT_AUTH_ICON_ACCESS_DENIED",
                        "forged": "ACCOUNT_AUTH_RESPONSE_INVALID",
                        "invalid_scope": "ACCOUNT_AUTH_SCOPES_INSUFFICIENT",
                    }[outcome]
                    self.assertEqual(response_code, 400 if outcome == "invalid_scope" else 502)
                    self.assertEqual(str(results[0]), expected)
                    self.assertEqual(path.read_bytes(), previous)
                    identify.assert_not_called()
                    if outcome in {"forged", "invalid_scope"}:
                        opener.open.assert_not_called()

    def test_callback_reports_certificate_errors_and_retains_successful_login(self):
        local_urlopen = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
        token = {
            "access_token": "synthetic-not-a-real-token", "expires_in": 3600,
            "scope": "openid user:content:view user:content:manage",
        }
        for fail_certificate in (True, False):
            with self.subTest(fail_certificate=fail_certificate), \
                    tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
                with socket.socket() as reserve:
                    reserve.bind(("127.0.0.1", 0))
                    port = reserve.getsockname()[1]
                args = ["yoto_authorize.py", "--client-id", "SYNTHETIC_CLIENT", "--work-dir", directory,
                        "--port", str(port), "--timeout", "3"]
                outcomes = []

                def urlopen(request, *args, **kwargs):
                    if isinstance(request, urllib.request.Request) and request.full_url == "https://login.yotoplay.com/oauth/token":
                        if fail_certificate:
                            raise urllib.error.URLError(ssl.SSLCertVerificationError("synthetic"))
                        return io.BytesIO(json.dumps(token).encode())
                    return local_urlopen(request, *args, **kwargs)

                def authorize():
                    try:
                        auth.main()
                        outcomes.append(None)
                    except (Exception, SystemExit) as error:
                        outcomes.append(error)

                with patch.object(sys, "argv", args), \
                        patch.object(auth.secrets, "token_urlsafe",
                                     side_effect=["synthetic-verifier", "synthetic-state", "synthetic-nonce"]), \
                        patch.object(auth, "configure_macos_trust") as configure, \
                        patch.object(auth.urllib.request, "urlopen", side_effect=urlopen):
                    worker = threading.Thread(target=authorize, daemon=True)
                    worker.start()
                    callback = f"http://127.0.0.1:{port}/callback?code=synthetic-code&state=synthetic-state"
                    deadline = time.monotonic() + 2
                    while True:
                        try:
                            with local_urlopen(callback, timeout=1) as response:
                                response_code = response.status
                            break
                        except urllib.error.HTTPError as error:
                            response_code = error.code
                            error.close()
                            break
                        except urllib.error.URLError:
                            if time.monotonic() >= deadline:
                                raise
                            time.sleep(0.02)
                    worker.join(timeout=6)
                    self.assertFalse(worker.is_alive())
                    configure.assert_called_once()
                if fail_certificate:
                    self.assertEqual(response_code, 502)
                    self.assertIsInstance(outcomes[0], SystemExit)
                    self.assertIn("secure connection could not be verified", str(outcomes[0]))
                    self.assertFalse((Path(directory) / "authorization.json").exists())
                else:
                    self.assertEqual(response_code, 200)
                    self.assertEqual(outcomes, [None])
                    self.assertEqual(json.loads((Path(directory) / "authorization.json").read_text())["access_token"],
                                     token["access_token"])


class IdentityTokenTests(unittest.TestCase):
    client = "SYNTHETIC_CLIENT"
    nonce = "synthetic-nonce"
    subject = "auth0|synthetic-subject"

    @classmethod
    def setUpClass(cls):
        cls.openssl = shutil.which("openssl")
        if not cls.openssl:
            raise unittest.SkipTest("OpenSSL is needed to generate synthetic signed tokens")
        cls.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.private_key = Path(cls.temporary.name) / "synthetic-key.pem"
        subprocess.run(
            [cls.openssl, "genrsa", "-out", str(cls.private_key), "2048"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        result = subprocess.run(
            [cls.openssl, "rsa", "-in", str(cls.private_key), "-modulus", "-noout"],
            check=True, capture_output=True, text=True,
        )
        modulus = int(result.stdout.strip().removeprefix("Modulus="), 16)
        cls.jwk = {
            "kty": "RSA", "kid": "synthetic-key", "alg": "RS256", "use": "sig",
            "n": cls.b64(modulus.to_bytes((modulus.bit_length() + 7) // 8, "big")),
            "e": cls.b64((65537).to_bytes(3, "big")),
        }

    @staticmethod
    def b64(data):
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    def claims(self, **changes):
        now = int(time.time())
        claims = {
            "iss": accounts.ISSUER, "sub": self.subject, "aud": self.client,
            "nonce": self.nonce, "iat": now - 1, "exp": now + 3600,
        }
        claims.update(changes)
        return claims

    def sign(self, claims, header=None):
        header = header or {"alg": "RS256", "kid": "synthetic-key"}
        message = ".".join(self.b64(json.dumps(value, separators=(",", ":")).encode())
                           for value in (header, claims)).encode("ascii")
        signature = subprocess.run(
            [self.openssl, "dgst", "-sha256", "-sign", str(self.private_key)],
            input=message, check=True, capture_output=True,
        ).stdout
        return message.decode("ascii") + "." + self.b64(signature)

    @staticmethod
    def corrupt_signature(token):
        header, claims, signature = token.split(".")
        signature = ("B" if signature[0] == "A" else "A") + signature[1:]
        return ".".join((header, claims, signature))

    def opener(self, *, status=401, keys=None):
        opener = Mock()

        def open_request(request, timeout):
            self.assertEqual(timeout, 15)
            if request.full_url == accounts.USERINFO:
                raise urllib.error.HTTPError(request.full_url, status, "synthetic", {}, io.BytesIO(b"private"))
            self.assertEqual(request.full_url, accounts.JWKS)
            return io.BytesIO(json.dumps({"keys": [self.jwk] if keys is None else keys}).encode())

        opener.open.side_effect = open_request
        return opener

    def identify(self, access_token, *, identity_token=None, opener=None):
        log = io.StringIO()
        opener = opener or self.opener()
        with patch.object(accounts, "configure_macos_trust"), \
                patch.object(accounts.urllib.request, "build_opener", return_value=opener), \
                redirect_stdout(log):
            result = accounts.identify_account(
                access_token, client_id=self.client, id_token=identity_token, nonce=self.nonce,
            )
        return result, log.getvalue()

    def test_signed_identity_and_api_tokens_match_userinfo_account(self):
        expected = hashlib.sha256((accounts.ISSUER + "\0" + self.subject).encode()).hexdigest()
        access = "synthetic-access-token"
        digest = hashlib.sha256(access.encode("ascii")).digest()[:16]
        identity = self.sign(self.claims(at_hash=self.b64(digest)))
        key, log = self.identify(access, identity_token=identity)
        self.assertEqual(key, expected)
        self.assertEqual(log.strip(), "IDENTITY_DIAGNOSTIC userinfo_http=401")
        for secret in (access, identity, self.subject, self.client):
            self.assertNotIn(secret, log)

        api_token = self.sign(self.claims(aud=[accounts.API_AUDIENCE, accounts.USERINFO], azp=self.client))
        key, _ = self.identify(api_token)
        self.assertEqual(key, expected)

        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps({"sub": self.subject}).encode())
        key, _ = self.identify(access, identity_token=identity, opener=opener)
        self.assertEqual(key, expected)
        opener.open.assert_called_once()

    def test_invalid_identity_tokens_never_select_an_account(self):
        valid = self.sign(self.claims())
        cases = {
            "signature": self.corrupt_signature(valid),
            "issuer": self.sign(self.claims(iss="https://example.invalid/")),
            "audience": self.sign(self.claims(aud="OTHER_CLIENT")),
            "multiple audiences": self.sign(self.claims(aud=[self.client, "OTHER_CLIENT"])),
            "authorized party": self.sign(self.claims(azp="OTHER_CLIENT")),
            "nonce": self.sign(self.claims(nonce="other-nonce")),
            "expired": self.sign(self.claims(exp=int(time.time()) - 10)),
            "future issued": self.sign(self.claims(iat=int(time.time()) + 600)),
            "not yet valid": self.sign(self.claims(nbf=int(time.time()) + 600)),
            "access token hash": self.sign(self.claims(at_hash="wrong")),
            "algorithm": self.sign(self.claims(), {"alg": "HS256", "kid": "synthetic-key"}),
            "critical header": self.sign(self.claims(), {"alg": "RS256", "kid": "synthetic-key", "crit": ["x"]}),
        }
        for name, token in cases.items():
            with self.subTest(name=name), self.assertRaises(accounts.AccountIdentityError) as error:
                self.identify("synthetic-access-token", identity_token=token)
            self.assertEqual(error.exception.code, "ACCOUNT_IDENTITY_TOKEN_INVALID")
            for secret in (token, self.subject, self.client):
                self.assertNotIn(secret, str(error.exception))

    def test_invalid_api_tokens_never_select_an_account(self):
        base = self.claims(aud=accounts.API_AUDIENCE, azp=self.client)
        cases = {
            "wrong audience": {**base, "aud": accounts.USERINFO},
            "wrong client": {**base, "azp": "OTHER_CLIENT"},
            "conflicting client claims": {**base, "client_id": "OTHER_CLIENT"},
            "missing client": {key: value for key, value in base.items() if key != "azp"},
            "machine identity": {**base, "sub": "synthetic@clients"},
            "missing subject": {key: value for key, value in base.items() if key != "sub"},
        }
        for name, claims in cases.items():
            with self.subTest(name=name), self.assertRaises(accounts.AccountIdentityError) as error:
                self.identify(self.sign(claims))
            self.assertEqual(error.exception.code, "ACCOUNT_IDENTITY_TOKEN_INVALID")
        with self.assertRaises(accounts.AccountIdentityError) as error:
            self.identify(self.corrupt_signature(self.sign(base)))
        self.assertEqual(error.exception.code, "ACCOUNT_IDENTITY_TOKEN_INVALID")

    def test_missing_keys_and_verifier_fail_safely(self):
        token = self.sign(self.claims())
        for keys in ([], [self.jwk, self.jwk]):
            with self.subTest(keys=len(keys)), self.assertRaises(accounts.AccountIdentityError) as error:
                self.identify("synthetic-access-token", identity_token=token, opener=self.opener(keys=keys))
            self.assertEqual(error.exception.code, "ACCOUNT_IDENTITY_KEYS_UNAVAILABLE")
        with patch.object(accounts.subprocess, "run", side_effect=OSError("synthetic failure")), \
                self.assertRaises(accounts.AccountIdentityError) as error:
            self.identify("synthetic-access-token", identity_token=token)
        self.assertEqual(error.exception.code, "ACCOUNT_IDENTITY_VERIFIER_UNAVAILABLE")

    def test_userinfo_failures_are_distinct_when_no_signed_token_is_available(self):
        cases = [
            (urllib.error.HTTPError(accounts.USERINFO, 401, "synthetic", {}, io.BytesIO(b"private")),
             "ACCOUNT_IDENTITY_REJECTED"),
            (urllib.error.HTTPError(accounts.USERINFO, 503, "synthetic", {}, io.BytesIO(b"private")),
             "ACCOUNT_IDENTITY_SERVICE_UNAVAILABLE"),
            (urllib.error.URLError("synthetic network failure"), "ACCOUNT_IDENTITY_SERVICE_UNAVAILABLE"),
        ]
        for failure, expected in cases:
            opener = Mock()
            opener.open.side_effect = failure
            with self.subTest(expected=expected), self.assertRaises(accounts.AccountIdentityError) as error:
                self.identify("synthetic-opaque-token", opener=opener)
            self.assertEqual(error.exception.code, expected)

    def test_callback_uses_signed_fallback_and_preserves_existing_progress(self):
        local_urlopen = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
        identity = self.sign(self.claims())
        for valid in (True, False):
            with self.subTest(valid=valid), tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
                path = Path(directory) / "authorization.json"
                auth.save_token(path, {"access_token": "synthetic-existing-token"})
                previous = path.read_bytes()
                progress = Path(directory) / "upload-state.json"
                progress.write_bytes(b'{"albums":{"Synthetic Card":{"status":"verified"}}}')
                previous_progress = progress.read_bytes()
                token = {
                    "access_token": "synthetic-access-token", "expires_in": 3600,
                    "scope": auth.DEFAULT_SCOPES, "id_token": identity if valid else self.corrupt_signature(identity),
                }
                with socket.socket() as reserve:
                    reserve.bind(("127.0.0.1", 0))
                    port = reserve.getsockname()[1]
                args = ["yoto_authorize.py", "--client-id", self.client, "--work-dir", directory,
                        "--port", str(port), "--timeout", "5", "--identify-account", "--replace"]
                outcomes = []
                log = io.StringIO()

                def urlopen(request, *positional, **kwargs):
                    if isinstance(request, urllib.request.Request) and request.full_url == "https://login.yotoplay.com/oauth/token":
                        return io.BytesIO(json.dumps(token).encode())
                    return local_urlopen(request, *positional, **kwargs)

                def authorize():
                    try:
                        auth.main()
                        outcomes.append(None)
                    except (Exception, SystemExit) as error:
                        outcomes.append(error)

                with patch.object(sys, "argv", args), \
                        patch.object(auth.secrets, "token_urlsafe",
                                     side_effect=["synthetic-verifier", "synthetic-state", self.nonce]), \
                        patch.object(auth, "configure_macos_trust"), \
                        patch.object(accounts, "configure_macos_trust"), \
                        patch.object(auth.urllib.request, "urlopen", side_effect=urlopen), \
                        patch.object(accounts.urllib.request, "build_opener", return_value=self.opener()), \
                        redirect_stdout(log):
                    worker = threading.Thread(target=authorize, daemon=True)
                    worker.start()
                    callback = f"http://127.0.0.1:{port}/callback?code=synthetic-code&state=synthetic-state"
                    deadline = time.monotonic() + 3
                    while True:
                        try:
                            with local_urlopen(callback, timeout=5) as response:
                                response_code = response.status
                            break
                        except urllib.error.HTTPError as error:
                            response_code = error.code
                            error.close()
                            break
                        except urllib.error.URLError:
                            if time.monotonic() >= deadline:
                                raise
                            time.sleep(0.02)
                    worker.join(timeout=8)
                    self.assertFalse(worker.is_alive())
                self.assertEqual(progress.read_bytes(), previous_progress)
                self.assertNotIn(identity, log.getvalue())
                self.assertNotIn(self.subject, log.getvalue())
                if valid:
                    self.assertEqual((response_code, outcomes), (200, [None]))
                    saved = json.loads(path.read_text())
                    self.assertEqual(saved["yotolio_account_key"],
                                     hashlib.sha256((accounts.ISSUER + "\0" + self.subject).encode()).hexdigest())
                    self.assertNotIn("id_token", saved)
                else:
                    self.assertEqual(response_code, 502)
                    self.assertEqual(str(outcomes[0]), "ACCOUNT_IDENTITY_TOKEN_INVALID")
                    self.assertEqual(path.read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
