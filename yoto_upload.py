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
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


ROOT = None
WORK = None
STATE_FILE = None
API = "https://api.yotoplay.com"
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".aif", ".aiff", ".wma", ".mp4"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif"}
LOCK = threading.RLock()
STATE = {"audio": {}, "covers": {}, "albums": {}, "icons": {}}


@contextmanager
def work_lock():
    import fcntl

    WORK.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(WORK / "upload.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another uploader is using this work directory") from None
        yield
    finally:
        os.close(descriptor)


def save_state():
    with LOCK:
        temporary = STATE_FILE.with_suffix(".new")
        descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(STATE, stream, ensure_ascii=False, indent=2)
        temporary.replace(STATE_FILE)


def token_data():
    path = WORK / "authorization.json"
    if not path.is_file():
        raise RuntimeError("Authorization missing; run yoto_authorize.py with the same --work-dir")
    token = json.loads(path.read_text())
    if token["obtained_at"] + token["expires_in"] < time.time() + 60:
        raise RuntimeError("Yoto access token expires within 60 seconds; new authorization required")
    return token


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
            raise RuntimeError(
                f"{method} {route}: HTTP {error.code}; check authorization, scopes, and API availability"
            ) from None
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt + 1 < attempts:
                print(f"RETRY {method} {route}: {type(error).__name__}", flush=True)
                time.sleep(min(60, 2 ** (attempt + 1)))
                continue
            raise RuntimeError(f"{method} {route}: {type(error).__name__}") from None


def natural_key(path):
    return [(0, int(piece)) if piece.isdigit() else (1, piece.casefold())
            for piece in re.split(r"(\d+)", str(path))]


def digest_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def matched_icons(folder, audio):
    icons = {}
    for path in folder.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif"}:
            continue
        match = re.match(r"^(?:icon\s*)?(\d+)(?:$|[\s._-])", path.stem, re.I)
        if not match:
            continue
        number = int(match.group(1))
        if number in icons:
            raise RuntimeError(f"Duplicate icon number {number}: {folder.name}")
        icons[number] = path
    expected = list(range(1, len(audio) + 1))
    audio_numbers = []
    for path in audio:
        match = re.match(r"^(\d+)", path.stem)
        audio_numbers.append(int(match.group(1)) if match else None)
    if sorted(icons) != expected or audio_numbers != expected:
        raise RuntimeError(f"Song/icon numbering mismatch: {folder.name}")
    return [icons[number] for number in expected]


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
    folders = album_folders()
    if not (ROOT / "Covers").is_dir():
        raise RuntimeError("Covers directory missing; run --prepare-covers first")
    albums = []
    for folder in folders:
        title = re.sub(r"^\d+\s*[-—–.]\s*", "", folder.name)
        title = re.sub(r"^(?:Yoto|youtube版)\s*[-—–]\s*", "", title, flags=re.I)
        covers = [p for p in (ROOT / "Covers").iterdir()
                  if p.is_file() and p.stem == folder.name]
        if len(covers) != 1:
            raise RuntimeError(f"Expected exactly one cover for {folder.name}")
        audio = sorted((p for p in folder.rglob("*")
                        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS),
                       key=lambda p: natural_key(p.relative_to(folder)))
        if not 1 <= len(audio) <= 100:
            raise RuntimeError(f"{title}: invalid track count {len(audio)}")
        albums.append({
            "title": title, "folder": folder, "cover": covers[0], "audio": audio,
            "icons": matched_icons(folder, audio),
        })
    if len({a["title"].casefold() for a in albums}) != len(albums):
        raise RuntimeError("Album titles must remain distinct after removing numbering")
    return albums


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
            except (urllib.error.URLError, TimeoutError) as error:
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


