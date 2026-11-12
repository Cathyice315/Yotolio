import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import copy
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from yoto_discovery import AUDIO_EXTENSIONS, IMAGE_EXTENSIONS, cover_data, discover, natural_key
from yoto_tls import CERTIFICATE_ERROR, certificate_error, configure_macos_trust


ROOT = None
WORK = None
STATE_FILE = None
API = "https://api.yotoplay.com"
LOCK = threading.RLock()
STATE = {"audio": {}, "covers": {}, "albums": {}, "icons": {}}


class YotoHTTPError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


@contextmanager
def work_lock(work=None, filename="upload.lock"):
    work = WORK if work is None else Path(work)
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(work / filename, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError):
            raise RuntimeError("Another uploader is using this work directory") from None
        yield
    finally:
        os.close(descriptor)


def save_state():
    with LOCK:
        temporary = STATE_FILE.with_suffix(".new")
        descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(STATE, stream, ensure_ascii=False, indent=2)
        temporary.replace(STATE_FILE)


def token_data():
    path = WORK / "authorization.json"
    if not path.is_file():
        raise RuntimeError("Authorization missing; run yoto_authorize.py with the same --work-dir")
    token = json.loads(path.read_text(encoding="utf-8"))
    if token["obtained_at"] + token["expires_in"] < time.time() + 60:
        raise RuntimeError("Yoto access token expires within 60 seconds; new authorization required")
    return token


def authorized_scopes(token):
    scope = token.get("scope", "")
    verified = token.get("yotolio_verified_scopes", [])
    if not isinstance(scope, str) or not isinstance(verified, list):
        raise ValueError("Invalid saved authorization scopes")
    verifiable = {"user:content:view", "user:icons:manage"}
    if any(not isinstance(item, str) or item not in verifiable for item in verified):
        raise ValueError("Invalid saved permission verification")
    scopes = set(scope.split())
    if verified and "user:content:manage" not in scopes:
        raise ValueError("Permission verification requires content management")
    return scopes.union(verified)


def request_json(route, method="GET", body=None, content_type=None, query=None):
    url = API + route
    if query:
        url += "?" + urllib.parse.urlencode(query)
    if isinstance(body, dict):
        body = json.dumps(body).encode()
        content_type = "application/json"
    attempts = 5 if method == "GET" else 1
    for attempt in range(attempts):
        headers = {
            "Authorization": "Bearer " + token_data()["access_token"],
            "Accept": "application/json",
        }
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
                delay = min(60, 2 ** (attempt + 1))
                print(f"RETRY {method} {route}: HTTP {error.code}, waiting {delay}s", flush=True)
                time.sleep(delay)
                continue
            raise YotoHTTPError(
                error.code,
                f"{method} {route}: HTTP {error.code}; check authorization, scopes, and API availability"
            ) from None
        except (urllib.error.URLError, TimeoutError, ssl.SSLCertVerificationError) as error:
            if certificate_error(error):
                raise RuntimeError(CERTIFICATE_ERROR) from None
            if attempt + 1 < attempts:
                print(f"RETRY {method} {route}: {type(error).__name__}", flush=True)
                time.sleep(min(60, 2 ** (attempt + 1)))
                continue
            raise RuntimeError(f"{method} {route}: {type(error).__name__}") from None


