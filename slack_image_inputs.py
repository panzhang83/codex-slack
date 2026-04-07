import re
import shutil
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen


IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".avif",
}

MIME_EXTENSION_HINTS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/avif": ".avif",
}


@dataclass(frozen=True)
class SlackImageDownload:
    file_id: str
    filename: str
    download_url: str
    mimetype: str = ""


def _read_event_object(event_payload):
    if not isinstance(event_payload, dict):
        return {}
    event = event_payload.get("event")
    if isinstance(event, dict):
        return event
    return event_payload


def extract_candidate_files(event_payload):
    event = _read_event_object(event_payload)
    candidates = []
    seen_ids = set()

    sources = []
    files = event.get("files")
    if isinstance(files, list):
        sources.append(files)

    message = event.get("message")
    if isinstance(message, dict):
        nested_files = message.get("files")
        if isinstance(nested_files, list):
            sources.append(nested_files)

    for source in sources:
        for item in source:
            if not isinstance(item, dict):
                continue
            file_id = str(item.get("id") or "").strip()
            if file_id and file_id in seen_ids:
                continue
            if file_id:
                seen_ids.add(file_id)
            candidates.append(item)
    return candidates


def _normalize_extension(value):
    normalized = str(value or "").strip().lower()
    if not normalized:
        return ""
    if not normalized.startswith("."):
        normalized = f".{normalized}"
    return normalized


def _extension_from_name(name):
    basename = Path(str(name or "")).name
    return _normalize_extension(Path(basename).suffix)


def is_image_like_file(file_obj):
    if not isinstance(file_obj, dict):
        return False
    if file_obj.get("is_external"):
        return False

    mimetype = str(file_obj.get("mimetype") or "").strip().lower()
    if mimetype.startswith("image/"):
        return True

    filetype = _normalize_extension(file_obj.get("filetype"))
    if filetype in IMAGE_EXTENSIONS:
        return True

    name_ext = _extension_from_name(file_obj.get("name"))
    return name_ext in IMAGE_EXTENSIONS


def choose_download_url(file_obj):
    if not isinstance(file_obj, dict):
        return None
    for key in ("url_private_download", "url_private"):
        candidate = str(file_obj.get(key) or "").strip()
        if not candidate:
            continue
        parsed = urlparse(candidate)
        if parsed.scheme == "https" and parsed.netloc:
            return candidate
    return None


def _sanitize_filename_component(value):
    basename = Path(str(value or "")).name
    stem = Path(basename).stem
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")
    return sanitized or "image"


def _guess_extension(file_obj, fallback_name):
    name_ext = _extension_from_name(fallback_name)
    if name_ext in IMAGE_EXTENSIONS:
        return name_ext

    mimetype = str(file_obj.get("mimetype") or "").strip().lower()
    if mimetype in MIME_EXTENSION_HINTS:
        return MIME_EXTENSION_HINTS[mimetype]

    filetype = _normalize_extension(file_obj.get("filetype"))
    if filetype in IMAGE_EXTENSIONS:
        return filetype
    return ".img"


def choose_download_filename(file_obj, index=0):
    basename = Path(str((file_obj or {}).get("name") or "")).name
    stem = _sanitize_filename_component(basename or (file_obj or {}).get("id") or f"image-{index + 1}")
    ext = _guess_extension(file_obj or {}, basename)
    return f"{stem}{ext}"


def build_image_downloads_from_event(event_payload):
    downloads = []
    for item in extract_candidate_files(event_payload):
        if not is_image_like_file(item):
            continue
        download_url = choose_download_url(item)
        if not download_url:
            continue
        file_id = str(item.get("id") or "").strip() or f"file-{len(downloads) + 1}"
        downloads.append(
            SlackImageDownload(
                file_id=file_id,
                filename=choose_download_filename(item, index=len(downloads)),
                download_url=download_url,
                mimetype=str(item.get("mimetype") or "").strip(),
            )
        )
    return downloads


def _unique_path(base_dir: Path, filename: str):
    candidate = base_dir / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for i in range(2, 1000):
        candidate = base_dir / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Unable to allocate unique filename for {filename!r}.")


def download_slack_image_files(downloads, bot_token, *, download_dir=None, timeout_seconds=30):
    token = str(bot_token or "").strip()
    if not token:
        raise RuntimeError("Missing Slack bot token for file download.")

    items = list(downloads or [])
    if not items:
        return []

    if download_dir:
        base_dir = Path(download_dir)
        base_dir.mkdir(parents=True, exist_ok=True)
    else:
        base_dir = Path(tempfile.mkdtemp(prefix="codex-slack-images-"))

    downloaded_paths = []
    current_file_id = "-"
    try:
        for item in items:
            current_file_id = str(getattr(item, "file_id", "-") or "-")
            target_path = _unique_path(base_dir, item.filename)
            request = Request(
                item.download_url,
                headers={"Authorization": f"Bearer {token}"},
            )
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read()
            target_path.write_bytes(payload)
            downloaded_paths.append(target_path)
    except Exception as exc:
        cleanup_downloaded_files(downloaded_paths)
        raise RuntimeError(f"Failed downloading Slack image {current_file_id}") from exc

    return downloaded_paths


def cleanup_downloaded_files(paths):
    for raw_path in paths or []:
        path = Path(raw_path)
        with suppress(Exception):
            path.unlink()


def cleanup_download_directory(path):
    if not path:
        return
    with suppress(Exception):
        shutil.rmtree(Path(path), ignore_errors=True)
