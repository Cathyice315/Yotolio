"""Discover local audio and artwork without requiring a particular folder layout."""

from collections import Counter, defaultdict
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import unicodedata
import zlib


AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".aif", ".aiff", ".wma", ".mp4"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif"}
COVER_EXTENSIONS = {".png", ".jpg", ".jpeg"}
ASSET_DIRECTORIES = {"art", "artwork", "cover", "covers", "image", "images",
                     "picture", "pictures", "photo", "photos", "icon", "icons"}
AUDIO_DIRECTORIES = {"audio", "audios", "music", "songs", "song", "tracks", "track", "mp3", "media"}


def natural_key(value):
    return [(0, int(piece)) if piece.isdigit() else (1, piece.casefold())
            for piece in re.split(r"(\d+)", str(value))]


def clean_title(name):
    name = re.sub(r"^\d+\s*[-—–.]\s*", "", name)
    return re.sub(r"^(?:Yoto|youtube版)\s*[-—–]\s*", "", name, flags=re.I).strip()


def normalized(value):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold())


def _generic_directory(name, kinds):
    name = normalized(name)
    return any(name == kind + suffix for kind in kinds
               for suffix in ("", "file", "files", "asset", "assets"))


def track_number(stem, icon=False):
    prefix = r"(?:icon[\s._-]*)?" if icon else ""
    match = re.match(rf"^{prefix}(\d+)(?:$|[\s._-])", stem, re.I)
    return int(match.group(1)) if match else None


def track_name(stem, icon=False):
    if icon:
        stem = re.sub(r"^icon[\s._-]*", "", stem, flags=re.I)
    stem = re.sub(r"^\d+[\s._-]+", "", stem)
    return normalized(stem)


def is_cover_name(path):
    return bool(re.search(r"(?:^|[\s._-])(cover|front|artwork|album[\s_-]*art)(?:$|[\s._-])",
                          path.stem, re.I)) or path.stem.casefold() in {"封面", "专辑封面"}


def _text_frame(data):
    if not data:
        return ""
    encoding = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}.get(data[0])
    if not encoding:
        return ""
    try:
        return data[1:].decode(encoding, errors="replace").strip("\x00 \t\r\n").split("\x00")[0]
    except UnicodeError:
        return ""


def _syncsafe(data):
    if len(data) != 4 or any(byte & 0x80 for byte in data):
        return 0
    return (data[0] << 21) | (data[1] << 14) | (data[2] << 7) | data[3]


def _number_tag(value):
    match = re.match(r"\s*(\d+)", value or "")
    return int(match.group(1)) if match and int(match.group(1)) else None


def mp3_tags(path):
    tags = {}
    with path.open("rb") as stream:
        header = stream.read(10)
        if len(header) == 10 and header[:3] == b"ID3" and header[3] in {3, 4}:
            size = _syncsafe(header[6:10])
            if size <= 8_000_000 and not (header[5] & 0x40):
                data = stream.read(size)
                if header[5] & 0x80:
                    data = data.replace(b"\xff\x00", b"\xff")
                offset = 0
                while offset + 10 <= len(data):
                    frame = data[offset:offset + 4]
                    length_bytes = data[offset + 4:offset + 8]
                    length = _syncsafe(length_bytes) if header[3] == 4 else int.from_bytes(length_bytes, "big")
                    offset += 10
                    if not frame.strip(b"\0") or length <= 0 or offset + length > len(data):
                        break
                    if frame in {b"TALB", b"TIT2", b"TRCK", b"TPOS"}:
                        tags[frame] = _text_frame(data[offset:offset + length])
                    offset += length
        if not tags.get(b"TALB"):
            stream.seek(0, os.SEEK_END)
            if stream.tell() >= 128:
                stream.seek(-128, os.SEEK_END)
                tail = stream.read(128)
                if tail[:3] == b"TAG":
                    tags[b"TALB"] = tail[63:93].decode("latin-1").strip("\0 ")
                    if tail[125] == 0 and tail[126]:
                        tags[b"TRCK"] = str(tail[126])
    return {
        "album": tags.get(b"TALB", "").strip(),
        "title": tags.get(b"TIT2", "").strip(),
        "track": _number_tag(tags.get(b"TRCK")),
        "disc": _number_tag(tags.get(b"TPOS")) or 1,
    }