def digest_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def value_fingerprint(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def album_fingerprint(album):
    if len(album["audio"]) != len(album["icons"]):
        raise RuntimeError(f"Local audio/icon mapping is incomplete: {album['title']}")
    return value_fingerprint({
        "title": album["title"],
        "tracks": [
            {"name": audio.name, "audio": digest_file(audio),
             "icon": digest_file(icon) if icon is not None else None}
            for audio, icon in zip(album["audio"], album["icons"], strict=True)
        ],
        "cover": hashlib.sha256(cover_data(album)[0]).hexdigest(),
        "hasCover": album["cover"] is not None,
    })


def card_fingerprint(card):
    return value_fingerprint({key: card.get(key) for key in ("cardId", "title", "updatedAt", "metadata", "content")})


def album_folders():
    prepared = ROOT / "Covers"
    prepared_names = {p.stem for p in prepared.iterdir() if p.is_file()} if prepared.is_dir() else set()

    def visit(folder):
        children = sorted(folder.iterdir(), key=natural_key)
        files = [p for p in children if p.is_file()]
        direct_audio = any(p.suffix.lower() in AUDIO_EXTENSIONS for p in files)
        marked_cover = any("cover" in p.stem.casefold() and p.suffix.lower() in IMAGE_EXTENSIONS
                           for p in files)
        if direct_audio or (
            (folder.name in prepared_names or marked_cover)
            and any(p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS for p in folder.rglob("*"))
        ):
            return [folder]
        found = []
        for child in children:
            if child.is_dir() and child != prepared and child != WORK and not child.name.startswith("."):
                found.extend(visit(child))
        return found

    folders = []
    for folder in sorted(ROOT.iterdir(), key=natural_key):
        if folder.is_dir() and folder != prepared and folder != WORK and not folder.name.startswith("."):
            folders.extend(visit(folder))
    if not folders:
        raise RuntimeError("No album folders with audio were found under --root")
    if len({p.name for p in folders}) != len(folders):
        raise RuntimeError("Album folder names must be distinct for cover extraction")
    return folders


def prepare_covers():
    folders = album_folders()
    copies = []
    destination = ROOT / "Covers"
    for folder in folders:
        existing = [p for p in destination.glob("*") if p.is_file() and p.stem == folder.name]
        if existing:
            if len(existing) != 1 or existing[0].suffix.lower() not in {".jpg", ".jpeg", ".png"}:
                raise RuntimeError(f"Ambiguous or unsupported prepared cover: {folder.name}")
            print(f"COVER_PRESENT {folder.name}", flush=True)
            continue
        images = [p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png"}]
        named = [p for p in images if "cover" in p.stem.casefold()]
        unnumbered = [p for p in images
                      if not re.match(r"^(?:icon\s*)?\d+(?:$|[\s._-])", p.stem, re.I)]
        candidates = named if named else unnumbered
        if len(candidates) != 1:
            raise RuntimeError(
                f"Cannot choose one cover for {folder.name}; place the chosen image in "
                "Covers/<exact album folder name>.jpg (or .png/.jpeg), then rerun"
            )
        copies.append((candidates[0], destination / (folder.name + candidates[0].suffix.lower())))
    destination.mkdir(exist_ok=True)
    for source, target in copies:
        with source.open("rb") as incoming, target.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        if digest_file(source) != digest_file(target):
            raise RuntimeError(f"Cover copy checksum mismatch: {source.name}")
        print(f"COVER_COPIED {target.name}", flush=True)
    print(f"COVERS_READY: {len(folders)} albums; originals unchanged", flush=True)


def inventory():
    return discover(ROOT, WORK)


def upload_audio(path):
    digest = digest_file(path)
    with LOCK:
        cached = copy.deepcopy(STATE["audio"].get(digest))
    if cached and cached.get("transcodedSha256"):
        return cached
    if not cached:
        upload = request_json(
            "/media/transcode/audio/uploadUrl",
            query={"sha256": digest, "filename": path.name},
        )["upload"]
        upload_id = upload["uploadId"]
        if upload.get("uploadUrl"):
            mime = {".mp3": "audio/mpeg", ".m4a": "audio/mp4"}.get(
                path.suffix.lower(), mimetypes.guess_type(path.name)[0]
            )
            if not mime:
                raise RuntimeError(f"Unknown audio MIME type: {path.name}")
            request = urllib.request.Request(
                upload["uploadUrl"], data=path.read_bytes(), method="PUT",
                headers={"Content-Type": mime},
            )
            try:
                with urllib.request.urlopen(request, timeout=600) as response:
                    response.read()
            except urllib.error.HTTPError as error:
                raise RuntimeError(f"Audio upload {path.name}: HTTP {error.code}") from None
            except (urllib.error.URLError, TimeoutError, ssl.SSLCertVerificationError) as error:
                if certificate_error(error):
                    raise RuntimeError(CERTIFICATE_ERROR) from None
                raise RuntimeError(f"Audio upload {path.name}: {type(error).__name__}") from None
        cached = {"uploadId": upload_id}
        with LOCK:
            STATE["audio"][digest] = cached
            save_state()
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        transcode = request_json(
            f"/media/upload/{urllib.parse.quote(cached['uploadId'], safe='')}/transcoded",
            query={"loudnorm": "false"},
        )["transcode"]
        if transcode.get("transcodedSha256"):
            info = transcode["transcodedInfo"]
            if info.get("duration", 0) <= 0 or info.get("fileSize", 0) <= 0:
                raise RuntimeError(f"Invalid transcoded audio metadata: {path.name}")
            if info.get("channels") not in {"mono", "stereo"}:
                raise RuntimeError(f"Unexpected transcoded channel value: {info.get('channels')}")
            if not info.get("format"):
                raise RuntimeError(f"Missing transcoded format: {path.name}")
            result = {
                "uploadId": cached["uploadId"],
                "transcodedSha256": transcode["transcodedSha256"],
                "info": info,
            }
            with LOCK:
                STATE["audio"][digest] = result
                save_state()
            return result
        progress = transcode.get("progress", {})
        if transcode.get("error") or progress.get("phase") in {"failed", "error"}:
            raise RuntimeError(f"Transcoding failed for {path.name}: {progress}")
        time.sleep(3)
    raise RuntimeError(f"Transcoding timeout after 15 minutes: {path.name}")


def upload_cover(album):
    data, filename, mime = cover_data(album)
    digest = hashlib.sha256(data).hexdigest()
    if digest in STATE["covers"]:
        return STATE["covers"][digest]
    response = request_json(
        "/media/coverImage/user/me/upload", method="POST",
        body=data, content_type=mime,
        query={"autoconvert": "true", "coverType": "default", "filename": filename},
    )["coverImage"]
    if not response.get("mediaUrl", "").startswith("https://"):
        raise RuntimeError("Cover API returned no HTTPS media URL")
    STATE["covers"][digest] = response["mediaUrl"]
    save_state()
    return response["mediaUrl"]


def icon_cache_key(data):
    native_png = (
        data.startswith(b"\x89PNG\r\n\x1a\n")
        and len(data) >= 26
        and struct.unpack(">II", data[16:24]) == (16, 16)
        and data[24:26] == b"\x08\x06"
    )
    mode = "false" if native_png else "true"
    return hashlib.sha256(data).hexdigest() + ":" + mode, mode


def upload_icon(path):
    data = path.read_bytes()
    cache_key, mode = icon_cache_key(data)
    with LOCK:
        cached = STATE["icons"].get(cache_key)
    if cached:
        return cached
    mime = mimetypes.guess_type(path.name)[0]
    if not mime or not mime.startswith("image/"):
        raise RuntimeError(f"Unknown icon image format: {path.name}")
    response = request_json(
        "/media/displayIcons/user/me/upload", method="POST",
        body=data, content_type=mime,
        query={"autoConvert": mode, "filename": path.name},
    )["displayIcon"]
    media_id = response.get("mediaId")
    if not isinstance(media_id, str) or not media_id:
        raise RuntimeError(f"Icon API returned no media ID: {path.name}")
    with LOCK:
        STATE["icons"][cache_key] = media_id
        save_state()
    return media_id


def upload_icons(album):
    result = [None] * len(album["icons"])
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(upload_icon, path): index
                   for index, path in enumerate(album["icons"]) if path is not None}
        for future in as_completed(futures):
            result[futures[future]] = future.result()
    print(f"ICONS_UPLOADED {album['title']}: {sum(icon is not None for icon in result)} of "
          f"{len(result)} song icons; missing source={sum(icon is None for icon in result)}", flush=True)
    return result


def build_payload(album, audio, cover_url, icons):
    chapters = []
    for index, (path, media, icon) in enumerate(zip(album["audio"], audio, icons, strict=True), 1):
        info = media["info"]
        key = f"{index:02d}"
        track = {
            "key": "01", "title": path.name, "overlayLabel": str(index),
            "trackUrl": "yoto:#" + media["transcodedSha256"],
            "duration": info["duration"], "fileSize": info["fileSize"],
            "channels": info["channels"], "format": info["format"], "type": "audio",
        }
        chapter = {
            "key": key, "title": path.name, "overlayLabel": str(index),
            "tracks": [track],
        }
        if icon is not None:
            track["display"] = {"icon16x16": "yoto:#" + icon}
            chapter["display"] = {"icon16x16": "yoto:#" + icon}
        chapters.append(chapter)
    return {
        "title": album["title"],
        "metadata": {
            "cover": {"imageL": cover_url},
            "media": {
                "duration": sum(m["info"]["duration"] for m in audio),
                "fileSize": sum(m["info"]["fileSize"] for m in audio),
            },
        },
        "content": {
            "activity": "yoto_Player", "version": "1", "playbackType": "linear",
            "config": {
                "resumeTimeout": 2592000,
                "onlineOnly": False,
                "shuffle": [{"start": 0, "end": len(chapters) - 1, "limit": len(chapters)}],
            },
            "chapters": chapters,
        },
    }


def check_card(card, album, expected=None):
    if card.get("title") != album["title"]:
        raise RuntimeError(f"Title verification failed: {album['title']}")
    content = card.get("content", {})
    chapters = content.get("chapters")
    if not isinstance(chapters, list):
        raise RuntimeError(f"No chapter list for {album['title']}")
    tracks = [track for chapter in chapters for track in chapter.get("tracks", [])]
    if len(chapters) != len(album["audio"]) or len(tracks) != len(album["audio"]):
        raise RuntimeError(f"Track count verification failed: {album['title']}")
    if content.get("config", {}).get("shuffle") != [
        {"start": 0, "end": len(chapters) - 1, "limit": len(chapters)}
    ]:
        raise RuntimeError(f"Shuffle verification failed: {album['title']}")
    cover = card.get("metadata", {}).get("cover", {}).get("imageL")
    if not cover:
        raise RuntimeError(f"Cover missing: {album['title']}")
    if expected:
        expected_tracks = [chapter["tracks"][0] for chapter in expected["content"]["chapters"]]
        for track, wanted in zip(tracks, expected_tracks, strict=True):
            for field in ("trackUrl", "title", "duration", "fileSize", "format", "channels"):
                if field == "duration":
                    actual_duration = track.get(field)
                    if not isinstance(actual_duration, (int, float)):
                        raise RuntimeError(f"Missing track duration: {album['title']}")
                    # Content reads can round the transcode duration up by one second.
                    if 0 <= actual_duration - wanted[field] <= 1:
                        continue
                if track.get(field) != wanted[field]:
                    raise RuntimeError(f"Track {field} verification failed: {album['title']}")
        if cover != expected["metadata"]["cover"]["imageL"]:
            raise RuntimeError(f"Cover verification failed: {album['title']}")
    return len(tracks)


def full_read_enabled():
    return "user:content:view" in authorized_scopes(token_data())


def check_icons(card, icons):
    chapters = card["content"]["chapters"]
    if len(chapters) != len(icons):
        raise RuntimeError("Icon count does not match chapter count")
    for index, (chapter, icon) in enumerate(zip(chapters, icons, strict=True), 1):
        if icon is None:
            continue
        expected = "yoto:#" + icon
        owners = [chapter, *chapter["tracks"]]
        if any((owner.get("display") or {}).get("icon16x16") != expected for owner in owners):
            raise RuntimeError(f"Icon verification failed at song {index}: {card['title']}")


def without_icons(content):
    result = copy.deepcopy(content)
    for chapter in result["chapters"]:
        for owner in [chapter, *chapter["tracks"]]:
            display = owner.get("display")
            if isinstance(display, dict):
                display.pop("icon16x16", None)
            if not display:
                owner.pop("display", None)
    return result


def fill_existing_icons(card, album, icons, replacement_cover=None):
    content = copy.deepcopy(card["content"])
    changed = False
    for chapter, path, icon in zip(content["chapters"], album["audio"], icons, strict=True):
        if len(chapter["tracks"]) != 1 or chapter["tracks"][0]["title"] != path.name:
            raise RuntimeError(f"Existing song order/name differs from local source: {album['title']}")
        if icon is None:
            continue
        expected = "yoto:#" + icon
        for owner in [chapter, *chapter["tracks"]]:
            display = owner.get("display") or {}
            existing_icon = display.get("icon16x16")
            if existing_icon and existing_icon != expected:
                raise RuntimeError(f"Refusing to overwrite an existing different icon: {album['title']}")
            if existing_icon != expected:
                owner["display"] = {**display, "icon16x16": expected}
                changed = True
    payload = {
        "cardId": card["cardId"], "title": card["title"],
        "metadata": copy.deepcopy(card["metadata"]), "content": content,
    }
    if replacement_cover is not None:
        payload["metadata"]["cover"]["imageL"] = replacement_cover
        changed = True
    if changed:
        backup_dir = WORK / "before-icons"
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        backup_file = backup_dir / (card["cardId"] + ".json")
        if not backup_file.exists():
            descriptor = os.open(backup_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(card, stream, ensure_ascii=False, indent=2)
        current = request_json("/content/" + urllib.parse.quote(card["cardId"], safe=""))["card"]
        if any(current.get(key) != card.get(key) for key in ("updatedAt", "title", "content", "metadata")):
            raise RuntimeError(f"Card changed during icon upload; no update sent: {album['title']}")
        library = request_json("/content/mine")["cards"]
        if not valid_card_library(library):
            raise RuntimeError("Cannot read Yoto's card list; no update sent")
        matches = [item for item in library if item["title"] == card["title"]]
        if len(matches) != 1 or matches[0]["cardId"] != card["cardId"]:
            raise RuntimeError(f"Card is no longer uniquely in the Yoto library; no update sent: {album['title']}")
        request_json("/content", method="POST", body=payload)
    updated = request_json("/content/" + urllib.parse.quote(card["cardId"], safe=""))["card"]
    if without_icons(updated["content"]) != without_icons(card["content"]):
        raise RuntimeError(f"Non-icon content changed during icon update: {album['title']}")
    if updated["metadata"] != payload["metadata"] or updated["title"] != card["title"]:
        raise RuntimeError(f"Cover/metadata/title changed during icon update: {album['title']}")
    check_card(updated, album)
    check_icons(updated, icons)
    return payload


def valid_card_library(library):
    return isinstance(library, list) and all(
        isinstance(card, dict) and all(
            isinstance(card.get(field), str) and card[field].strip()
            for field in ("title", "cardId")
        ) for card in library
    )


def check_recreation_absent(title, recorded, library):
    if not valid_card_library(library):
        raise RuntimeError("Cannot check Yoto's card list; no card was created")
    old_id = recorded.get("cardId")
    if old_id is not None and (not isinstance(old_id, str) or not old_id):
        raise RuntimeError(f"Invalid previous card ID; no card was created: {title}")
    # A removed card can still resolve by ID; check membership in the library instead.
    if any(card.get("title") == title or (old_id and card.get("cardId") == old_id) for card in library):
        raise RuntimeError(f"The matching or previously saved card still exists on Yoto; no card was created: {title}")


def reconcile_created_card(album, card, recorded, expected):
    title = album["title"]
    if recorded.get("status") == "created" and not recorded.get("cardId"):
        raise RuntimeError(f"Missing card ID for unfinished creation: {title}")
    check_card(card, album, expected)
    expected_chapters = expected["content"]["chapters"]
    actual_chapters = card["content"]["chapters"]
    icons = []
    for wanted, actual in zip(expected_chapters, actual_chapters, strict=True):
        if (len(wanted["tracks"]) != 1 or len(actual["tracks"]) != 1
                or wanted.get("title") != actual.get("title")):
            raise RuntimeError(f"Unfinished creation differs from saved chapter evidence: {title}")
        wanted_icons = [(owner.get("display") or {}).get("icon16x16")
                        for owner in (wanted, wanted["tracks"][0])]
        actual_icons = [(owner.get("display") or {}).get("icon16x16")
                        for owner in (actual, actual["tracks"][0])]
        icon = wanted_icons[0]
        if (wanted_icons[0] != wanted_icons[1] or actual_icons != wanted_icons
                or (icon is not None and (not isinstance(icon, str) or not icon.startswith("yoto:#")
                                          or len(icon) <= len("yoto:#")))):
            raise RuntimeError(f"Unfinished creation differs from saved icon evidence: {title}")
        icons.append(icon.removeprefix("yoto:#") if icon is not None else None)
    recorded.update(status="verified", cardId=card["cardId"], trackCount=len(album["audio"]),
                    verification="get_content", iconIds=icons, iconsVerified=all(icons))
    save_state()
    print(f"VERIFIED {title}: card={card['cardId']}; tracks={len(album['audio'])}; "
          f"icons={sum(icon is not None for icon in icons)}; resumed without modifying the card", flush=True)


def checked_remote_card(title, card_id, fingerprint):
    library = request_json("/content/mine")["cards"]
    if not valid_card_library(library):
        raise RuntimeError("Cannot read Yoto's card list; no update sent")
    matches = [item for item in library if item["title"] == title]
    if len(matches) != 1 or matches[0]["cardId"] != card_id:
        raise RuntimeError(f"Card is no longer uniquely in the Yoto library; no update sent: {title}")
    card = request_json("/content/" + urllib.parse.quote(card_id, safe=""))["card"]
    if (not isinstance(card, dict) or card.get("cardId") != card_id or card.get("title") != title
            or card_fingerprint(card) != fingerprint):
        raise RuntimeError(f"Yoto card changed since review; check it again before updating: {title}")
    return card


def save_overwrite_snapshot(card, fingerprint):
    directory = WORK / "before-overwrite"
    directory.mkdir(mode=0o700, exist_ok=True)
    name = hashlib.sha256(card["cardId"].encode("utf-8")).hexdigest()[:16] + "-" + fingerprint + ".json"
    target = directory / name
    try:
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        saved = json.loads(target.read_text(encoding="utf-8"))
        if card_fingerprint(saved) != fingerprint:
            raise RuntimeError("The saved pre-update snapshot is invalid; no update sent") from None
        return
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(card, stream, ensure_ascii=False, indent=2)


def check_replaced_card(card, album, expected):
    check_card(card, album, expected)
    if card.get("cardId") != expected.get("cardId"):
        raise RuntimeError(f"Updated card ID differs from the selected card: {album['title']}")
    for wanted, actual in zip(expected["content"]["chapters"], card["content"]["chapters"], strict=True):
        for owner, actual_owner in zip((wanted, *wanted["tracks"]), (actual, *actual["tracks"]), strict=True):
            expected_display = owner.get("display") or {}
            actual_display = actual_owner.get("display") or {}
            if (not isinstance(actual_display, dict)
                    or actual_display.get("icon16x16") != expected_display.get("icon16x16")
                    or (expected_display.get("icon16x16") is None and actual_display.get("iconUrl16x16"))):
                raise RuntimeError(f"Updated icons differ from the selected source: {album['title']}")
    for key, value in expected["metadata"].items():
        if key not in {"cover", "media"} and card["metadata"].get(key) != value:
            raise RuntimeError(f"Updated card metadata changed unexpectedly: {album['title']}")


def overwrite_album(album, library, review):
    title = album["title"]
    matches = [card for card in library if card["title"] == title]
    if len(matches) != 1:
        raise RuntimeError(f"The reviewed card is no longer unique; no update sent: {title}")
    card_id = matches[0]["cardId"]
    card = checked_remote_card(title, card_id, review["remoteFingerprint"])
    if album_fingerprint(album) != review["localFingerprint"]:
        raise RuntimeError(f"Local files changed since review; scan and check again: {title}")
    recorded = STATE["albums"].get(title)
    if recorded is not None and not isinstance(recorded, dict):
        raise RuntimeError(f"Invalid saved album evidence: {title}")
    if recorded and recorded.get("pendingCoverUpdate"):
        raise RuntimeError(f"An earlier cover update needs reconciliation before replacing this card: {title}")
    unfinished_create = recorded and recorded.get("status") in {"pending_create", "created"}
    if (review["action"] == "resume") != bool(unfinished_create):
        raise RuntimeError(f"The previous creation state changed; check this card again: {title}")
    if unfinished_create:
        expected = recorded.get("payload")
        if not isinstance(expected, dict) or (recorded.get("cardId") and recorded["cardId"] != card_id):
            raise RuntimeError(f"Unfinished creation cannot be reconciled with this card: {title}")
        reconcile_created_card(album, card, recorded, expected)
        return

    pending = recorded and recorded.get("status") == "pending_overwrite"
    if pending:
        expected = recorded.get("payload")
        if (recorded.get("cardId") != card_id or not isinstance(expected, dict)
                or recorded.get("sourceFingerprint") != review["localFingerprint"]):
            raise RuntimeError(f"An unfinished overwrite has different source or card evidence: {title}")
        print(f"START {title}: {len(album['audio'])} tracks", flush=True)
        try:
            check_replaced_card(card, album, expected)
        except RuntimeError:
            if card_fingerprint(card) != recorded.get("baselineFingerprint"):
                raise RuntimeError(
                    f"OVERWRITE_CHANGED: The card changed during an unfinished overwrite; "
                    f"no update sent: {title}"
                ) from None
            raise RuntimeError(
                f"OVERWRITE_UNCONFIRMED: {title}. The earlier request may still take effect; "
                "no new update was sent. Check Yoto, then check again."
            ) from None
        recorded.update(status="verified", verification="get_content", trackCount=len(album["audio"]))
        save_state()
        print(f"VERIFIED {title}: card={card_id}; tracks={len(album['audio'])}; "
              "previous overwrite reconciled without another update", flush=True)
        return
    else:
        content = card.get("content")
        metadata = card.get("metadata")
        if not isinstance(content, dict) or not isinstance(metadata, dict):
            raise RuntimeError(f"Existing card has invalid content or metadata: {title}")
        if content.get("playbackType") not in (None, "linear") or content.get("activity") not in (None, "yoto_Player"):
            raise RuntimeError(f"Only ordinary linear cards can be overwritten safely: {title}")
        config = content.get("config", {})
        if not isinstance(config, dict):
            raise RuntimeError(f"Existing card has invalid playback settings: {title}")
        print(f"START {title}: {len(album['audio'])} tracks", flush=True)
        audio = [None] * len(album["audio"])
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(upload_audio, path): index for index, path in enumerate(album["audio"])}
            for completed, future in enumerate(as_completed(futures), 1):
                audio[futures[future]] = future.result()
                print(f"AUDIO {title}: {completed}/{len(audio)}", flush=True)
        cover_url = upload_cover(album)
        print(f"COVER_UPLOADED {title}", flush=True)
        icons = upload_icons(album)
        replacement = build_payload(album, audio, cover_url, icons)
        payload = {
            "cardId": card_id, "title": title,
            "metadata": {**copy.deepcopy(metadata), **replacement["metadata"]},
            "content": {**copy.deepcopy(content), **replacement["content"]},
        }
        payload["content"]["config"] = {
            **replacement["content"]["config"], **copy.deepcopy(config),
            "shuffle": replacement["content"]["config"]["shuffle"],
        }
        history = recorded.get("previousRecords", []) if recorded else []
        if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
            raise RuntimeError(f"Invalid previous card history: {title}")
        previous_records = copy.deepcopy(history)
        if recorded:
            previous = copy.deepcopy(recorded)
            previous.pop("previousRecords", None)
            previous_records.append(previous)

    if album_fingerprint(album) != review["localFingerprint"]:
        raise RuntimeError(f"Local files changed during upload; no update sent: {title}")
    current = checked_remote_card(title, card_id, review["remoteFingerprint"])
    save_overwrite_snapshot(current, review["remoteFingerprint"])
    STATE["albums"][title] = {
        "status": "pending_overwrite", "cardId": card_id, "payload": payload,
        "baselineFingerprint": review["remoteFingerprint"], "sourceFingerprint": review["localFingerprint"],
        "sourceUploaded": True, "coverPlaceholder": album["cover"] is None,
        "iconIds": icons, "iconsVerified": all(icons), "previousRecords": previous_records,
    }
    save_state()
    print(f"SAVING {title}", flush=True)
    updated = request_json("/content", method="POST", body=payload)["card"]
    check_replaced_card(updated, album, payload)
    updated = request_json("/content/" + urllib.parse.quote(card_id, safe=""))["card"]
    check_replaced_card(updated, album, payload)
    STATE["albums"][title].update(status="verified", verification="get_content", trackCount=len(album["audio"]))
    save_state()
    print(f"VERIFIED {title}: card={card_id}; tracks={len(album['audio'])}; "
          f"icons={sum(icon is not None for icon in icons)}; existing card replaced", flush=True)


def run_album(album, library, replace_placeholder=True, recreate_missing=False, create_only=False, review=None):
    title = album["title"]
    if review and review["action"] in {"replace", "resume"}:
        overwrite_album(album, library, review)
        return
    existing = [card for card in library if card.get("title") == title]
    if len(existing) > 1:
        raise RuntimeError(f"Duplicate existing title; refusing to create or alter: {title}")
    recorded = STATE["albums"].get(title)
    if recorded is not None and not isinstance(recorded, dict):
        raise RuntimeError(f"Invalid saved album evidence: {title}")
    if recorded and recorded.get("status") == "pending_overwrite":
        raise RuntimeError(f"An unfinished overwrite must be reconciled before changing this card: {title}")
    if (create_only and review and review["action"] == "create" and not existing
            and recorded and recorded.get("status") == "verified"
            and not recorded.get("pendingCoverUpdate")):
        old_id = recorded.get("cardId")
        if not isinstance(old_id, str) or not old_id.strip():
            raise RuntimeError(f"Invalid previous verified card ID; no card was created: {title}")
        recreate_missing = True
    previous_records = []
    if recreate_missing:
        if not recorded or recorded.get("status") not in {"pending_create", "created", "verified"}:
            raise RuntimeError(f"No previous creation evidence to reconcile: {title}")
        history = recorded.get("previousRecords", [])
        if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
            raise RuntimeError(f"Invalid previous card history: {title}")
        check_recreation_absent(title, recorded, library)
        previous = copy.deepcopy(recorded)
        previous.pop("previousRecords", None)
        previous_records = [*copy.deepcopy(history), previous]
    elif existing:
        card_id = existing[0]["cardId"]
        if recorded and recorded.get("cardId") and recorded["cardId"] != card_id:
            raise RuntimeError(f"Remote card ID differs from saved state: {title}")
        if not full_read_enabled():
            raise RuntimeError("Existing card icon updates require user:content:view authorization")
        card = request_json("/content/" + urllib.parse.quote(card_id, safe=""))["card"]
        if not isinstance(card, dict) or card.get("cardId") != card_id:
            raise RuntimeError(f"Yoto returned a different card; no update sent: {title}")
        expected = copy.deepcopy(recorded.get("payload")) if recorded else None
        if create_only:
            if not recorded or recorded.get("status") not in {"pending_create", "created"} or not isinstance(expected, dict):
                raise RuntimeError(f"Invalid unfinished creation evidence: {title}")
            reconcile_created_card(album, card, recorded, expected)
            return
        pending = recorded.get("pendingCoverUpdate") if recorded else None
        if recorded and (pending or (replace_placeholder and recorded.get("coverPlaceholder")
                                     and album["cover"] is not None)):
            if recorded.get("cardId") != card_id or not isinstance(expected, dict):
                raise RuntimeError(f"Missing evidence for placeholder cover update: {title}")
        cover_already_replaced = False
        if pending:
            if not isinstance(pending, dict) or not expected or not all(
                isinstance(pending.get(key), str) and pending[key]
                for key in ("previous", "replacement", "sourceDigest")
            ):
                raise RuntimeError(f"Invalid pending cover-update evidence: {title}")
            if expected["metadata"]["cover"]["imageL"] != pending["previous"]:
                raise RuntimeError(f"Pending cover-update baseline differs from saved evidence: {title}")
            current_cover = card.get("metadata", {}).get("cover", {}).get("imageL")
            if current_cover == pending["replacement"]:
                expected["metadata"]["cover"]["imageL"] = pending["replacement"]
                cover_already_replaced = True
            elif current_cover != pending["previous"]:
                raise RuntimeError(f"Cover changed during an unfinished update; no update sent: {title}")
            if album["cover"] is not None:
                current_digest = hashlib.sha256(cover_data(album)[0]).hexdigest()
                if current_digest != pending["sourceDigest"]:
                    raise RuntimeError(f"Selected cover changed during an unfinished update: {title}")
            elif not cover_already_replaced:
                raise RuntimeError(f"Reselect the cover for the unfinished update: {title}")
        check_card(card, album, expected)
        previous_icons = recorded.get("iconIds") if recorded else None
        if previous_icons is None:
            previous_icons = [None] * len(album["icons"])
        if (not isinstance(previous_icons, list) or len(previous_icons) != len(album["icons"])
                or any(icon is not None and (not isinstance(icon, str) or not icon) for icon in previous_icons)):
            raise RuntimeError(f"Invalid saved icon evidence: {title}")
        check_icons(card, [saved if source is None else None
                           for source, saved in zip(album["icons"], previous_icons, strict=True)])
        icons = upload_icons(album)
        replacement_cover = None
        if (replace_placeholder and recorded and recorded.get("coverPlaceholder")
                and album["cover"] is not None and not cover_already_replaced):
            if pending:
                replacement_cover = pending["replacement"]
            else:
                replacement_cover = upload_cover(album)
                previous_cover = card["metadata"]["cover"]["imageL"]
                if replacement_cover != previous_cover:
                    pending = {
                        "previous": previous_cover, "replacement": replacement_cover,
                        "sourceDigest": hashlib.sha256(cover_data(album)[0]).hexdigest(),
                    }
                    recorded["pendingCoverUpdate"] = pending
                    save_state()
                else:
                    cover_already_replaced = True
                    replacement_cover = None
        payload = fill_existing_icons(card, album, icons, replacement_cover)
        final_icons = [new if new is not None else previous
                       for new, previous in zip(icons, previous_icons, strict=True)]
        cover_replaced = cover_already_replaced or replacement_cover is not None
        STATE["albums"][title] = {
            **(recorded or {}), "cardId": card_id, "status": "verified",
            "trackCount": len(album["audio"]), "verification": "get_content",
            "payload": payload, "iconIds": final_icons, "iconsVerified": all(icons),
            "sourceUploaded": recorded.get("sourceUploaded", True) if recorded else False,
            "coverPlaceholder": False if cover_replaced else bool(recorded and recorded.get("coverPlaceholder")),
        }
        if cover_replaced:
            STATE["albums"][title].pop("pendingCoverUpdate", None)
        save_state()
        print(f"EXISTING_ICONS_VERIFIED {title}: {sum(icon is not None for icon in icons)} icons; "
              f"missing source={sum(icon is None for icon in icons)}; "
              f"placeholder cover replaced={'yes' if cover_replaced else 'no'}; no duplicate", flush=True)
        return
    if not recreate_missing and recorded and recorded.get("status") in {"pending_create", "created", "verified"}:
        print("RECOVERY_REQUIRED " + json.dumps({
            "type": "missing_prior_card", "title": title, "priorStatus": recorded["status"],
        }, ensure_ascii=False), flush=True)
        raise RuntimeError(f"Unreconciled prior create for {title}; refusing to create a duplicate")
    print(f"START {title}: {len(album['audio'])} tracks", flush=True)
    audio = [None] * len(album["audio"])
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(upload_audio, path): index
                   for index, path in enumerate(album["audio"])}
        for completed, future in enumerate(as_completed(futures), 1):
            index = futures[future]
            audio[index] = future.result()
            print(f"AUDIO {title}: {completed}/{len(audio)}", flush=True)
    cover_url = upload_cover(album)
    print(f"COVER_UPLOADED {title}", flush=True)
    icons = upload_icons(album)
    payload = build_payload(album, audio, cover_url, icons)
    if review and album_fingerprint(album) != review["localFingerprint"]:
        raise RuntimeError(f"Local files changed during upload; no card created: {title}")
    if recreate_missing or create_only:
        check_recreation_absent(title, recorded or {}, request_json("/content/mine")["cards"])
    STATE["albums"][title] = {
        "status": "pending_create", "payload": payload, "sourceUploaded": True,
        "coverPlaceholder": album["cover"] is None,
    }
    if recreate_missing:
        STATE["albums"][title]["previousRecords"] = previous_records
    save_state()
    print(f"SAVING {title}", flush=True)
    created = request_json("/content", method="POST", body=payload)["card"]
    card_id = created["cardId"]
    STATE["albums"][title].update({"status": "created", "cardId": card_id})
    save_state()
    check_card(created, album, payload)
    check_icons(created, icons)
    verification = "post_content_response"
    if full_read_enabled():
        card = request_json("/content/" + urllib.parse.quote(card_id, safe=""))["card"]
        check_card(card, album, payload)
        check_icons(card, icons)
        verification = "get_content"
    STATE["albums"][title].update({
        "status": "verified", "trackCount": len(audio), "verification": verification,
        "iconIds": icons, "iconsVerified": all(icons),
    })
    save_state()
    print(f"VERIFIED {title}: card={card_id}; tracks={len(audio)}; "
          f"icons={sum(icon is not None for icon in icons)}; "
          f"missing source icons={sum(icon is None for icon in icons)}; "
          f"cover={'placeholder' if album['cover'] is None else 'provided'}; shuffle=yes; via={verification}", flush=True)


