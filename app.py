import os, re, json, base64, struct, wave, mimetypes
from pathlib import Path
from urllib.parse import quote
from flask import Flask, request, jsonify, abort, send_from_directory
import requests

try:
    from mutagen import File as MutagenFile
except Exception:
    MutagenFile = None

try:
    from bitchord_addon_auth import BitChordAuth
except Exception:
    BitChordAuth = None

app = Flask(__name__)
PORT = int(os.environ.get("PORT", "8080"))
MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", "music")).resolve()
CATALOG_FILE = Path(os.environ.get("CATALOG_FILE", "catalog.json")).resolve()
IA_BASE = "https://archive.org"
MAX_IA_RESULTS = 8
ALLOWED_EXTENSIONS = {".flac", ".wav", ".mp3", ".aac", ".m4a", ".ogg", ".opus"}

def configured_host():
    return os.environ.get("BITCHORD_HOST", "").strip().lower()

def auth_enabled():
    return bool(configured_host() and BitChordAuth)

AUTH = BitChordAuth(hosts=[configured_host()]) if auth_enabled() else None

QUALITY_OPTIONS = [
    {"value": "max", "label": "Max (best available source)"},
    {"value": "hires", "label": "Hi-Res lossless (verified 24-bit / >44.1 kHz)"},
    {"value": "lossless", "label": "Lossless (FLAC / ALAC / WAV)"},
    {"value": "high", "label": "High quality lossy (AAC / Opus / MP3)"}
]

def manifest_data():
    doc = {
        "id": "in.freeaudio.bitchord.ultimate",
        "name": "Ultimate Hi-Res + Dolby",
        "version": "2.0.0",
        "resources": ["search", "stream"],
        "allowDownloads": 1,
        "checkValidLossless": 0,
        "settings": [{
            "key": "quality",
            "type": "select",
            "label": "Audio quality",
            "default": "max",
            "options": QUALITY_OPTIONS
        }]
    }
    # Only claim the current BitChord auth protocol when its verifier and host
    # are configured. Set BITCHORD_HOST to the exact public hostname.
    if auth_enabled():
        doc["bitchordAuth"] = True
    return doc

def require_auth():
    if not auth_enabled():
        return None
    result = AUTH.verify_headers(request.headers)
    if not result.ok:
        return (result.body, result.status, result.headers)
    return None

def json_response(value, status=200):
    return jsonify(value), status, {"Cache-Control": "no-store"}

def encode_id(payload):
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

def decode_id(value):
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        obj = json.loads(raw.decode("utf-8"))
        if not isinstance(obj, dict):
            raise ValueError("bad id")
        return obj
    except Exception:
        abort(404)

def flac_streaminfo(data):
    """Read FLAC STREAMINFO from a native FLAC header; return verified basics."""
    if len(data) < 42 or data[:4] != b"fLaC":
        return {}
    block_header = data[4]
    block_type = block_header & 0x7f
    block_len = int.from_bytes(data[5:8], "big")
    if block_type != 0 or block_len < 34 or len(data) < 8 + 34:
        return {}
    packed = int.from_bytes(data[18:26], "big")
    sample_rate = (packed >> 44) & 0xfffff
    channels = ((packed >> 41) & 0x7) + 1
    bit_depth = ((packed >> 36) & 0x1f) + 1
    if not sample_rate or bit_depth < 4 or bit_depth > 32:
        return {}
    return {"sampleRate": sample_rate, "bitDepth": bit_depth, "channels": channels}