def mp4_tags(path):
    result = {}

    def atoms(stream, start, end):
        position = start
        while position + 8 <= end:
            stream.seek(position)
            header = stream.read(8)
            if len(header) != 8:
                break
            size, kind = struct.unpack(">I4s", header)
            header_size = 8
            if size == 1:
                extended = stream.read(8)
                if len(extended) != 8:
                    break
                size = struct.unpack(">Q", extended)[0]
                header_size = 16
            elif size == 0:
                size = end - position
            if size < header_size or position + size > end:
                break
            yield kind, position + header_size, position + size
            position += size

    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        length = stream.tell()

        def find(start, end, chain):
            for kind, body, stop in atoms(stream, start, end):
                if kind != chain[0]:
                    continue
                if len(chain) > 1:
                    find(body + (4 if kind == b"meta" else 0), stop, chain[1:])
                else:
                    for item, item_body, item_end in atoms(stream, body, stop):
                        if item not in {b"\xa9alb", b"\xa9nam", b"trkn", b"disk"}:
                            continue
                        for subkind, subbody, subend in atoms(stream, item_body, item_end):
                            if subkind != b"data" or subend - subbody > 4096:
                                continue
                            stream.seek(subbody + 8)
                            value = stream.read(max(0, subend - subbody - 8))
                            if item in {b"\xa9alb", b"\xa9nam"}:
                                result["album" if item == b"\xa9alb" else "title"] = (
                                    value.decode("utf-8", errors="replace").strip("\0 ")
                                )
                            elif len(value) >= 4:
                                result["track" if item == b"trkn" else "disc"] = int.from_bytes(value[2:4], "big")
        find(0, length, [b"moov", b"udta", b"meta", b"ilst"])
    return {"album": result.get("album", ""), "title": result.get("title", ""), "track": result.get("track"),
            "disc": result.get("disc") or 1}


def audio_tags(path):
    if path.suffix.casefold() == ".mp3":
        return mp3_tags(path)
    if path.suffix.casefold() in {".m4a", ".mp4"}:
        return mp4_tags(path)
    return {"album": "", "title": "", "track": None, "disc": 1}


def selection_store(work):
    path = work / "asset-selections.json"
    if not path.exists():
        return {"version": 1, "collections": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("collections"), dict):
        raise RuntimeError("Invalid asset-selections.json; correct the private selections before scanning")
    return data


def load_selections(work, root):
    collections = selection_store(work)["collections"]
    selections = collections.get(str(Path(root).resolve()), {})
    if not isinstance(selections, dict):
        raise RuntimeError("Invalid collection asset selections")
    return selections


def selected_file(value, extensions):
    if not isinstance(value, str) or not value:
        return None
    path = Path(value).expanduser().resolve()
    return path if path.is_file() and path.suffix.casefold() in extensions else None


def _file_list(root, work):
    audio, images = [], []
    def on_error(error):
        raise RuntimeError(f"Cannot read collection folder: {error}") from error

    for current, directories, filenames in os.walk(root, followlinks=False, onerror=on_error):
        folder = Path(current)
        linked = [name for name in directories if not name.startswith(".") and (folder / name).is_symlink()]
        if linked:
            raise RuntimeError(f"Linked folders are not scanned; select the original folder: {folder / linked[0]}")
        directories[:] = sorted(
            (name for name in directories
             if not name.startswith(".") and not (folder / name).is_symlink()
             and (folder / name).resolve() != work),
            key=natural_key,
        )
        for name in sorted(filenames, key=natural_key):
            path = folder / name
            extension = path.suffix.casefold()
            if name.startswith(".") or extension not in AUDIO_EXTENSIONS | IMAGE_EXTENSIONS:
                continue
            if path.is_symlink():
                raise RuntimeError(f"Media symlinks are not scanned; select the original file: {path}")
            if not path.is_file():
                continue
            if extension in AUDIO_EXTENSIONS:
                audio.append(path)
            elif extension in IMAGE_EXTENSIONS:
                images.append(path)
    return audio, images


def _nearby_images(folder, images):
    return [path for path in images
            if path.parent == folder
            or (path.parent.parent == folder and _generic_directory(path.parent.name, ASSET_DIRECTORIES))]