def verify_album(album, library):
    matches = [card for card in library if card.get("title") == album["title"]]
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one remote card: {album['title']}")
    recorded = STATE["albums"].get(album["title"])
    if (not recorded or recorded.get("status") != "verified" or not recorded.get("payload")
            or not isinstance(recorded.get("iconIds"), list)
            or len(recorded["iconIds"]) != len(album["icons"])):
        raise RuntimeError(f"No saved source evidence for read-only verification: {album['title']}")
    if recorded.get("pendingCoverUpdate"):
        raise RuntimeError(f"Unfinished cover update; resume the upload before auditing: {album['title']}")
    card_id = matches[0]["cardId"]
    if recorded.get("cardId") != card_id:
        raise RuntimeError(f"Remote card ID differs from saved state: {album['title']}")
    card = request_json("/content/" + urllib.parse.quote(card_id, safe=""))["card"]
    count = check_card(card, album, recorded["payload"])
    icons = []
    for path, saved_icon in zip(album["icons"], recorded["iconIds"], strict=True):
        if path is None:
            icons.append(saved_icon)
            continue
        data = path.read_bytes()
        key, _ = icon_cache_key(data)
        if key not in STATE["icons"]:
            raise RuntimeError(f"Local icon differs from uploaded source: {album['title']}")
        icons.append(STATE["icons"][key])
    if icons != recorded["iconIds"]:
        raise RuntimeError(f"Local song/icon mapping changed: {album['title']}")
    check_icons(card, icons)
    tracks = [track for chapter in card["content"]["chapters"] for track in chapter["tracks"]]
    for path, track in zip(album["audio"], tracks, strict=True):
        if track["title"] != path.name:
            raise RuntimeError(f"Local song order/name changed: {album['title']}")
        media = STATE["audio"].get(digest_file(path))
        if recorded.get("sourceUploaded", True):
            if not media or track["trackUrl"] != "yoto:#" + media["transcodedSha256"]:
                raise RuntimeError(f"Local audio differs from uploaded source: {album['title']}")
    if recorded.get("sourceUploaded", True):
        cover_url = STATE["covers"].get(hashlib.sha256(cover_data(album)[0]).hexdigest())
        if not cover_url or card["metadata"]["cover"]["imageL"] != cover_url:
            raise RuntimeError(f"Local cover differs from uploaded source: {album['title']}")
    else:
        if (
            without_icons(card["content"]) != without_icons(recorded["payload"]["content"])
            or card["metadata"] != recorded["payload"]["metadata"]
        ):
            raise RuntimeError(f"Pre-existing card differs from retained baseline: {album['title']}")
        print(f"BASELINE_ONLY {album['title']}: pre-existing audio/cover preserved, not re-uploaded", flush=True)
    print(f"AUDITED {album['title']}: {count} tracks; "
          f"{sum(icon is not None for icon in icons)} remote icons checked; "
          f"missing source icons={sum(path is None for path in album['icons'])}; "
          f"cover={'placeholder' if recorded.get('coverPlaceholder') else 'present'}; shuffle=yes", flush=True)