def local_metadata(path):
    ext = path.suffix.lower()
    meta = {"title": path.stem, "artist": "Local library", "album": "", "duration": None}
    if ext == ".flac":
        try:
            with path.open("rb") as f:
                meta.update(flac_streaminfo(f.read(64)))
        except Exception:
            pass
        meta.update({"format": "flac", "codec": "flac", "container": "flac", "lossless": True})
    elif ext == ".wav":
        try:
            with wave.open(str(path), "rb") as w:
                meta.update({
                    "sampleRate": w.getframerate(),
                    "bitDepth": w.getsampwidth() * 8,
                    "channels": w.getnchannels(),
                    "duration": round(w.getnframes() / max(1, w.getframerate()), 2)
                })
        except Exception:
            pass
        meta.update({"format": "wav", "codec": "pcm", "container": "wav", "lossless": True})
    else:
        codec = ext.lstrip(".")
        lossless = False
        if ext == ".m4a":
            codec = "aac"
            if MutagenFile:
                try:
                    audio = MutagenFile(str(path))
                    info_codec = str(getattr(getattr(audio, "info", None), "codec", "")).lower()
                    if "alac" in info_codec:
                        codec, lossless = "alac", True
                except Exception:
                    pass
        if ext in {".mp3", ".aac", ".ogg", ".opus"}:
            codec = {"ogg": "vorbis", "opus": "opus"}.get(codec, codec)
        meta.update({"format": ext.lstrip("."), "codec": codec, "container": ext.lstrip("."), "lossless": lossless})
    if MutagenFile and meta.get("duration") is None:
        try:
            audio = MutagenFile(str(path))
            info = getattr(audio, "info", None)
            if info:
                meta["duration"] = round(float(getattr(info, "length", 0) or 0), 2) or None
                if not meta.get("sampleRate") and getattr(info, "sample_rate", None):
                    meta["sampleRate"] = int(info.sample_rate)
                if not meta.get("bitDepth") and getattr(info, "bits_per_sample", None):
                    meta["bitDepth"] = int(info.bits_per_sample)
        except Exception:
            pass
    # Read common tags if available.
    if MutagenFile:
        try:
            audio = MutagenFile(str(path), easy=True)
            if audio:
                for src, dst in (("title", "title"), ("artist", "artist"), ("album", "album")):
                    value = audio.get(src)
                    if value:
                        meta[dst] = str(value[0])
        except Exception:
            pass
    return meta

def is_hires(meta):
    return bool(meta.get("lossless") and (
        (meta.get("bitDepth") or 0) > 16 or (meta.get("sampleRate") or 0) > 44100
    ))

def quality_class(meta):
    if meta.get("audioMode") == "DOLBY_ATMOS":
        return "dolby"
    if meta.get("lossless"):
        return "hires" if is_hires(meta) else "lossless"
    return "high"

def quality_allowed(meta, requested):
    cls = quality_class(meta)
    if requested in ("", "max"):
        return True
    if requested == "hires":
        return cls == "hires"
    if requested == "lossless":
        return cls in ("hires", "lossless")
    if requested in ("high", "HIGH"):
        return cls == "high"
    if requested in ("LOW", "low"):
        return cls == "high"
    if requested in ("LOSSLESS", "lossless"):
        return cls in ("hires", "lossless")
    return True

def local_search(q, quality):
    if not MUSIC_DIR.is_dir():
        return []
    needle = q.casefold()
    rows = []
    for path in MUSIC_DIR.iterdir():
        if not path.is_file() or path.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        meta = local_metadata(path)
        searchable = " ".join(str(meta.get(k, "")) for k in ("title", "artist", "album")).casefold()
        if needle not in searchable or not quality_allowed(meta, quality):
            continue
        row = {
            "id": encode_id({"kind": "local", "file": path.name}),
            "title": meta["title"],
            "artist": meta["artist"],
            "album": meta.get("album", ""),
            "format": meta["format"],
            "audioQuality": "LOSSLESS" if meta.get("lossless") else "HIGH"
        }
        if meta.get("duration"):
            row["duration"] = meta["duration"]
        if meta.get("audioMode") == "DOLBY_ATMOS":
            row.update({"audioMode": "DOLBY_ATMOS", "audioModes": ["DOLBY_ATMOS"], "atmos": True})
        rows.append((meta, row))
    rows.sort(key=lambda x: (0 if quality_class(x[0]) == "hires" else 1 if quality_class(x[0]) == "lossless" else 2))
    return [row for _, row in rows[:20]]

def license_is_open(md):
    """Conservative allowlist: only known public-domain or Creative Commons URLs."""
    metadata = md.get("metadata", {}) or {}
    license_url = str(metadata.get("licenseurl") or metadata.get("license_url") or "").lower()
    rights = str(metadata.get("rights") or "").lower()
    if "creativecommons.org/publicdomain/" in license_url:
        return True
    if re.search(r"creativecommons\.org/licenses/(by|by-sa|by-nc|by-nc-sa|zero)/", license_url):
        return True
    return "public domain" in rights or "public-domain" in rights