def _anchor(folder, root, audio_dirs, images, prepared_names):
    current = folder
    while current != root:
        parent = current.parent
        nearby = _nearby_images(parent, images)
        one_audio_folder = sum(path.is_relative_to(parent) for path in audio_dirs) == 1
        generic = _generic_directory(current.name, AUDIO_DIRECTORIES) or bool(
            re.fullmatch(r"(?:disc|disk|cd|part)[\s._-]*\d+", current.name, re.I))
        if parent == root:
            if generic and one_audio_folder and (nearby or len(audio_dirs) == 1):
                current = parent
            break
        if (generic or parent.name in prepared_names
                or any(is_cover_name(path) for path in nearby)
                or (one_audio_folder and nearby)):
            current = parent
        else:
            break
    return current


def _image_size(path):
    if path.suffix.casefold() == ".png":
        with path.open("rb") as stream:
            data = stream.read(24)
        if len(data) == 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
    if path.suffix.casefold() == ".gif":
        with path.open("rb") as stream:
            data = stream.read(10)
        if len(data) == 10 and data[:3] == b"GIF":
            return struct.unpack("<HH", data[6:10])
    if path.suffix.casefold() in {".jpg", ".jpeg"}:
        with path.open("rb") as stream:
            if stream.read(2) != b"\xff\xd8":
                return None
            while True:
                if stream.read(1) != b"\xff":
                    return None
                marker = stream.read(1)
                while marker == b"\xff":
                    marker = stream.read(1)
                if not marker or marker in {b"\xd9", b"\xda"}:
                    return None
                number = marker[0]
                if number == 0x01 or 0xD0 <= number <= 0xD8:
                    continue
                length = stream.read(2)
                if len(length) != 2:
                    return None
                length = int.from_bytes(length, "big")
                if length < 2:
                    return None
                if number in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                    data = stream.read(5)
                    if length >= 7 and len(data) == 5:
                        height, width = struct.unpack(">HH", data[1:5])
                        return (width, height) if width and height else None
                    return None
                stream.seek(length - 2, os.SEEK_CUR)
    return None


def _track_identity(track, tags):
    metadata = tags.get(track, {})
    number = track_number(track.stem)
    if number is None:
        number = metadata.get("track")
    names = {track_name(track.stem), normalized(metadata.get("title", ""))}
    return number, names - {""}


def _is_icon_like(path, audio, tags):
    if _generic_directory(path.parent.name, {"icon", "icons"}) or re.match(
            r"^icon(?:$|[\s._-])", path.stem, re.I):
        return True
    dimensions = _image_size(path)
    if dimensions and max(dimensions) <= 64:
        return True
    if dimensions and min(dimensions) >= 128:
        return False
    number = track_number(path.stem, icon=True)
    name = track_name(path.stem, icon=True)
    return any((number is not None and number == _track_identity(track, tags)[0])
               or (name and name in _track_identity(track, tags)[1]) for track in audio)


def _cover_for(album, images, root, shared, selection):
    if "cover" in selection:
        path = selected_file(selection["cover"], COVER_EXTENSIONS)
        return (path, "provided", [], True) if path else (None, "selected_missing", [], True)
    title = normalized(album["title"])
    folder_name = normalized(album["folder"].name)
    matching = [
        path for path in images if path.suffix.casefold() in COVER_EXTENSIONS
        and (normalized(path.stem) in {title, folder_name}
             or normalized(re.sub(r"^(cover|front|artwork)[\s._-]*", "", path.stem, flags=re.I)) == title)
        and (path.is_relative_to(album["folder"]) or path.is_relative_to(root / "Covers"))
        and (path.parent == root / "Covers" or not _is_icon_like(path, album["audio"], album["audio_tags"]))
    ]
    prepared = [path for path in matching if path.parent == root / "Covers"
                and path.stem == album["folder"].name]
    if prepared:
        matching = prepared
    if not matching:
        local = [path for path in images if path.is_relative_to(album["folder"])
                 and path.suffix.casefold() in COVER_EXTENSIONS]
        marked = [path for path in local if is_cover_name(path)]
        if shared:
            matching = marked
            if len(matching) == 1:
                return None, "ambiguous", matching, False
        else:
            matching = marked or [
                path for path in local if not _is_icon_like(path, album["audio"], album["audio_tags"])
            ]
    if len(matching) > 1:
        sized = [path for path in matching if (dimensions := _image_size(path))
                 and min(dimensions) >= 128]
        if sized:
            matching = sized
    if len(matching) == 1:
        return matching[0], "provided", matching, False
    return None, "ambiguous" if matching else "missing", matching, False


