from dataclasses import dataclass
from pathlib import Path
import tempfile
from urllib.request import Request, urlopen

import slack_image_inputs


DOCUMENT_EXTENSIONS = {
    ".txt",
    ".md",
    ".rst",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".csv",
    ".tsv",
    ".xml",
    ".html",
    ".htm",
    ".css",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".py",
    ".sh",
    ".java",
    ".jl",
    ".go",
    ".rs",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".sql",
    ".tex",
    ".log",
    ".pdf",
    ".docx",
    ".ipynb",
    ".diff",
    ".patch",
}

MIME_EXTENSION_HINTS = {
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/x-julia": ".jl",
    "application/json": ".json",
    "application/x-ndjson": ".jsonl",
    "application/x-ipynb+json": ".ipynb",
    "application/yaml": ".yaml",
    "application/x-yaml": ".yaml",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "text/html": ".html",
    "text/css": ".css",
    "text/javascript": ".js",
    "application/javascript": ".js",
    "application/x-sh": ".sh",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}

DOCUMENT_MIME_TYPES = set(MIME_EXTENSION_HINTS)

FILETYPE_EXTENSION_HINTS = {
    "docx": ".docx",
    "ipynb": ".ipynb",
    "julia": ".jl",
    "notebook": ".ipynb",
}


@dataclass(frozen=True)
class SlackDocumentDownload:
    file_id: str
    filename: str
    download_url: str
    mimetype: str = ""


@dataclass(frozen=True)
class DownloadedSlackDocument:
    file_id: str
    filename: str
    path: Path
    mimetype: str = ""


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


def _extension_from_filetype(filetype):
    normalized = _normalize_extension(filetype)
    if normalized in DOCUMENT_EXTENSIONS:
        return normalized
    alias = str(filetype or "").strip().lower()
    return FILETYPE_EXTENSION_HINTS.get(alias, "")


def _sanitize_filename_component(value):
    basename = Path(str(value or "")).name
    stem = Path(basename).stem
    sanitized = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in stem).strip("._")
    return sanitized or "document"


def is_document_like_file(file_obj):
    if not isinstance(file_obj, dict):
        return False
    if file_obj.get("is_external"):
        return False
    if slack_image_inputs.is_image_like_file(file_obj):
        return False

    mimetype = str(file_obj.get("mimetype") or "").strip().lower()
    if mimetype.startswith("text/"):
        return True
    if mimetype in DOCUMENT_MIME_TYPES:
        return True

    filetype = _extension_from_filetype(file_obj.get("filetype"))
    if filetype in DOCUMENT_EXTENSIONS:
        return True

    name_ext = _extension_from_name(file_obj.get("name"))
    return name_ext in DOCUMENT_EXTENSIONS


def _guess_extension(file_obj, fallback_name):
    name_ext = _extension_from_name(fallback_name)
    if name_ext in DOCUMENT_EXTENSIONS:
        return name_ext

    mimetype = str(file_obj.get("mimetype") or "").strip().lower()
    if mimetype in MIME_EXTENSION_HINTS:
        return MIME_EXTENSION_HINTS[mimetype]

    filetype = _extension_from_filetype(file_obj.get("filetype"))
    if filetype in DOCUMENT_EXTENSIONS:
        return filetype
    return ".txt"


def choose_download_filename(file_obj, index=0):
    basename = Path(str((file_obj or {}).get("name") or "")).name
    stem = _sanitize_filename_component(basename or (file_obj or {}).get("id") or f"document-{index + 1}")
    ext = _guess_extension(file_obj or {}, basename)
    return f"{stem}{ext}"


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


def build_document_downloads_from_event(event_payload):
    downloads = []
    for item in slack_image_inputs.extract_candidate_files(event_payload):
        if not is_document_like_file(item):
            continue
        download_url = slack_image_inputs.choose_download_url(item)
        if not download_url:
            continue
        file_id = str(item.get("id") or "").strip() or f"file-{len(downloads) + 1}"
        downloads.append(
            SlackDocumentDownload(
                file_id=file_id,
                filename=choose_download_filename(item, index=len(downloads)),
                download_url=download_url,
                mimetype=str(item.get("mimetype") or "").strip(),
            )
        )
    return downloads


def download_slack_document_files(downloads, bot_token, *, download_dir=None, timeout_seconds=30):
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
        base_dir = Path(tempfile.mkdtemp(prefix="codex-slack-documents-"))

    downloaded_files = []
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
            downloaded_files.append(
                DownloadedSlackDocument(
                    file_id=item.file_id,
                    filename=item.filename,
                    path=target_path,
                    mimetype=item.mimetype,
                )
            )
    except Exception as exc:
        cleanup_downloaded_documents(downloaded_files)
        raise RuntimeError(f"Failed downloading Slack document {current_file_id}") from exc

    return downloaded_files


def cleanup_downloaded_documents(downloaded_documents):
    documents = list(downloaded_documents or [])
    if not documents:
        return
    slack_image_inputs.cleanup_downloaded_files([item.path for item in documents])


def cleanup_download_directory(path):
    if not path:
        return
    slack_image_inputs.cleanup_download_directory(path)