def upload_cover(path):
    digest = digest_file(path)
    if digest in STATE["covers"]:
        return STATE["covers"][digest]
    mime = mimetypes.guess_type(path.name)[0]
    if mime not in {"image/jpeg", "image/png"}:
        raise RuntimeError(f"Unexpected cover format: {path.name}")
    response = request_json(
        "/media/coverImage/user/me/upload", method="POST",
        body=path.read_bytes(), content_type=mime,
        query={"autoconvert": "true", "coverType": "default", "filename": path.name},
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
                   for index, path in enumerate(album["icons"])}
        for future in as_completed(futures):
            result[futures[future]] = future.result()
    print(f"ICONS_UPLOADED {album['title']}: {len(result)} song icons", flush=True)
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
            "display": {"icon16x16": "yoto:#" + icon},
        }
        chapters.append({
            "key": key, "title": path.name, "overlayLabel": str(index),
            "tracks": [track], "display": {"icon16x16": "yoto:#" + icon},
        })
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
    return "user:content:view" in token_data().get("scope", "").split()


def check_icons(card, icons):
    chapters = card["content"]["chapters"]
    if len(chapters) != len(icons):
        raise RuntimeError("Icon count does not match chapter count")
    for index, (chapter, icon) in enumerate(zip(chapters, icons, strict=True), 1):
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


def fill_existing_icons(card, album, icons):
    content = copy.deepcopy(card["content"])
    changed = False
    for chapter, path, icon in zip(content["chapters"], album["audio"], icons, strict=True):
        if len(chapter["tracks"]) != 1 or chapter["tracks"][0]["title"] != path.name:
            raise RuntimeError(f"Existing song order/name differs from local source: {album['title']}")
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
    if changed:
        backup_dir = WORK / "before-icons"
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        backup_file = backup_dir / (card["cardId"] + ".json")
        if not backup_file.exists():
            descriptor = os.open(backup_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                json.dump(card, stream, ensure_ascii=False, indent=2)
        current = request_json("/content/" + urllib.parse.quote(card["cardId"], safe=""))["card"]
        if any(current.get(key) != card.get(key) for key in ("updatedAt", "title", "content", "metadata")):
            raise RuntimeError(f"Card changed during icon upload; no update sent: {album['title']}")
        request_json("/content", method="POST", body=payload)
    updated = request_json("/content/" + urllib.parse.quote(card["cardId"], safe=""))["card"]
    if without_icons(updated["content"]) != without_icons(card["content"]):
        raise RuntimeError(f"Non-icon content changed during icon update: {album['title']}")
    if updated["metadata"] != card["metadata"] or updated["title"] != card["title"]:
        raise RuntimeError(f"Cover/metadata/title changed during icon update: {album['title']}")
    check_card(updated, album)
    check_icons(updated, icons)
    return payload


def run_album(album, library):
    title = album["title"]
    existing = [card for card in library if card.get("title") == title]
    if len(existing) > 1:
        raise RuntimeError(f"Duplicate existing title; refusing to create or alter: {title}")
    recorded = STATE["albums"].get(title)
    if existing:
        card_id = existing[0]["cardId"]
        if recorded and recorded.get("cardId") and recorded["cardId"] != card_id:
            raise RuntimeError(f"Remote card ID differs from saved state: {title}")
        if not full_read_enabled():
            raise RuntimeError("Existing card icon updates require explicit user:content:view authorization")
        card = request_json("/content/" + urllib.parse.quote(card_id, safe=""))["card"]
        check_card(card, album, recorded.get("payload") if recorded else None)
        icons = upload_icons(album)
        payload = fill_existing_icons(card, album, icons)
        STATE["albums"][title] = {
            **(recorded or {}), "cardId": card_id, "status": "verified",
            "trackCount": len(album["audio"]), "verification": "get_content",
            "payload": payload, "iconIds": icons, "iconsVerified": True,
            "sourceUploaded": recorded.get("sourceUploaded", True) if recorded else False,
        }
        save_state()
        print(f"EXISTING_ICONS_VERIFIED {title}: {len(icons)} icons; audio/cover/settings unchanged; no duplicate", flush=True)
        return
    if recorded and recorded.get("status") in {"pending_create", "created", "verified"}:
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
    cover_url = upload_cover(album["cover"])
    icons = upload_icons(album)
    payload = build_payload(album, audio, cover_url, icons)
    STATE["albums"][title] = {"status": "pending_create", "payload": payload, "sourceUploaded": True}
    save_state()
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
        "iconIds": icons, "iconsVerified": True,
    })
    save_state()
    print(f"VERIFIED {title}: card={card_id}; tracks={len(audio)}; icons={len(icons)}; cover=yes; shuffle=yes; via={verification}", flush=True)