def _icon_choices(album, images, selection):
    overrides = selection.get("icons", {})
    if not isinstance(overrides, dict):
        raise RuntimeError(f"Invalid saved icon selections for {album['title']}")
    choices = []
    for track in album["audio"]:
        key = str(track)
        if key in overrides:
            chosen = selected_file(overrides[key], IMAGE_EXTENSIONS)
            choices.append({"path": chosen, "reason": None if chosen else "selected_missing",
                            "candidates": [], "selected": True})
            continue
        candidates = [path for path in images if path.is_relative_to(album["folder"])
                      and path != album["cover"] and path not in album["cover_candidates"]
                      and not is_cover_name(path)]
        number, names = _track_identity(track, album["audio_tags"])
        matches = []
        for path in candidates:
            image_number = track_number(path.stem, icon=True)
            image_name = track_name(path.stem, icon=True)
            if number is not None and image_number == number:
                matches.append(path)
            elif image_name in names and (number is None or image_number is None):
                matches.append(path)
        choices.append({
            "path": matches[0] if len(matches) == 1 else None,
            "reason": None if len(matches) == 1 else ("ambiguous" if matches else "missing"),
            "candidates": matches, "selected": False,
        })
    return choices


def discover(root, work):
    root, work = Path(root).resolve(), Path(work).resolve()
    selections = load_selections(work, root)
    audio, images = _file_list(root, work)
    audio_dirs = {path.parent for path in audio}
    prepared = root / "Covers"
    prepared_names = {path.stem for path in images if path.parent == prepared}
    grouped = defaultdict(list)
    tags = {path: audio_tags(path) for path in audio}
    for path in audio:
        grouped[_anchor(path.parent, root, audio_dirs, images, prepared_names)].append(path)
    albums = []
    for folder, tracks in sorted(grouped.items(), key=lambda item: natural_key(item[0])):
        album_names = {tags[path]["album"] for path in tracks if tags[path]["album"]}
        has_album_cover = (folder.name in prepared_names
                           or any(is_cover_name(path) for path in _nearby_images(folder, images)))
        split = len(album_names) > 1 and (folder == root or not has_album_cover)
        groups = defaultdict(list)
        if split:
            for path in tracks:
                groups[tags[path]["album"] or ""].append(path)
        else:
            groups[""] = tracks
        for tagged_title, group in groups.items():
            single_tag = next(iter(album_names)) if len(album_names) == 1 else ""
            title = clean_title(tagged_title or (single_tag if folder == root else "") or folder.name)
            if not title:
                raise RuntimeError(f"Cannot determine an album title for: {folder}")
            positions = [(tags[path]["disc"], tags[path]["track"]) for path in group]
            if all(track is not None for _, track in positions) and len(set(positions)) == len(group):
                group.sort(key=lambda path: (tags[path]["disc"], tags[path]["track"], natural_key(path)))
            else:
                group.sort(key=lambda path: natural_key(path.relative_to(folder)))
            albums.append({
                "title": title, "folder": folder, "audio": group,
                "audio_tags": tags,
                "group_issue": "untagged_in_mixed_folder" if split and not tagged_title else None,
            })
    for path in images:
        dimensions = _image_size(path)
        size_suggests_cover = (path.suffix.casefold() in COVER_EXTENSIONS and dimensions
                               and min(dimensions) >= 128 and not _is_icon_like(path, [], tags))
        if (not is_cover_name(path) and not size_suggests_cover) or (path.parent == prepared and audio):
            continue
        folder = path.parent
        if _generic_directory(folder.name, ASSET_DIRECTORIES) and folder != root:
            folder = folder.parent
        belongs_to_album = any(folder.is_relative_to(album["folder"]) for album in albums)
        if (not belongs_to_album and (folder != root or not audio)
                and not any(track.is_relative_to(folder) for track in audio)):
            if not any(album["folder"] == folder for album in albums):
                albums.append({"title": clean_title(folder.name), "folder": folder,
                               "audio": [], "audio_tags": tags, "group_issue": None})
    if not albums:
        raise RuntimeError("No audio files or identifiable album covers were found in the selected folder")
    title_counts = Counter(album["title"].casefold() for album in albums)
    duplicates = sorted({album["title"] for album in albums if title_counts[album["title"].casefold()] > 1},
                        key=natural_key)
    if duplicates:
        names = ", ".join(repr(title) for title in duplicates)
        raise RuntimeError(
            f"Cards need distinct names: {names}. Rename the card folders, or adjust album tags "
            "when grouping by tags, then scan again. Nothing was uploaded."
        )
    folder_counts = Counter(album["folder"] for album in albums)
    for album in albums:
        selection = selections.get(album["title"], {})
        if not isinstance(selection, dict):
            raise RuntimeError(f"Invalid saved asset selections for {album['title']}")
        cover, status, candidates, selected = _cover_for(
            album, images, root, folder_counts[album["folder"]] > 1, selection)
        album.update(cover=cover, cover_status=status, cover_candidates=candidates, cover_selected=selected)
        album["icon_details"] = _icon_choices(album, images, selection)
    cover_owners = defaultdict(list)
    for album in albums:
        if album["cover"] and not album["cover_selected"]:
            cover_owners[album["cover"]].append(album)
    for path, owners in cover_owners.items():
        if len(owners) > 1:
            for album in owners:
                album.update(cover=None, cover_status="ambiguous", cover_candidates=[path])
    owners = defaultdict(list)
    for album in albums:
        for detail in album["icon_details"]:
            if detail["path"] and not detail["selected"]:
                owners[detail["path"]].append(detail)
    for path, details in owners.items():
        if len(details) > 1:
            for detail in details:
                detail.update(path=None, reason="ambiguous", candidates=[path])
    claimed_images = set()
    for album in albums:
        if album["cover"]:
            claimed_images.add(album["cover"])
        claimed_images.update(album["cover_candidates"])
        for detail in album["icon_details"]:
            if detail["path"]:
                claimed_images.add(detail["path"])
            claimed_images.update(detail["candidates"])
    reported_folders = set()
    for album in albums:
        album["icons"] = [detail["path"] for detail in album["icon_details"]]
        album["unmatched_icons"] = []
        if album["folder"] not in reported_folders:
            album["unmatched_icons"] = [
                path for path in images
                if path.is_relative_to(album["folder"]) and path not in claimed_images and not is_cover_name(path)
            ]
            reported_folders.add(album["folder"])
    return sorted(albums, key=lambda album: natural_key(album["title"]))