def archive_flac_info(identifier, filename):
    url = f"{IA_BASE}/download/{quote(identifier, safe='')}/{quote(filename, safe='')}"
    try:
        r = requests.get(url, headers={"Range": "bytes=0-63"}, timeout=10, stream=True)
        if r.status_code not in (200, 206):
            return {}
        return flac_streaminfo(r.raw.read(64))
    except Exception:
        return {}

def archive_search(q, quality):
    # Only openly licensed/public-domain records are surfaced; no commercial catalogue bypass.
    escaped = re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r'\\\1', q)
    params = [
        ("q", f'({escaped}) AND mediatype:audio'),
        ("fl[]", "identifier"), ("fl[]", "title"),
        ("fl[]", "creator"), ("fl[]", "year"),
        ("rows", str(MAX_IA_RESULTS)), ("page", "1"), ("output", "json")
    ]
    try:
        r = requests.get(f"{IA_BASE}/advancedsearch.php", params=params, timeout=15)
        r.raise_for_status()
        docs = r.json().get("response", {}).get("docs", [])
    except Exception:
        return []
    found = []
    for doc in docs:
        identifier = doc.get("identifier")
        if not identifier:
            continue
        try:
            mr = requests.get(f"{IA_BASE}/metadata/{quote(identifier, safe='')}", timeout=12)
            mr.raise_for_status()
            md = mr.json()
            if not license_is_open(md):
                continue
            flacs = [
                f for f in md.get("files", [])
                if str(f.get("name", "")).lower().endswith(".flac")
                and not f.get("private") and not f.get("viruscheck")
            ]
            if not flacs:
                continue
            flacs.sort(key=lambda f: int(f.get("size", 0) or 0), reverse=True)
            f = flacs[0]
            filename = str(f.get("name", ""))
            technical = archive_flac_info(identifier, filename)
            meta = {"lossless": True, **technical}
            if not quality_allowed(meta, quality):
                continue
            creator = doc.get("creator") or "Internet Archive contributor"
            if isinstance(creator, list):
                creator = ", ".join(map(str, creator[:3]))
            row = {
                "id": encode_id({"kind": "ia", "identifier": identifier, "file": filename}),
                "title": str(doc.get("title") or Path(filename).stem),
                "artist": str(creator),
                "album": str(doc.get("year") or ""),
                "format": "flac",
                "audioQuality": "LOSSLESS"
            }
            if technical.get("sampleRate") and technical.get("bitDepth"):
                row["audioQuality"] = "LOSSLESS · verified source metadata"
            found.append(row)
        except Exception:
            continue
    return found

def catalog_tracks(q, quality, atmos):
    """Optional catalog.json for provider URLs you own or are licensed to use."""
    if not CATALOG_FILE.is_file():
        return []
    try:
        items = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
        if not isinstance(items, list):
            return []
    except Exception:
        return []
    needle = q.casefold()
    rows = []
    for item in items:
        if not isinstance(item, dict) or not item.get("id") or not item.get("title") or not item.get("url"):
            continue
        if item.get("audioMode") == "DOLBY_ATMOS" and atmos != "auto":
            continue
        searchable = " ".join(str(item.get(k, "")) for k in ("title", "artist", "album")).casefold()
        if needle not in searchable:
            continue
        meta = {
            "lossless": str(item.get("codec", "")).lower() in ("flac", "alac", "wav", "pcm"),
            "sampleRate": item.get("sampleRate"),
            "bitDepth": item.get("bitDepth"),
            "audioMode": item.get("audioMode")
        }
        if not quality_allowed(meta, quality) and not (meta.get("audioMode") == "DOLBY_ATMOS" and atmos == "auto"):
            continue
        row = {
            "id": encode_id({"kind": "catalog", "entry": item}),
            "title": str(item["title"]),
            "artist": str(item.get("artist") or ""),
            "album": str(item.get("album") or ""),
            "format": str(item.get("format") or item.get("container") or ""),
            "audioQuality": "LOSSLESS" if meta["lossless"] else "HIGH"
        }
        if item.get("duration"):
            row["duration"] = item["duration"]
        if meta.get("audioMode") == "DOLBY_ATMOS":
            row.update({"audioMode": "DOLBY_ATMOS", "audioModes": ["DOLBY_ATMOS"], "atmos": True})
        rows.append(row)
    return rows[:20]

