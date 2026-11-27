"""Store uploaded files after checking them, so bad input is rejected with a readable message.

Checks, in order: a supported file type, not empty, within ``MAX_UPLOAD_MB``, and readable
(UTF-8 text for CSV/TSV/TXT, the Parquet magic bytes for Parquet). The file is streamed to disk
in chunks, so a large upload never sits in memory.
"""

from __future__ import annotations

import codecs
import re
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from tools.warehouse import SUPPORTED_SUFFIXES

CHUNK = 1 << 20


def _reject(code: int, message: str) -> HTTPException:
    return HTTPException(status_code=code, detail=message)


def safe_name(filename: str | None) -> str:
    name = Path(filename or "").name
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    if not name:
        raise _reject(status.HTTP_422_UNPROCESSABLE_CONTENT, "The uploaded file has no name")
    return name


async def save_upload(upload: UploadFile, directory: Path, max_mb: int) -> Path:
    """Validate and write one upload; returns its path. Raises HTTPException on bad input."""
    name = safe_name(upload.filename)
    suffix = Path(name).suffix.lower()
    file_format = SUPPORTED_SUFFIXES.get(suffix)
    if file_format is None:
        supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise _reject(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"{name}: unsupported file type {suffix or '(none)'}. Upload one of: {supported}",
        )

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    max_bytes = max_mb * 1024 * 1024
    decoder = codecs.getincrementaldecoder("utf-8")() if file_format == "csv" else None
    size = 0
    head = b""
    try:
        with path.open("wb") as out:
            while chunk := await upload.read(CHUNK):
                if size + len(chunk) > max_bytes:
                    raise _reject(
                        status.HTTP_413_CONTENT_TOO_LARGE,
                        f"{name} is larger than the {max_mb} MB upload limit",
                    )
                if decoder is not None:
                    try:
                        decoder.decode(chunk)
                    except UnicodeDecodeError as exc:
                        raise _reject(
                            status.HTTP_422_UNPROCESSABLE_CONTENT,
                            f"{name} is not UTF-8 text (byte 0x{chunk[exc.start]:02x} at "
                            f"position {size + exc.start}). Save it as UTF-8 and upload again",
                        ) from exc
                if size < 4:
                    head = (head + chunk)[:4]
                size += len(chunk)
                out.write(chunk)
        if decoder is not None:
            try:
                decoder.decode(b"", final=True)
            except UnicodeDecodeError as exc:
                raise _reject(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    f"{name} is not UTF-8 text: it ends in the middle of a character",
                ) from exc
        if size == 0:
            raise _reject(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{name} is empty")
        if file_format == "parquet" and head != b"PAR1":
            raise _reject(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"{name} is not a valid Parquet file"
            )
    except HTTPException:
        path.unlink(missing_ok=True)
        raise
    return path
