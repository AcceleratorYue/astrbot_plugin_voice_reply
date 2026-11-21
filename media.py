"""语音输入转 WAV：优先本地 SILK（pysilk 解码），不依赖系统 ffmpeg。

QQ/NapCat 的语音有两个来源：
- OneBot 消息里的 ``path`` 字段：本地缓存，实际是 SILK（扩展名可能写成 .amr）；
- ``url`` 字段：multimedia 下载地址，格式是 AMR。

AstrBot 核心的 ``Record.convert_to_file_path()`` 会优先用 http url 下载 AMR，
再调用系统 ``ffmpeg`` 转码；本机没有 ffmpeg 时整条链路就断了。
这里改为：本地文件存在就直接按魔数解码（silk 用 pysilk、其它用 imageio-ffmpeg 自带二进制），
只有本地文件缺失时才回退给 AstrBot 的解析逻辑。
"""

from __future__ import annotations

import asyncio
import importlib
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from astrbot.api import logger
from astrbot.core.utils.tencent_record_helper import tencent_silk_to_wav

TMP_KEEP_SECONDS = 2 * 3600
_FFMPEG_CACHE: str | None = None


def detect_audio(path: Path) -> str:
    """按魔数判断音频格式（不信任扩展名）。"""
    try:
        with Path(path).open("rb") as stream:
            head = stream.read(16)
    except OSError:
        return ""
    if not head:
        return ""
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head.startswith(b"\x02#!SILK") or head.startswith(b"#!SILK"):
        return "silk"
    if head.startswith(b"#!AMR"):
        return "amr"
    if head[:3] == b"ID3" or (
        len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0
    ):
        return "mp3"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"fLaC":
        return "flac"
    if head[4:8] == b"ftyp":
        return "mp4"
    return ""


def _file_uri_to_path(value: str) -> str:
    parsed = urlparse(value)
    path = unquote(parsed.path)
    # Windows 的 file:///C:/... 会变成 /C:/...
    if len(path) >= 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path


def local_candidates(record) -> list[Path]:
    """从 Record 组件里收集可能存在的本地音频路径。"""
    result: list[Path] = []
    seen: set[str] = set()
    for attr in ("path", "file"):
        value = getattr(record, attr, None)
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        if value.startswith(("http://", "https://", "base64://", "data:")):
            continue
        if value.startswith("file://"):
            try:
                value = _file_uri_to_path(value)
            except Exception:
                continue
        try:
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = (Path.cwd() / candidate).resolve()
        except (OSError, ValueError):
            continue
        key = str(candidate)
        if key in seen or not candidate.is_file():
            continue
        seen.add(key)
        result.append(candidate)
    return result


def ffmpeg_exe() -> str:
    """优先用 AstrBot 环境里的 imageio-ffmpeg 二进制，其次系统 ffmpeg。"""
    global _FFMPEG_CACHE
    if _FFMPEG_CACHE:
        return _FFMPEG_CACHE
    try:
        module = importlib.import_module("imageio_ffmpeg")
        exe = module.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            _FFMPEG_CACHE = exe
            return exe
    except Exception:
        pass
    _FFMPEG_CACHE = "ffmpeg"
    return _FFMPEG_CACHE


async def _ffmpeg_to_wav(source: Path, tmp_dir: Path) -> Path | None:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    output = tmp_dir / f"stt_{time.time_ns()}.wav"
    args = [
        ffmpeg_exe(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-ac",
        "1",
        "-ar",
        "24000",
        "-c:a",
        "pcm_s16le",
        str(output),
    ]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=60)
    except Exception as exc:
        logger.warning(f"[voice_reply] ffmpeg 转码失败：{exc}")
        return None
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
    if output.is_file() and output.stat().st_size > 44 and detect_audio(output) == "wav":
        return output
    return None


async def to_wav(record, tmp_dir: Path) -> Path:
    """把一条语音 Record 转成 24kHz 单声道 WAV。"""
    for candidate in local_candidates(record):
        kind = detect_audio(candidate)
        if kind == "wav":
            return candidate
        if kind == "silk":
            tmp_dir.mkdir(parents=True, exist_ok=True)
            output = tmp_dir / f"stt_{time.time_ns()}.wav"
            await tencent_silk_to_wav(str(candidate), str(output))
            if output.is_file() and output.stat().st_size > 44:
                logger.debug(f"[voice_reply] 本地 SILK 解码：{candidate.name}")
                return output
            continue
        converted = await _ffmpeg_to_wav(candidate, tmp_dir)
        if converted is not None:
            logger.debug(f"[voice_reply] 本地 {kind or '音频'} 转码：{candidate.name}")
            return converted

    # 本地文件不可用时，交给 AstrBot 的解析逻辑（可能需要下载 url）
    try:
        resolved = Path(await record.convert_to_file_path())
        if detect_audio(resolved) == "wav":
            return resolved
        converted = await _ffmpeg_to_wav(resolved, tmp_dir)
        if converted is not None:
            return converted
    except Exception as exc:
        logger.debug(f"[voice_reply] AstrBot 音频解析失败：{exc}")
    raise RuntimeError("无法取得可用的语音文件")


def cleanup_tmp(tmp_dir: Path) -> None:
    """清理过期的转码临时文件。"""
    now = time.time()
    try:
        for path in Path(tmp_dir).glob("stt_*.wav"):
            try:
                if now - path.stat().st_mtime > TMP_KEEP_SECONDS:
                    path.unlink()
            except OSError:
                pass
    except OSError:
        pass
