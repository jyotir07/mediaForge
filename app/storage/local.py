import asyncio
import os
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


class InvalidStorageKey(ValueError):
    pass


class TooLarge(Exception):
    pass


def media_key(media_id: uuid.UUID, *parts: str) -> str:
    return "/".join(["media", str(media_id), *parts])


class Storage:
    """Local-filesystem object store. Keys are relative, slash-separated, and confined to root."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def path(self, key: str) -> Path:
        segments = key.split("/")
        if not key or any(s in ("", ".", "..") or not _SEGMENT.match(s) for s in segments):
            raise InvalidStorageKey(key)
        resolved = self.root.joinpath(*segments).resolve()
        if not resolved.is_relative_to(self.root):
            raise InvalidStorageKey(key)
        return resolved

    def exists(self, key: str) -> bool:
        return self.path(key).is_file()

    def tmp_path(self, key: str) -> Path:
        final = self.path(key)
        final.parent.mkdir(parents=True, exist_ok=True)
        return final.with_name(f"{final.name}.tmp-{uuid.uuid4().hex}")

    def commit(self, tmp: Path, key: str) -> None:
        final = self.path(key)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, final)

    def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)

    async def write_stream(self, key: str, chunks: AsyncIterator[bytes], max_bytes: int) -> int:
        tmp = self.tmp_path(key)
        written = 0
        try:
            with tmp.open("wb") as f:
                async for chunk in chunks:
                    written += len(chunk)
                    if written > max_bytes:
                        raise TooLarge(f"exceeds {max_bytes} bytes")
                    await asyncio.to_thread(f.write, chunk)
            self.commit(tmp, key)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return written
