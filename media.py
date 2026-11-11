"""Bounded Telegram file transfers. No credentials or response bodies in errors."""

import json
import os
from pathlib import Path, PurePosixPath
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid

MAX_DOWNLOAD = 20 * 1024 * 1024
MAX_UPLOAD = 20 * 1024 * 1024


class MediaError(Exception):
    pass


def image_type(header):
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None, ".bin"


def download(api, attachment, update_id, root, limit):
    """Only called after authorization. Never use Telegram's filename as a path."""
    size = attachment.get("file_size", 0)
    if type(size) is not int or size < 0 or size > limit:
        raise MediaError("Attachment exceeds download size limit")
    info = api.call("getFile", {"file_id": attachment["file_id"]})
    if not isinstance(info, dict):
        raise MediaError("Invalid Telegram file metadata")
    remote = info.get("file_path", "")
    if (not isinstance(remote, str) or not remote or "\\" in remote
            or PurePosixPath(remote).is_absolute() or ".." in remote.split("/")
            or any(c in remote for c in ("?", "#", ":"))):
        raise MediaError("Invalid Telegram download path")
    remote_size = info.get("file_size", 0)
    if type(remote_size) is not int or remote_size < 0 or remote_size > limit:
        raise MediaError("Attachment exceeds download size limit")
    # The caller provides a workspace-local generated storage root. Original
    # filenames remain metadata, never filesystem paths. Each event is isolated.
    root = Path(root)
    workspace = Path.cwd().resolve()
    if not root.is_absolute():
        root = workspace / root
    root = root.resolve()
    if not root.is_relative_to(workspace):
        raise MediaError("Attachment directory must be inside workspace")
    directory = root / f"event-{update_id}"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not directory.resolve().is_relative_to(root):
        raise MediaError("Unsafe attachment directory")
    temp = None
    # api.base is https://api.telegram.org/botTOKEN/; do not log this URL.
    url = api.base.replace("/bot", "/file/bot", 1) + urllib.parse.quote(remote, safe="/")
    try:
        with urllib.request.urlopen(url, timeout=35) as response:
            length = response.headers.get("Content-Length")
            if length is not None and int(length) > limit:
                raise MediaError("Attachment exceeds download size limit")
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
                temp = Path(output.name)
                total = 0
                header = b""
                while True:
                    data = response.read(min(65536, limit - total + 1))
                    if not data:
                        break
                    total += len(data)
                    if total > limit:
                        raise MediaError("Attachment exceeds download size limit")
                    if len(header) < 512:
                        header += data[:512 - len(header)]
                    output.write(data)
                if not total:
                    raise MediaError("Empty Telegram attachment")
                output.flush()
                os.fsync(output.fileno())
        mime, extension = image_type(header)
        target = directory / (uuid.uuid4().hex + extension)
        os.replace(temp, target)
        return {"path": target.relative_to(workspace).as_posix(), "size": total,
                "mime_type": mime or "application/octet-stream",
                "original_name": str(attachment.get("file_name", ""))[:255]}
    except (OSError, ValueError):
        raise MediaError("Telegram download failed (network or local I/O)") from None
    finally:
        if temp is not None and temp.exists():
            temp.unlink()


def upload(api, method, field, path, params):
    """Bounded multipart upload; no retry (delivery can be ambiguous)."""
    path = Path(path)
    if not path.is_file():
        raise MediaError("Upload file is unavailable")
    with path.open("rb") as source:
        data = source.read(MAX_UPLOAD + 1)
    if not data or len(data) > MAX_UPLOAD:
        raise MediaError("Upload must be nonempty and at most 20 MiB")
    mime, extension = image_type(data[:512])
    if method == "sendPhoto" and (mime not in ("image/jpeg", "image/png") or len(data) > 10 * 1024 * 1024):
        raise MediaError("Photo upload requires JPEG/PNG at most 10 MiB; use send-document otherwise")
    # Generated ASCII filename avoids multipart header injection from local names.
    filename = "photo" + extension if field == "photo" else "attachment" + path.suffix.lower()[:20]
    if not filename.isascii() or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in filename):
        filename = "attachment.bin"
    boundary = uuid.uuid4().hex
    body = bytearray()
    for key, value in params.items():
        value = json.dumps(value) if isinstance(value, dict) else str(value)
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        body.extend(value.encode("utf-8") + b"\r\n")
    body.extend((f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
                 f'Content-Type: {mime or "application/octet-stream"}\r\n\r\n').encode())
    body.extend(data)
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    request = urllib.request.Request(api.base + method, data=bytes(body),
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.load(response)
        if not isinstance(result, dict) or not result.get("ok"):
            raise MediaError("Telegram rejected attachment upload")
    except urllib.error.HTTPError as exc:
        raise MediaError(f"Telegram upload API error ({exc.code})") from None
    except (OSError, ValueError):
        raise MediaError("Telegram upload failed; delivery may be ambiguous") from None
