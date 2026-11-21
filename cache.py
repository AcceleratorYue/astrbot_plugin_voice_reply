"""极简音频缓存：sha256 文本键 -> wav 文件，按文件数做 LRU 清理。"""

from __future__ import annotations

import os
from pathlib import Path


class AudioCache:
    def __init__(self, directory: Path, max_files: int = 200) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_files = max(0, int(max_files))

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.wav"

    def get(self, key: str) -> Path | None:
        path = self._path(key)
        if not path.is_file() or path.stat().st_size <= 44:
            return None
        try:
            os.utime(path, None)  # 命中即刷新 mtime，作为 LRU 依据
        except OSError:
            pass
        return path

    def put(self, key: str, data: bytes) -> Path:
        path = self._path(key)
        tmp = path.with_suffix(".wav.part")
        tmp.write_bytes(data)
        tmp.replace(path)
        self.prune()
        return path

    def prune(self) -> None:
        if self.max_files <= 0:
            return
        files = sorted(
            (p for p in self.dir.glob("*.wav") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        for old in files[: max(0, len(files) - self.max_files)]:
            try:
                old.unlink()
            except OSError:
                pass

    def stats(self) -> tuple[int, int]:
        files = list(self.dir.glob("*.wav"))
        total = 0
        for p in files:
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return len(files), total