def inspect_album(album, library):
    matches = [card for card in library if card.get("title") == album["title"]]
    result = {
        "title": album["title"], "presence": "missing", "cover": "unverified",
        "remoteTracks": None, "remoteIcons": None, "remoteCovers": None,
        "localFingerprint": None, "remoteFingerprint": None, "pendingCreate": False,
        "pendingOverwrite": False, "tracks": [], "error": None,
    }
    recorded = STATE["albums"].get(album["title"])
    if recorded is not None and not isinstance(recorded, dict):
        result["error"] = "Invalid saved album evidence"
        return result
    if recorded:
        result["pendingCreate"] = recorded.get("status") in {"pending_create", "created"}
        result["pendingOverwrite"] = recorded.get("status") == "pending_overwrite"
    try:
        result["localFingerprint"] = album_fingerprint(album)
    except (OSError, RuntimeError, KeyError, TypeError, ValueError) as error:
        result["error"] = f"Local files could not be checked: {error}"
        return result
    if len(matches) > 1:
        result["presence"] = "duplicate"
        return result
    if not matches:
        old_id = recorded.get("cardId") if recorded else None
        if old_id and any(card.get("cardId") == old_id for card in library):
            result["error"] = "PREVIOUS_CARD_RENAMED"
        return result
    result["presence"] = "present"
    try:
        card_id = matches[0].get("cardId")
        if not isinstance(card_id, str) or not card_id:
            raise RuntimeError("The matching Yoto card has no usable ID")
        card = request_json("/content/" + urllib.parse.quote(card_id, safe=""))["card"]
        if not isinstance(card, dict) or card.get("title") != album["title"] or card.get("cardId") != card_id:
            raise RuntimeError("The Yoto card changed while it was being checked")
        content = card.get("content")
        metadata = card.get("metadata")
        if not isinstance(content, dict) or not isinstance(metadata, dict):
            raise RuntimeError("The Yoto card has invalid content or metadata")
        chapters = content.get("chapters")
        if not isinstance(chapters, list):
            raise RuntimeError("The Yoto card has no readable chapter list")
        remote_tracks = []
        for chapter in chapters:
            if not isinstance(chapter, dict) or not isinstance(chapter.get("tracks"), list):
                raise RuntimeError("The Yoto card has an invalid chapter")
            chapter_display = chapter.get("display") or {}
            if not isinstance(chapter_display, dict):
                raise RuntimeError("The Yoto card has invalid icon metadata")
            for track in chapter["tracks"]:
                if not isinstance(track, dict):
                    raise RuntimeError("The Yoto card has an invalid track")
                if not isinstance(track.get("display") or {}, dict):
                    raise RuntimeError("The Yoto card has invalid icon metadata")
                remote_tracks.append((chapter, track))
        result["remoteTracks"] = len(remote_tracks)
        result["remoteIcons"] = sum(
            bool((track.get("display") or {}).get("icon16x16")
                 or (track.get("display") or {}).get("iconUrl16x16")
                 or (chapter.get("display") or {}).get("icon16x16")
                 or (chapter.get("display") or {}).get("iconUrl16x16"))
            for chapter, track in remote_tracks
        )
        cover = metadata.get("cover", {})
        if not isinstance(cover, dict):
            raise RuntimeError("The Yoto card has invalid cover metadata")
        remote_cover = cover.get("imageL")
        result["remoteCovers"] = int(any(cover.get(key) for key in ("imageL", "imageM", "imageS")))
        result["remoteFingerprint"] = card_fingerprint(card)
        if not remote_cover:
            result["cover"] = "missing"
        else:
            cover_digest = hashlib.sha256(cover_data(album)[0]).hexdigest()
            expected_cover = STATE["covers"].get(cover_digest)
            if expected_cover:
                result["cover"] = ("placeholder" if album["cover"] is None else "matched") \
                    if remote_cover == expected_cover else "different"
        for index in range(max(len(album["audio"]), len(remote_tracks))):
            path = album["audio"][index] if index < len(album["audio"]) else None
            chapter, track = remote_tracks[index] if index < len(remote_tracks) else (None, None)
            name = path.name if path else str(track.get("title") or "")
            remote_title = track.get("title") if track and isinstance(track.get("title"), str) else None
            item = {"name": name, "remoteTitle": remote_title, "audio": "unverified", "icon": "unverified"}
            if path is None:
                item.update(audio="extra", icon="extra")
            elif track is None:
                item.update(audio="missing", icon="missing")
            else:
                names_differ = remote_title is not None and remote_title != path.name
                media = STATE["audio"].get(digest_file(path))
                remote_url = track.get("trackUrl")
                if isinstance(media, dict) and isinstance(media.get("transcodedSha256"), str) \
                        and isinstance(remote_url, str) and remote_url:
                    item["audio"] = "matched" if remote_url == "yoto:#" + media["transcodedSha256"] else "different"
                elif names_differ:
                    item["audio"] = "name_mismatch"
                elif remote_title is not None:
                    item["audio"] = "name_only"
                chapter_display, track_display = chapter.get("display") or {}, track.get("display") or {}
                if not isinstance(chapter_display, dict) or not isinstance(track_display, dict):
                    raise RuntimeError("The Yoto card has invalid icon metadata")
                chapter_icon = chapter_display.get("icon16x16")
                track_icon = track_display.get("icon16x16")
                local_icon = album["icons"][index]
                if not chapter_icon and not track_icon:
                    item["icon"] = "missing"
                elif names_differ:
                    item["icon"] = "song_mismatch"
                elif remote_title is None:
                    item["icon"] = "unverified"
                elif not chapter_icon or not track_icon:
                    item["icon"] = "partial"
                elif chapter_icon != track_icon:
                    item["icon"] = "different"
                elif local_icon is None:
                    item["icon"] = "remote_only"
                else:
                    key, _ = icon_cache_key(local_icon.read_bytes())
                    expected_icon = STATE["icons"].get(key)
                    if expected_icon:
                        item["icon"] = "matched" if chapter_icon == "yoto:#" + expected_icon else "different"
            result["tracks"].append(item)
    except (OSError, RuntimeError, KeyError, TypeError, ValueError) as error:
        result.update(cover="unverified", remoteTracks=None, remoteIcons=None, remoteCovers=None,
                      remoteFingerprint=None, tracks=[], error=str(error))
    return result


