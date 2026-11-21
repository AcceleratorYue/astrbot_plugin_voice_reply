"""MiMo TTS 客户端：克隆 / 音色设计 / 预置音色，带缓存、并发限制与重试。

接口（OpenAI Chat Completions 兼容）：
    POST {api_base}/chat/completions
    {"model": "mimo-v2.5-tts-voiceclone",
     "messages": [{"role": "user", "content": "<风格指令/音色描述，可选>"},
                  {"role": "assistant", "content": "<要念的文本>"}],
     "audio": {"format": "wav", "voice": "<参考音频 base64>"}}
返回音频在 choices[0].message.audio.data（base64）。

三种模型：
- mimo-v2.5-tts-voiceclone   参考音频克隆（audio.voice = 样本 base64，必填）
- mimo-v2.5-tts-voicedesign  文字描述设计音色（user 消息 = 描述，不用 audio.voice）
- mimo-v2.5-tts              预置音色（audio.voice = 音色 ID）
克隆模型每次请求都要重传样本，所以样本 base64 常驻内存，并参与缓存键。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from pathlib import Path

import httpx

from astrbot.api import logger

WAV_HEADER = (b"RIFF", b"WAVE")
SAMPLE_LIMIT = 10 * 1024 * 1024  # MiMo 参考音频上限 10MB
TMP_KEEP_SECONDS = 2 * 3600
MODES = ("clone", "voicedesign", "preset")


def detect_audio_mime(raw: bytes) -> str:
    """按文件头判断样本 MIME：克隆模型要求 audio.voice 是 DataURL。"""
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        return "audio/wav"
    if raw[:3] == b"ID3" or (len(raw) > 1 and raw[0] == 0xFF and (raw[1] & 0xE0) == 0xE0):
        return "audio/mpeg"
    return "audio/wav"


class MiMoError(RuntimeError):
    """MiMo 调用失败。"""


class MiMoHTTPError(MiMoError):
    """带状态码的 HTTP 失败（例如克隆模型被平台限流时的 429）。"""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class MiMoTTS:
    def __init__(self, conf, data_dir: Path, cache) -> None:
        self.conf = conf
        self.data_dir = Path(data_dir)
        self.cache = cache
        self.tmp_dir = self.data_dir / "tmp"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self._client: httpx.AsyncClient | None = None
        self._sem: asyncio.Semaphore | None = None
        self._sample_b64: str | None = None
        self._sample_sha: str | None = None
        self._sample_mime: str = "audio/wav"
        self.sample_path: Path | None = None
        self.sample_error: str | None = None
        self.last_mode: str = ""
        self.stats = {
            "cache_hits": 0,
            "syntheses": 0,
            "failures": 0,
            "fallbacks": 0,
            "transcribed": 0,
            "stt_failures": 0,
            "last_ms": 0,
        }
        self._cleanup_tmp()

    # ---------------------------------------------------------------- 配置读取
    def _cfg(self, key: str, default=None):
        try:
            return self.conf.get(key, default)
        except Exception:
            return default

    @property
    def api_key(self) -> str:
        return str(self._cfg("api_key", "") or "").strip()

    @property
    def api_base(self) -> str:
        base = str(self._cfg("api_base", "") or "https://api.xiaomimimo.com/v1")
        return base.rstrip("/")

    @property
    def voice_mode(self) -> str:
        mode = str(self._cfg("voice_mode", "clone"))
        return mode if mode in MODES else "clone"

    @property
    def fallback_mode(self) -> str:
        mode = str(self._cfg("fallback_mode", "none"))
        if mode not in MODES and mode != "none":
            return "none"
        if mode == self.voice_mode:
            return "none"
        return mode

    @property
    def voice_description(self) -> str:
        return str(self._cfg("voice_description", "") or "").strip()

    @property
    def timeout(self) -> float:
        try:
            return max(5.0, float(self._cfg("timeout", 60)))
        except (TypeError, ValueError):
            return 60.0

    @property
    def retries(self) -> int:
        try:
            return max(0, min(int(self._cfg("retry", 1)), 5))
        except (TypeError, ValueError):
            return 1

    @property
    def concurrency(self) -> int:
        try:
            return max(1, min(int(self._cfg("concurrency", 2)), 8))
        except (TypeError, ValueError):
            return 2

    def _model_name(self, mode: str | None = None) -> str:
        mode = mode or self.voice_mode
        if mode == "voicedesign":
            return str(
                self._cfg("design_model", "mimo-v2.5-tts-voicedesign")
                or "mimo-v2.5-tts-voicedesign"
            )
        if mode == "preset":
            return str(self._cfg("preset_model", "mimo-v2.5-tts") or "mimo-v2.5-tts")
        return str(
            self._cfg("clone_model", "mimo-v2.5-tts-voiceclone")
            or "mimo-v2.5-tts-voiceclone"
        )

    def _voice_identity(self, mode: str | None = None) -> str:
        mode = mode or self.voice_mode
        if mode == "voicedesign":
            raw = self.voice_description or "默认描述"
            return "design:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        if mode == "preset":
            return "preset:" + str(self._cfg("preset_voice", "冰糖") or "冰糖")
        return "clone:" + (self._sample_sha or "missing")

    # ------------------------------------------------------------------ 生命周期
    def reload(self) -> bool:
        """重新读取参考音频与相关配置。返回是否可用。"""
        self._reset_client()
        self._sample_b64 = None
        self._sample_sha = None
        self._sample_mime = "audio/wav"
        self.sample_path = None
        self.sample_error = None
        self._sem = None
        if self.voice_mode != "clone":
            return True
        raw_path = str(self._cfg("reference_audio", "") or "").strip()
        if not raw_path:
            self.sample_error = "未配置参考音频路径"
            return False
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if not path.is_file():
            self.sample_error = f"参考音频不存在：{path}"
            return False
        try:
            raw = path.read_bytes()
        except OSError as exc:
            self.sample_error = f"读取参考音频失败：{exc}"
            return False
        if len(raw) < 1024:
            self.sample_error = "参考音频过短（小于 1KB）"
            return False
        if len(raw) > SAMPLE_LIMIT:
            self.sample_error = "参考音频超过 10MB 上限"
            return False
        self._sample_b64 = base64.b64encode(raw).decode("ascii")
        self._sample_sha = hashlib.sha256(raw).hexdigest()
        self._sample_mime = detect_audio_mime(raw)
        self.sample_path = path
        return True

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass

    def _reset_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(client.aclose())
            except RuntimeError:
                pass

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            kwargs: dict = {
                "timeout": httpx.Timeout(self.timeout),
                "follow_redirects": True,
                "trust_env": False,
            }
            proxy = str(self._cfg("proxy", "") or "").strip()
            if proxy:
                kwargs["proxy"] = proxy
            self._client = httpx.AsyncClient(**kwargs)
            logger.debug(f"[voice_reply] MiMo client created (proxy={bool(proxy)})")
        return self._client

    def _ensure_sem(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.concurrency)
        return self._sem

    # ------------------------------------------------------------------- 请求
    def build_payload(self, text: str, mode: str | None = None) -> dict:
        mode = mode or self.voice_mode
        style = self.voice_description or str(self._cfg("style_prompt", "") or "").strip()
        messages: list[dict] = []
        if style:
            messages.append({"role": "user", "content": style})
        messages.append({"role": "assistant", "content": text})
        audio: dict = {"format": "wav"}
        if mode == "preset":
            audio["voice"] = str(self._cfg("preset_voice", "冰糖") or "冰糖")
        elif mode == "clone":
            if not self._sample_b64:
                raise MiMoError(self.sample_error or "参考音频不可用")
            # 克隆模型要求 DataURL（MiMo 文档只写 base64，实际网关要 data: 前缀）
            audio["voice"] = f"data:{self._sample_mime};base64,{self._sample_b64}"
        else:  # voicedesign：不传 audio.voice，靠 user 消息描述
            if not style:
                raise MiMoError("音色设计模式需要填写 voice_description")
        return {"model": self._model_name(mode), "messages": messages, "audio": audio}

    def cache_key(self, text: str, mode: str | None = None) -> str:
        mode = mode or self.voice_mode
        style = self.voice_description or str(self._cfg("style_prompt", "") or "").strip()
        raw = "|".join([self._model_name(mode), self._voice_identity(mode), style, text])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def synthesize(self, text: str) -> Path:
        text = (text or "").strip()
        if not text:
            raise MiMoError("待合成文本为空")
        mode = self.voice_mode
        try:
            return await self._synthesize_with(mode, text)
        except MiMoHTTPError as exc:
            fallback = self.fallback_mode
            if exc.status_code == 429 and fallback != "none":
                logger.warning(
                    f"[voice_reply] {mode} 被平台限流(429)，改用 {fallback} 合成"
                )
                self.stats["fallbacks"] += 1
                return await self._synthesize_with(fallback, text)
            raise

    async def _synthesize_with(self, mode: str, text: str) -> Path:
        self.last_mode = mode
        use_cache = bool(self._cfg("cache_enabled", True))
        key = self.cache_key(text, mode)
        if use_cache:
            hit = self.cache.get(key)
            if hit is not None:
                self.stats["cache_hits"] += 1
                logger.debug(f"[voice_reply] cache hit {key[:12]} ({len(text)} 字)")
                return hit
        started = time.monotonic()
        try:
            raw = await self._request(text, mode)
        except Exception:
            self.stats["failures"] += 1
            raise
        self.stats["syntheses"] += 1
        self.stats["last_ms"] = int((time.monotonic() - started) * 1000)
        if use_cache:
            return self.cache.put(key, raw)
        path = self.tmp_dir / f"voice_{time.time_ns()}.wav"
        path.write_bytes(raw)
        return path

    async def _request(self, text: str, mode: str) -> bytes:
        if not self.api_key:
            raise MiMoError("未配置 MiMo API Key")
        payload = self.build_payload(text, mode)
        url = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        attempts = self.retries + 1
        last_error: MiMoError | None = None
        async with self._ensure_sem():
            for attempt in range(attempts):
                try:
                    response = await self._ensure_client().post(
                        url, json=payload, headers=headers
                    )
                    if response.status_code >= 400:
                        raise MiMoHTTPError(
                            response.status_code,
                            f"HTTP {response.status_code}: {response.text[:300]}",
                        )
                    return self._decode(response)
                except (httpx.TimeoutException, httpx.ConnectError) as exc:
                    last_error = MiMoError(f"网络错误：{exc}")
                except MiMoHTTPError as exc:
                    if exc.status_code < 500:
                        raise  # 4xx 不重试：交给上层兜底
                    last_error = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(1.2 * (attempt + 1))
        raise last_error or MiMoError("请求失败")

    def _decode(self, response: httpx.Response) -> bytes:
        try:
            data = response.json()
        except Exception as exc:
            raise MiMoError(f"响应不是 JSON：{response.text[:200]}") from exc
        choices = data.get("choices") or []
        message = (choices[0] or {}).get("message", {}) if choices else {}
        encoded = (message.get("audio") or {}).get("data")
        if not encoded:
            raise MiMoError(f"响应缺少音频字段：{str(data)[:300]}")
        try:
            raw = base64.b64decode(encoded)
        except Exception as exc:
            raise MiMoError("音频 base64 解码失败") from exc
        if len(raw) < 44 or raw[:4] != WAV_HEADER[0] or raw[8:12] != WAV_HEADER[1]:
            raise MiMoError("返回音频不是合法 WAV")
        return raw

    # ------------------------------------------------------------------- ASR
    def build_asr_payload(self, audio_path) -> dict:
        """构造 MiMo ASR 请求体：单条 input_audio + DataURL。"""
        path = Path(audio_path)
        raw = path.read_bytes()
        if not raw:
            raise MiMoError("音频文件为空")
        mime = detect_audio_mime(raw)
        audio_format = "mp3" if mime == "audio/mpeg" else "wav"
        data_url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
        payload = {
            "model": str(self._cfg("stt_model", "mimo-v2.5-asr") or "mimo-v2.5-asr"),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_audio", "input_audio": {"data": data_url}}
                    ],
                }
            ],
            "max_completion_tokens": 1024,
        }
        language = str(self._cfg("stt_language", "auto") or "auto").strip()
        if language in ("zh", "en"):
            payload["asr_options"] = {"language": language}
        logger.debug(
            f"[voice_reply] ASR 请求：{audio_format} {len(raw) / 1024:.0f}KB lang={language}"
        )
        return payload

    async def transcribe(self, audio_path) -> str:
        """把音频文件转成文字（MiMo-V2.5-ASR）。"""
        payload = self.build_asr_payload(audio_path)
        data = await self._post_with_retry(payload)
        choices = data.get("choices") or []
        message = (choices[0] or {}).get("message", {}) if choices else {}
        text = message.get("content") or message.get("reasoning_content") or ""
        text = text.strip() if isinstance(text, str) else ""
        if not text:
            raise MiMoError(f"ASR 返回空文本：{str(data)[:200]}")
        self.stats["transcribed"] += 1
        return text

    async def _post_with_retry(self, payload: dict) -> dict:
        """通用 JSON 请求（供 ASR 使用）：429/5xx/超时重试，4xx 直接抛。"""
        if not self.api_key:
            raise MiMoError("未配置 MiMo API Key")
        url = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        attempts = self.retries + 1
        last_error: MiMoError | None = None
        async with self._ensure_sem():
            for attempt in range(attempts):
                try:
                    response = await self._ensure_client().post(
                        url, json=payload, headers=headers
                    )
                    if response.status_code >= 400:
                        raise MiMoHTTPError(
                            response.status_code,
                            f"HTTP {response.status_code}: {response.text[:300]}",
                        )
                    try:
                        return response.json()
                    except Exception as exc:
                        raise MiMoError(
                            f"响应不是 JSON：{response.text[:200]}"
                        ) from exc
                except (httpx.TimeoutException, httpx.ConnectError) as exc:
                    last_error = MiMoError(f"网络错误：{exc}")
                except MiMoHTTPError as exc:
                    if exc.status_code < 500:
                        raise
                    last_error = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(1.2 * (attempt + 1))
        raise last_error or MiMoError("请求失败")

    # ------------------------------------------------------------------ 维护
    def _cleanup_tmp(self) -> None:
        now = time.time()
        for path in self.tmp_dir.glob("voice_*.wav"):
            try:
                if now - path.stat().st_mtime > TMP_KEEP_SECONDS:
                    path.unlink()
            except OSError:
                pass

    def describe(self) -> dict:
        cache_files, cache_bytes = self.cache.stats()
        return {
            "mode": self.voice_mode,
            "fallback": self.fallback_mode,
            "model": self._model_name(),
            "description": self.voice_description,
            "sample_ok": bool(self._sample_b64) or self.voice_mode != "clone",
            "sample": str(self.sample_path) if self.sample_path else "",
            "sample_error": self.sample_error or "",
            "api_key_set": bool(self.api_key),
            "cache_files": cache_files,
            "cache_bytes": cache_bytes,
            "stt_model": str(self._cfg("stt_model", "mimo-v2.5-asr") or "mimo-v2.5-asr"),
            "stt_enabled": bool(self._cfg("stt_enabled", True)),
            "stats": dict(self.stats),
        }
