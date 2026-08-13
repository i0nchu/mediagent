"""JMComic vertical-slice image restoration, isolated from downloads."""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path


SCRAMBLE_268850 = 268850
SCRAMBLE_421926 = 421926


class JMComicImageError(ValueError):
    """Raised when a JMComic page cannot be restored safely."""


def scramble_segment_count(*, scramble_id: int | str, photo_id: int | str, filename: str) -> int:
    threshold = int(scramble_id)
    aid = int(photo_id)
    if aid < threshold:
        return 0
    if aid < SCRAMBLE_268850:
        return 10
    divisor = 10 if aid < SCRAMBLE_421926 else 8
    digest = hashlib.md5(f"{aid}{Path(filename).name}".encode()).hexdigest()
    return (ord(digest[-1]) % divisor) * 2 + 2


def restore_vertical_slices(content: bytes, *, segment_count: int) -> bytes:
    if segment_count == 0:
        return content
    if segment_count < 2:
        raise JMComicImageError("JMComic image segment count must be zero or at least two.")
    try:
        from PIL import Image
        with Image.open(BytesIO(content)) as source:
            source.load()
            width, height = source.size
            if height < segment_count:
                raise JMComicImageError("JMComic image is shorter than its segment count.")
            restored = Image.new(source.mode, source.size)
            remainder = height % segment_count
            base_height = height // segment_count
            for index in range(segment_count):
                move = base_height + (remainder if index == 0 else 0)
                source_y = height - base_height * (index + 1) - remainder
                destination_y = base_height * index + (remainder if index > 0 else 0)
                restored.paste(source.crop((0, source_y, width, source_y + move)), (0, destination_y))
            output = BytesIO()
            image_format = source.format or "PNG"
            restored.save(output, format=image_format)
            return output.getvalue()
    except JMComicImageError:
        raise
    except Exception as exc:
        raise JMComicImageError("JMComic image data could not be restored.") from exc


def materialize_page_content(content: bytes, runtime_decode: dict[str, object]) -> bytes:
    """Apply only the page transform declared by normalized runtime metadata."""
    if runtime_decode.get("provider") != "jmcomic":
        raise JMComicImageError("Runtime decode metadata does not belong to JMComic.")
    try:
        segment_count = int(runtime_decode.get("vertical_segments") or 0)
    except (TypeError, ValueError) as exc:
        raise JMComicImageError("JMComic runtime segment count is invalid.") from exc
    return restore_vertical_slices(content, segment_count=segment_count)