def main():
    global ROOT, WORK, STATE_FILE, STATE
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True, help="Folder containing your audio and image files")
    parser.add_argument("--work-dir", type=Path, default=Path(".yoto-myo"),
                        help="Private credentials/cache directory; use one per account and collection")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--plan", action="store_true", help="List local albums; no API calls")
    modes.add_argument("--prepare-covers", action="store_true", help="Copy and rename covers locally")
    modes.add_argument("--existing-only", action="store_true", help="Fill existing cards' missing icons only")
    modes.add_argument("--create-only", action="store_true",
                       help="Create missing cards; leave already completed or unrelated cards unchanged")
    modes.add_argument("--update-existing", action="store_true",
                       help="Fill existing cards' missing icons and replace this tool's placeholder covers")
    modes.add_argument("--verify-only", action="store_true", help="Read-only remote/source audit; never POST")
    modes.add_argument("--inspect-only", action="store_true",
                       help="Report selected cards and source matches with read-only Yoto requests")
    modes.add_argument("--recreate-missing", action="store_true",
                       help="After confirming deletion, recheck and recreate exactly one previously recorded card")
    parser.add_argument("--album", action="append", help="Select an album; repeat to select multiple albums")
    parser.add_argument("--stop-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--review-plan", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.recreate_missing and (not args.album or len(args.album) != 1):
        parser.error("--recreate-missing requires exactly one --album")
    ROOT = args.root.expanduser().resolve()
    WORK = args.work_dir.expanduser().resolve()
    STATE_FILE = WORK / "upload-state.json"
    if not ROOT.is_dir():
        parser.error("--root must be an existing directory")
    if args.prepare_covers:
        if args.album:
            parser.error("--prepare-covers does not support --album")
        prepare_covers()
        return
    albums = inventory()
    print(f"LOCAL_INVENTORY: {len(albums)} albums; {sum(len(a['audio']) for a in albums)} tracks", flush=True)
    if args.plan:
        for album in albums:
            count = len(album["audio"])
            audio_issue = album.get("group_issue") or ("missing" if count == 0 else "too_many" if count > 100 else None)
            print(json.dumps({
                "title": album["title"], "tracks": count,
                "icons": sum(icon is not None for icon in album["icons"]),
                "cover": str(album["cover"]) if album["cover"] else None,
                "coverStatus": album["cover_status"],
                "coverCandidates": [str(path) for path in album["cover_candidates"]],
                "unmatchedIcons": [str(path) for path in album["unmatched_icons"]],
                "source": str(album["folder"].relative_to(ROOT)),
                "audioIssue": audio_issue,
                "trackDetails": [{
                    "name": path.name, "source": str(path),
                    "hasIcon": detail["path"] is not None, "issue": detail["reason"],
                    "icon": str(detail["path"]) if detail["path"] is not None else None,
                    "candidates": [str(candidate) for candidate in detail["candidates"]],
                } for path, detail in zip(album["audio"], album["icon_details"], strict=True)],
            }, ensure_ascii=False))
        return
    configure_macos_trust()
    with work_lock():
        if STATE_FILE.exists():
            STATE = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            for key in ("audio", "covers", "albums", "icons"):
                if not isinstance(STATE.get(key), dict):
                    raise RuntimeError(f"Invalid upload-state.json section: {key}")
        run_selected(args, albums)


def run_selected(args, albums):
    if not full_read_enabled():
        raise RuntimeError("user:content:view authorization is required for independent verification")
    library = request_json("/content/mine")["cards"]
    selected_titles = [args.album] if isinstance(args.album, str) else args.album
    if selected_titles:
        if len(set(selected_titles)) != len(selected_titles):
            raise RuntimeError("Each --album must be selected only once")
        requested = set(selected_titles)
        albums = [album for album in albums if album["title"] in requested]
        if {album["title"] for album in albums} != requested:
            raise RuntimeError("Each --album must match a discovered title")
    review_plan = {}
    if getattr(args, "review_plan", None):
        if not getattr(args, "create_only", False) or not selected_titles:
            raise RuntimeError("A reviewed upload requires explicitly selected cards")
        try:
            entries = json.loads(args.review_plan)
        except (TypeError, ValueError):
            raise RuntimeError("Invalid reviewed upload plan") from None
        if not isinstance(entries, list) or len(entries) != len(selected_titles):
            raise RuntimeError("Invalid reviewed upload plan")
        for item in entries:
            if (not isinstance(item, dict) or item.get("title") not in selected_titles
                    or item["title"] in review_plan or item.get("action") not in {"create", "replace", "resume"}
                    or not isinstance(item.get("localFingerprint"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", item["localFingerprint"])
                    or (item["action"] != "create" and (
                        not isinstance(item.get("remoteFingerprint"), str)
                        or not re.fullmatch(r"[a-f0-9]{64}", item["remoteFingerprint"])
                    ))):
                raise RuntimeError("Invalid reviewed upload plan")
            review_plan[item["title"]] = item
    guarded_library = any(getattr(args, flag, False) for flag in ("inspect_only", "create_only", "update_existing"))
    if guarded_library and not valid_card_library(library):
        raise RuntimeError("Cannot read Yoto's card list; no result was assumed")
    if getattr(args, "inspect_only", False):
        print(f"SELECTED: {len(albums)} cards", flush=True)
        for album in albums:
            print("INSPECTED " + json.dumps(inspect_album(album, library), ensure_ascii=False), flush=True)
            if getattr(args, "stop_file", None) and args.stop_file.exists():
                print("PAUSED: current card checked; remaining cards were not checked", flush=True)
                return
        print(f"FINISHED: {len(albums)} requested albums checked", flush=True)
        return
    if args.existing_only or getattr(args, "update_existing", False):
        remote_titles = {card.get("title") for card in library}
        for album in albums:
            if album["title"] not in remote_titles:
                print(f"SKIPPED {album['title']}: not_existing", flush=True)
        albums = [album for album in albums if album["title"] in remote_titles]
        if not albums:
            if getattr(args, "update_existing", False):
                print(f"FINISHED: 0 requested albums present exactly once; library has {len(library)} playlists", flush=True)
                return
            raise RuntimeError("No matching existing cards; no cards were created")
    eligible = []
    for album in albums:
        count = len(album["audio"])
        issue = album.get("group_issue") or ("missing" if count == 0 else "too_many" if count > 100 else None)
        if issue:
            if selected_titles and len(selected_titles) == 1:
                raise RuntimeError(f"Cannot process {album['title']}: {issue}")
            print(f"SKIPPED {album['title']}: {issue}", flush=True)
        else:
            eligible.append(album)
    albums = eligible
    if not albums:
        raise RuntimeError("No selected cards have an unambiguous group of 1–100 audio tracks")
    print(f"SELECTED: {len(albums)} cards", flush=True)
    processed_titles = set()
    for album in albums:
        if args.verify_only and not any(card.get("title") == album["title"] for card in library):
            raise RuntimeError(f"Album missing from remote library: {album['title']}")
        if args.verify_only:
            verify_album(album, library)
        else:
            if args.existing_only:
                run_album(album, library, replace_placeholder=False)
            elif getattr(args, "update_existing", False):
                if not any(card["title"] == album["title"] for card in library):
                    print(f"SKIPPED {album['title']}: not_existing", flush=True)
                    if getattr(args, "stop_file", None) and args.stop_file.exists():
                        print("PAUSED: current card finished; saved state is ready to resume", flush=True)
                        return
                    continue
                run_album(album, library)
            elif getattr(args, "recreate_missing", False):
                run_album(album, library, recreate_missing=True)
            elif getattr(args, "create_only", False):
                review = review_plan.get(album["title"])
                if review and album_fingerprint(album) != review["localFingerprint"]:
                    raise RuntimeError(f"Local files changed since review; scan and check again: {album['title']}")
                recorded = STATE["albums"].get(album["title"])
                if recorded is not None and not isinstance(recorded, dict):
                    raise RuntimeError(f"Invalid saved album evidence: {album['title']}")
                existing = [card for card in library if card.get("title") == album["title"]]
                unfinished = recorded and recorded.get("status") in {"pending_create", "created"}
                if review and review["action"] in {"replace", "resume"}:
                    run_album(album, library, replace_placeholder=False, create_only=True, review=review)
                elif review and existing and unfinished:
                    raise RuntimeError(f"The card appeared after review; check it again: {album['title']}")
                elif existing and not unfinished:
                    print(f"SKIPPED {album['title']}: already_existing", flush=True)
                    if getattr(args, "stop_file", None) and args.stop_file.exists():
                        print("PAUSED: current card finished; saved state is ready to resume", flush=True)
                        return
                    continue
                else:
                    if existing and not isinstance(recorded.get("payload"), dict):
                        raise RuntimeError(f"Invalid unfinished creation evidence: {album['title']}")
                    if review:
                        run_album(album, library, replace_placeholder=False, create_only=True, review=review)
                    else:
                        run_album(album, library, replace_placeholder=False, create_only=True)
            else:
                run_album(album, library)
            library = request_json("/content/mine")["cards"]
            if guarded_library and not valid_card_library(library):
                raise RuntimeError("Cannot read Yoto's card list; no result was assumed")
        processed_titles.add(album["title"])
        if getattr(args, "stop_file", None) and args.stop_file.exists():
            print("PAUSED: current card finished; saved state is ready to resume", flush=True)
            return
    actual_titles = [card.get("title") for card in library]
    for title in processed_titles:
        if actual_titles.count(title) != 1:
            raise RuntimeError(f"Final library title count is not exactly one: {title}")
    print(f"FINISHED: {len(processed_titles)} requested albums present exactly once; library has {len(library)} playlists", flush=True)


if __name__ == "__main__":
    main()