def verify_album(album, library):
    matches = [card for card in library if card.get("title") == album["title"]]
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one remote card: {album['title']}")
    recorded = STATE["albums"].get(album["title"])
    if not recorded or recorded.get("status") != "verified" or not recorded.get("payload") or not recorded.get("iconIds"):
        raise RuntimeError(f"No saved source evidence for read-only verification: {album['title']}")
    card_id = matches[0]["cardId"]
    if recorded.get("cardId") != card_id:
        raise RuntimeError(f"Remote card ID differs from saved state: {album['title']}")
    card = request_json("/content/" + urllib.parse.quote(card_id, safe=""))["card"]
    count = check_card(card, album, recorded["payload"])
    icons = []
    for path in album["icons"]:
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
        cover_url = STATE["covers"].get(digest_file(album["cover"]))
        if not cover_url or card["metadata"]["cover"]["imageL"] != cover_url:
            raise RuntimeError(f"Local cover differs from uploaded source: {album['title']}")
    else:
        if (
            without_icons(card["content"]) != without_icons(recorded["payload"]["content"])
            or card["metadata"] != recorded["payload"]["metadata"]
        ):
            raise RuntimeError(f"Pre-existing card differs from retained baseline: {album['title']}")
        print(f"BASELINE_ONLY {album['title']}: pre-existing audio/cover preserved, not re-uploaded", flush=True)
    print(f"AUDITED {album['title']}: {count} tracks/icons; cover=yes; shuffle=yes", flush=True)


def main():
    global ROOT, WORK, STATE_FILE, STATE
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True, help="Collection containing album folders")
    parser.add_argument("--work-dir", type=Path, default=Path(".yoto-myo"),
                        help="Private credentials/cache directory; use one per account and collection")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--plan", action="store_true", help="List local albums; no API calls")
    modes.add_argument("--prepare-covers", action="store_true", help="Copy and rename covers locally")
    modes.add_argument("--existing-only", action="store_true", help="Fill existing cards' missing icons only")
    modes.add_argument("--verify-only", action="store_true", help="Read-only remote/source audit; never POST")
    parser.add_argument("--album")
    args = parser.parse_args()
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
            print(json.dumps({"title": album["title"], "tracks": len(album["audio"]), "icons": len(album["icons"])}, ensure_ascii=False))
        return
    with work_lock():
        if STATE_FILE.exists():
            STATE = json.loads(STATE_FILE.read_text())
            for key in ("audio", "covers", "albums", "icons"):
                if not isinstance(STATE.get(key), dict):
                    raise RuntimeError(f"Invalid upload-state.json section: {key}")
        run_selected(args, albums)


def run_selected(args, albums):
    if not full_read_enabled():
        raise RuntimeError("Explicit user:content:view authorization is required for independent verification")
    library = request_json("/content/mine")["cards"]
    if args.existing_only:
        remote_titles = {card.get("title") for card in library}
        albums = [album for album in albums if album["title"] in remote_titles]
        if not albums:
            raise RuntimeError("No matching existing cards; no cards were created")
    if args.album:
        albums = [album for album in albums if album["title"] == args.album]
        if len(albums) != 1:
            raise RuntimeError("--album must match exactly one title")
    for album in albums:
        if args.verify_only and not any(card.get("title") == album["title"] for card in library):
            raise RuntimeError(f"Album missing from remote library: {album['title']}")
        if args.verify_only:
            verify_album(album, library)
        else:
            run_album(album, library)
            library = request_json("/content/mine")["cards"]
    expected_titles = {album["title"] for album in albums}
    actual_titles = [card.get("title") for card in library]
    for title in expected_titles:
        if actual_titles.count(title) != 1:
            raise RuntimeError(f"Final library title count is not exactly one: {title}")
    print(f"FINISHED: {len(albums)} requested albums present exactly once; library has {len(library)} playlists", flush=True)


if __name__ == "__main__":
    main()