@app.get("/")
def home():
    return jsonify({"name": manifest_data()["name"], "status": "ok", "manifest": "/manifest.json"})

@app.get("/manifest.json")
def manifest():
    return json_response(manifest_data())

@app.get("/search")
def search():
    denied = require_auth()
    if denied:
        return denied
    q = (request.args.get("q") or "").strip()
    if not q:
        return json_response({"tracks": []})
    quality = request.args.get("quality", "max")
    atmos = request.args.get("atmos", "")
    rows = catalog_tracks(q, quality, atmos)
    rows += local_search(q, quality)
    rows += archive_search(q, quality)
    # Prefer immersive only when BitChord explicitly requests Atmos auto.
    if atmos == "auto":
        rows.sort(key=lambda r: 0 if r.get("atmos") else 1)
    return json_response({"tracks": rows[:30]})

@app.get("/stream/<track_id>")
def stream(track_id):
    denied = require_auth()
    if denied:
        return denied
    item = decode_id(track_id)
    quality = request.args.get("quality", "max")
    atmos = request.args.get("atmos", "")
    if item.get("kind") == "local":
        name = os.path.basename(item.get("file", ""))
        path = (MUSIC_DIR / name).resolve()
        if path.parent != MUSIC_DIR or not path.is_file() or path.suffix.lower() not in ALLOWED_EXTENSIONS:
            abort(404)
        meta = local_metadata(path)
        if not quality_allowed(meta, quality):
            abort(404)
        url = request.host_url.rstrip("/") + "/media/" + quote(name)
        response = {
            "url": url, "format": meta["format"], "codec": meta["codec"],
            "container": meta["container"], "manifest": "none", "encrypted": False
        }
        if meta.get("sampleRate"):
            response["sampleRate"] = meta["sampleRate"]
        if meta.get("bitDepth"):
            response["bitDepth"] = meta["bitDepth"]
        if meta.get("duration"):
            response["duration"] = meta["duration"]
        return json_response(response)
    if item.get("kind") == "ia":
        identifier, name = item.get("identifier", ""), item.get("file", "")
        if not identifier or not name.lower().endswith(".flac") or "/" in name or "\\" in name:
            abort(404)
        url = f"{IA_BASE}/download/{quote(identifier, safe='')}/{quote(name, safe='')}"
        tech = archive_flac_info(identifier, name)
        if quality == "hires" and not (tech.get("bitDepth", 0) > 16 or tech.get("sampleRate", 0) > 44100):
            abort(404)
        response = {
            "url": url, "format": "flac", "quality": "FLAC source",
            "codec": "flac", "container": "flac", "manifest": "none", "encrypted": False
        }
        response.update(tech)
        return json_response(response)
    if item.get("kind") == "catalog":
        entry = item.get("entry") or {}
        url = entry.get("url", "")
        if not url.startswith("https://"):
            abort(404)
        if entry.get("audioMode") == "DOLBY_ATMOS" and atmos != "auto":
            abort(404)
        response = {
            "url": url,
            "format": entry.get("format") or entry.get("container") or "",
            "codec": entry.get("codec") or "",
            "container": entry.get("container") or "",
            "manifest": entry.get("manifest") or "none",
            "encrypted": bool(entry.get("encrypted", False))
        }
        for key in ("sampleRate", "bitDepth", "bitrate", "audioMode"):
            if entry.get(key) is not None:
                response[key] = entry[key]
        if entry.get("audioMode") == "DOLBY_ATMOS":
            response["quality"] = "Dolby Atmos (lossy immersive mix)"
        return json_response(response)
    abort(404)

@app.get("/media/<path:filename>")
def media(filename):
    # Media requests do not carry BitChord auth headers; keep this route limited
    # to files deliberately placed in the server's own music directory.
    name = os.path.basename(filename)
    path = (MUSIC_DIR / name).resolve()
    if path.parent != MUSIC_DIR or not path.is_file() or path.suffix.lower() not in ALLOWED_EXTENSIONS:
        abort(404)
    return send_from_directory(MUSIC_DIR, name, conditional=True)

@app.get("/health")
def health():
    count = sum(1 for p in MUSIC_DIR.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED_EXTENSIONS) if MUSIC_DIR.is_dir() else 0
    return jsonify({"ok": True, "local_audio_files": count, "auth_enabled": auth_enabled()})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