def _chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


@lru_cache(maxsize=64)
def placeholder_cover(title):
    """Make a stable, locally generated PNG; no external image service is needed."""
    digest = hashlib.sha256(title.encode("utf-8")).digest()
    palettes = [
        ((247, 147, 108), (220, 75, 58), (255, 219, 118)),
        ((143, 196, 234), (45, 114, 181), (255, 203, 108)),
        ((166, 207, 165), (54, 135, 105), (255, 220, 135)),
        ((213, 176, 222), (142, 80, 157), (249, 194, 109)),
        ((255, 205, 124), (222, 113, 56), (129, 188, 201)),
    ]
    background, ink, accent = palettes[digest[0] % len(palettes)]
    size = 320
    rows = bytearray()
    bar_heights = [46, 76, 110, 67, 93, 52]
    for y in range(size):
        rows.append(0)
        for x in range(size):
            color = background
            if (x - 55) ** 2 + (y - 58) ** 2 < 27 ** 2 or (x - 275) ** 2 + (y - 264) ** 2 < 38 ** 2:
                color = accent
            if 49 <= x < 271 and 69 <= y < 251:
                color = (255, 250, 239)
                for index, height in enumerate(bar_heights):
                    left = 83 + index * 27
                    if left <= x < left + 13 and 160 - height // 2 <= y < 160 + height // 2:
                        color = ink
            rows.extend(color)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(rows), 9))
            + _chunk(b"IEND", b""))


def cover_data(album):
    if album["cover"] is None:
        return placeholder_cover(album["title"]), "placeholder.png", "image/png"
    path = album["cover"]
    mime = "image/png" if path.suffix.casefold() == ".png" else "image/jpeg"
    return path.read_bytes(), path.name, mime
