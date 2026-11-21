"""语音接管：关键词触发的整条语音回复。

实现要点
--------
AstrBot 内置 TTS 的触发点位于 ResultDecorateStage：分段回复之后、只处理 Plain 段、
且受 provider_tts_settings.trigger_probability 概率控制。本插件改在更早的
on_decorating_result 钩子里，把整条回复的纯文本一次性合成为一条语音，
并用 Record 替换消息链。链里不再有 Plain，于是分段、内置 TTS、文本转图片
都不会再介入，语音只发一条、触发完全确定。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.message_event_result import ResultContentType
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.star.filter.permission import PermissionType

from .acting import DOC_SFX, DOC_STYLES, apply_acting, strip_acting_markers
from .cache import AudioCache
from .media import cleanup_tmp, to_wav
from .mimo import MiMoTTS
from .text_clean import clean_for_tts, contains_any, strip_tags

PLUGIN_NAME = "astrbot_plugin_voice_reply"


@register("voice_reply", "AcceleratorYue", "关键词触发的整条语音回复（MiMo 音色克隆）", "1.7.1")
class VoiceReply(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        try:
            cache_max = int(self.conf.get("cache_max_files", 200))
        except (TypeError, ValueError):
            cache_max = 200
        self.cache = AudioCache(self.data_dir / "cache", cache_max)
        self.tts = MiMoTTS(self.conf, self.data_dir, self.cache)
        self.stats = {"spoken": 0, "too_long": 0, "failed": 0, "skipped": 0}
        self._reply_state: dict[str, dict] = {}
        self._streaming_warned = False

    # ------------------------------------------------------------ 生命周期
    async def initialize(self) -> None:
        cleanup_tmp(self.data_dir / "tmp")
        if self._cfg("proactive_voice_enabled", True):
            self._install_send_hook()
        ok = self.tts.reload()
        if not ok:
            logger.warning(f"[voice_reply] 参考音频不可用：{self.tts.sample_error}")
        self._self_check()

    async def terminate(self) -> None:
        self._remove_send_hook()
        await self.tts.aclose()

    def _self_check(self) -> None:
        try:
            conf = self.context.get_config()
        except Exception:
            return
        tts_conf = conf.get("provider_tts_settings", {}) or {}
        provider_conf = conf.get("provider_settings", {}) or {}
        if tts_conf.get("enable"):
            logger.info(
                "[voice_reply] 检测到 AstrBot 内置 TTS 已开启：本插件会把回复替换为语音，"
                "内置 TTS 不会触发，建议在配置里关闭「语音回复」以免混淆。"
            )
        if (self.context.get_config().get("provider_stt_settings") or {}).get(
            "enable"
        ) and self._cfg("stt_enabled", True):
            logger.info(
                "[voice_reply] 检测到 AstrBot 内置语音转文字已开启：插件转写会自动跳过，"
                "避免重复转写；如需由插件接管，请关闭内置 STT。"
            )
        if provider_conf.get("streaming_response"):
            logger.warning(
                "[voice_reply] streaming_response=true：流式回复无法被本插件接管，"
                "关键词触发将不生效。如需语音请关闭流式输出。"
            )

    # ------------------------------------------------------------ 配置读取
    def _cfg(self, key: str, default=None):
        try:
            return self.conf.get(key, default)
        except Exception:
            return default

    def _str_list(self, key: str) -> list[str]:
        value = self._cfg(key, [])
        if not isinstance(value, (list, tuple)):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def _refresh_conf_from_disk(self) -> None:
        path = getattr(self.conf, "config_path", None)
        if not path:
            return
        try:
            raw = Path(path).read_text(encoding="utf-8-sig")
            data = json.loads(raw)
        except (OSError, ValueError) as exc:
            logger.warning(f"[voice_reply] 重新读取配置失败：{exc}")
            return
        if isinstance(data, dict):
            self.conf.update(data)

    # ------------------------------------------------------------ 触发判定
    def _in_scope(self, umo: str) -> bool:
        def hit(keys: list[str]) -> bool:
            return any(key == umo or key in umo for key in keys)

        if hit(self._str_list("blacklist")):
            return False
        whitelist = self._str_list("whitelist")
        if whitelist:
            return hit(whitelist)
        return True

    def _triggered(self, user_text: str, reply_text: str) -> bool:
        if str(self._cfg("trigger_mode", "keyword")) == "always":
            return True
        trigger_on = str(self._cfg("trigger_on", "both"))
        hit_user = trigger_on in ("user", "both") and contains_any(
            user_text, self._str_list("user_keywords")
        )
        hit_reply = trigger_on in ("reply", "both") and contains_any(
            reply_text, self._str_list("reply_tags")
        )
        return hit_user or hit_reply

    # ------------------------------------------------------------ 钩子主体
    # 100000 = smart_reply_tag（剥离 [quote]/[at] 等标签并插入 Reply/At），必须排在它之后
    @filter.on_decorating_result(priority=99000)
    async def take_over_voice(self, event: AstrMessageEvent) -> None:
        try:
            await self._take_over(event)
        except Exception:
            logger.exception("[voice_reply] 语音接管异常，本次按原样发送")

    async def _take_over(self, event: AstrMessageEvent) -> None:
        if not self._cfg("enabled", True):
            return
        result = event.get_result()
        if result is None or not result.chain:
            return
        if not result.is_llm_result():
            return
        content_type = getattr(result, "result_content_type", None)
        if content_type in (
            ResultContentType.STREAMING_RESULT,
            ResultContentType.STREAMING_FINISH,
        ):
            if not self._streaming_warned:
                self._streaming_warned = True
                logger.warning("[voice_reply] 收到流式回复，跳过语音接管")
            return
        if not self._in_scope(event.unified_msg_origin):
            return

        user_text = event.message_str or ""
        raw_text, has_text = self._join_plain(result.chain)
        if not has_text or not raw_text.strip():
            return

        # 控制标签任何时候都要从消息里去掉（避免泄漏给用户，也不能被念出来）
        self._strip_tags_in_chain(result.chain)
        spoken_text, _ = self._join_plain(result.chain)
        # 演技标记无论是否触发语音都不该显示在聊天文字里
        if self._acting_enabled():
            self._strip_acting_in_chain(result.chain)
        umo = event.unified_msg_origin
        if not self._triggered(user_text, raw_text):
            self._record_reply_state(umo, voiced=False)
            return

        spoken = clean_for_tts(
            spoken_text, strip_emoji=bool(self._cfg("strip_emoji", True))
        )
        if self._acting_enabled():
            payload_text, spoken = apply_acting(
                spoken, self._style_allow(), self._sfx_allow()
            )
        else:
            payload_text = spoken
        min_chars = self._int_cfg("min_chars", 2, 1)
        max_chars = self._int_cfg("max_chars", 200, 1)
        if len(spoken) < min_chars:
            self.stats["skipped"] += 1
            self._record_reply_state(umo, voiced=False)
            return
        if len(spoken) > max_chars:
            self.stats["too_long"] += 1
            logger.info(
                f"[voice_reply] 回复 {len(spoken)} 字超过上限 {max_chars}，本次只发文字"
            )
            self._record_reply_state(umo, voiced=False)
            return

        try:
            audio_path = await self.tts.synthesize(payload_text)
        except Exception as exc:
            self.stats["failed"] += 1
            logger.warning(f"[voice_reply] 合成失败，退回文字：{exc}")
            self._record_reply_state(umo, voiced=False)
            return

        keep = [comp for comp in result.chain if not isinstance(comp, Comp.Plain)]
        keep.append(Comp.Record.fromFileSystem(str(audio_path)))
        result.chain = keep
        self.stats["spoken"] += 1
        self._record_reply_state(umo, voiced=True, chars=len(spoken))
        event.set_extra("_voice_reply_spoken", True)
        logger.info(
            f"[voice_reply] 已发送语音：{len(spoken)} 字 -> {audio_path.name}"
        )

    # ------------------------------------------------------------ 主动消息接管
    def _install_send_hook(self) -> None:
        """拦截 Context.send_message：定时任务/主动消息不经过发送前钩子。

        AstrBot 的 cron 消息由 send_message_to_user 工具发出，直接走
        Context.send_message，不经过 ResultDecorateStage，所以 on_decorating_result
        看不到它。这里在类级别包一层，把带触发标签的主动消息也合成成语音。
        """
        from astrbot.core.star.context import Context

        if getattr(Context, "_voice_reply_original_send", None) is None:
            Context._voice_reply_original_send = Context.send_message

            async def _patched_send(ctx_self, session, message_chain):
                owner = getattr(Context, "_voice_reply_owner", None)
                chain = message_chain
                if owner is not None:
                    try:
                        chain = await owner._maybe_voice_proactive(session, message_chain)
                    except Exception:
                        logger.exception("[voice_reply] 主动消息语音接管失败")
                original = Context._voice_reply_original_send
                return await original(ctx_self, session, chain)

            Context.send_message = _patched_send
            logger.info("[voice_reply] 已接管 Context.send_message（主动消息/定时任务）")
        Context._voice_reply_owner = self

    def _remove_send_hook(self) -> None:
        from astrbot.core.star.context import Context

        if getattr(Context, "_voice_reply_owner", None) is not self:
            return
        original = getattr(Context, "_voice_reply_original_send", None)
        if original is not None:
            Context.send_message = original
        for attr in ("_voice_reply_original_send", "_voice_reply_owner"):
            try:
                delattr(Context, attr)
            except AttributeError:
                pass
        logger.debug("[voice_reply] 已恢复 Context.send_message")

    async def _maybe_voice_proactive(self, session, message_chain):
        """定时任务等主动纯文本消息：命中触发标签就改发语音。"""
        if not self._cfg("enabled", True):
            return message_chain
        if not self._cfg("proactive_voice_enabled", True):
            return message_chain
        chain = getattr(message_chain, "chain", None)
        if not chain:
            return message_chain
        texts = [
            comp.text for comp in chain if isinstance(comp, Comp.Plain) and comp.text
        ]
        if not texts:
            return message_chain
        if any(not isinstance(comp, Comp.Plain) for comp in chain):
            return message_chain  # 含图片/文件等的消息不动

        umo = str(session)
        if not self._in_scope(umo):
            return message_chain

        raw_text = chr(10).join(texts)
        tags = self._str_list("reply_tags")
        triggered = contains_any(raw_text, tags) or bool(
            self._cfg("proactive_always_voice", False)
        )
        base_text = strip_tags(raw_text, tags)  # 控制标签一定剥掉
        acting = self._acting_enabled()
        display_text = (
            strip_acting_markers(base_text, self._style_allow(), self._sfx_allow())
            if acting
            else base_text
        )
        if not triggered:
            if display_text == raw_text:
                return message_chain
            return MessageChain([Comp.Plain(display_text)])

        cleaned = clean_for_tts(
            base_text, strip_emoji=bool(self._cfg("strip_emoji", True))
        )
        if acting:
            payload_text, spoken = apply_acting(
                cleaned, self._style_allow(), self._sfx_allow()
            )
        else:
            payload_text = spoken = cleaned
        if len(spoken) < self._int_cfg("min_chars", 2, 1):
            return MessageChain([Comp.Plain(display_text)])
        if len(spoken) > self._int_cfg("max_chars", 200, 1):
            logger.info(
                f"[voice_reply] 主动消息 {len(spoken)} 字超过上限，只发文字"
            )
            return MessageChain([Comp.Plain(display_text)])

        try:
            audio_path = await self.tts.synthesize(payload_text)
        except Exception as exc:
            logger.warning(f"[voice_reply] 主动消息合成失败，退回文字：{exc}")
            return MessageChain([Comp.Plain(display_text)])

        logger.info(
            f"[voice_reply] 主动消息改发语音：{len(spoken)} 字 -> {audio_path.name}"
        )
        self._record_reply_state(umo, voiced=True, chars=len(spoken))
        return MessageChain([Comp.Record.fromFileSystem(str(audio_path))])

    # ------------------------------------------------------------ 定时任务保底
    def _is_cron_event(self, event) -> bool:
        try:
            if event.get_platform_name() == "cron":
                return True
        except Exception:
            pass
        return type(event).__name__ == "CronMessageEvent"

    def _cron_tool_prompt(self) -> str:
        return (
            "【定时任务发送规则｜系统注入】这是定时任务，用户不会看到你的最终回复文本。"
            "你必须调用 send_message_to_user 工具把要发的内容发出去，"
            "把内容放进 messages 参数（type=plain）；只写最终回复等于没发。"
            "如果要发语音，就在工具参数文本的最前面写「（语音）」。"
        )

    @filter.on_agent_done()
    async def cron_fallback_delivery(self, event, run_context, response) -> None:
        """定时任务兜底：agent 没调用发送工具时，补发最终回复。

        AstrBot 的 cron 任务靠模型自己调用 send_message_to_user；如果模型把内容
        写在最终回复里而没调工具，消息不会发给用户。这里在 agent 结束时检查
        `_has_send_oper`，没有发送过就补发（并走本插件的语音接管）。
        """
        try:
            if not self._cfg("enabled", True):
                return
            if not self._cfg("cron_fallback_enabled", True):
                return
            if not self._is_cron_event(event):
                return
            if event.get_extra("_voice_reply_cron_fallback_done"):
                return
            event.set_extra("_voice_reply_cron_fallback_done", True)
            if getattr(event, "_has_send_oper", False):
                return
            text = ""
            if response is not None:
                text = str(getattr(response, "completion_text", "") or "").strip()
            if len(text) < self._int_cfg("min_chars", 2, 1):
                return
            umo = event.unified_msg_origin
            if not self._in_scope(umo):
                return
            logger.warning(
                f"[voice_reply] 定时任务没有调用发送工具，兜底补发：{text[:60]}"
            )
            chain = await self._maybe_voice_proactive(
                umo, MessageChain([Comp.Plain(text)])
            )
            await self.context.send_message(umo, chain)
            voiced = any(isinstance(c, Comp.Record) for c in chain.chain)
            self._record_reply_state(umo, voiced=voiced, chars=len(text))
        except Exception:
            logger.exception("[voice_reply] 定时任务兜底发送失败")

    # ------------------------------------------------------------ 语音转文字
    @filter.on_llm_request(priority=100000)
    async def transcribe_voice_input(self, event: AstrMessageEvent, req) -> None:
        """把消息里的语音转成文字，注入本轮 prompt 与 event.message_str。

        优先在 AstrBot 预处理器之前？不——预处理器已经把 Record 归一化成 wav，
        这里直接复用；若 AstrBot 内置 STT 已开启则跳过，避免重复转写。
        """
        try:
            if not self._cfg("enabled", True) or not self._cfg("stt_enabled", True):
                return
            if event.get_extra("_voice_reply_stt_done"):
                return
            event.set_extra("_voice_reply_stt_done", True)
            if self._builtin_stt_enabled():
                logger.debug("[voice_reply] AstrBot 内置 STT 已开启，插件转写跳过")
                return
            records: list = []
            for comp in event.get_messages():
                if isinstance(comp, Comp.Record):
                    records.append(comp)
                elif isinstance(comp, Comp.Reply) and getattr(comp, "chain", None):
                    # 引用消息里的语音也一并转写（与 AstrBot 内置 STT 行为一致）
                    records.extend(
                        sub for sub in comp.chain if isinstance(sub, Comp.Record)
                    )
            if not records:
                return
            texts: list[str] = []
            for record in records:
                try:
                    path = await to_wav(record, self.data_dir / "tmp")
                    text = await self.tts.transcribe(path)
                except Exception as exc:
                    self.tts.stats["stt_failures"] = (
                        self.tts.stats.get("stt_failures", 0) + 1
                    )
                    logger.warning(f"[voice_reply] 语音转文字失败：{exc}")
                    continue
                if text:
                    texts.append(text)
            if not texts:
                return
            transcript = "\n".join(texts).strip()
            prefix = str(self._cfg("stt_message_prefix", "") or "")
            injected = prefix + transcript
            req.prompt = (
                (req.prompt + "\n\n" + injected).strip()
                if getattr(req, "prompt", None)
                else injected
            )
            try:
                base = event.message_str or ""
                event.message_str = (base + injected) if base else injected
                message_obj = getattr(event, "message_obj", None)
                if message_obj is not None:
                    obj_text = getattr(message_obj, "message_str", None) or ""
                    message_obj.message_str = (
                        (obj_text + injected) if obj_text else injected
                    )
            except Exception:
                pass
            try:
                chain = event.get_messages()
                for index, comp in enumerate(chain):
                    if isinstance(comp, Comp.Record):
                        chain[index] = Comp.Plain(injected)
                        break
            except Exception:
                pass
            logger.info(f"[voice_reply] 语音转文字：{transcript[:80]}")
        except Exception:
            logger.debug("[voice_reply] 语音转文字异常", exc_info=True)

    def _builtin_stt_enabled(self) -> bool:
        try:
            conf = self.context.get_config()
            return bool((conf.get("provider_stt_settings") or {}).get("enable"))
        except Exception:
            return False

    # ------------------------------------------------------------ 提示词注入
    @filter.on_llm_request()
    async def inject_prompt_notes(self, event: AstrMessageEvent, req) -> None:
        """向 system_prompt 追加两段临时说明（不写入对话历史）。

        1. 能力说明：让角色知道自己能发语音；
        2. 状态说明：上一条回复是否以语音发出（弥补历史里只看得到文字）。
        """
        try:
            if not self._cfg("enabled", True):
                return
            if not self._in_scope(event.unified_msg_origin):
                return
            if event.get_extra("_voice_reply_prompt_injected"):
                return
            parts: list[str] = []
            if self._cfg("inject_capability_prompt", True):
                note = str(self._cfg("capability_prompt", "") or "").strip()
                if note:
                    parts.append(note)
            if self._acting_enabled() and self._cfg("inject_acting_prompt", True):
                acting_note = self._acting_prompt()
                if acting_note:
                    parts.append(acting_note)
            if self._cfg("cron_tool_prompt", True) and self._is_cron_event(event):
                parts.append(self._cron_tool_prompt())
            dynamic = self._last_voice_note(event.unified_msg_origin)
            if dynamic:
                parts.append(dynamic)
            if not parts:
                return
            event.set_extra("_voice_reply_prompt_injected", True)
            req.system_prompt = (
                (getattr(req, "system_prompt", "") or "") + "\n\n" + "\n\n".join(parts)
            )
            logger.debug("[voice_reply] 已注入提示词说明（%s 段）", len(parts))
        except Exception:
            logger.debug("[voice_reply] 注入提示词说明失败", exc_info=True)

    # ------------------------------------------------------------ 演技配置
    def _style_allow(self) -> list[str]:
        items = self._str_list("acting_style_list")
        return items or list(DOC_STYLES)

    def _sfx_allow(self) -> list[str]:
        items = self._str_list("acting_sfx_list")
        return items or list(DOC_SFX)

    def _acting_enabled(self) -> bool:
        return bool(self._cfg("acting_enabled", True))

    # ------------------------------------------------------------ 工具方法
    def _int_cfg(self, key: str, default: int, low: int = 0) -> int:
        try:
            return max(low, int(self._cfg(key, default)))
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------ 回复状态
    def _record_reply_state(self, umo: str, *, voiced: bool, chars: int = 0) -> None:
        """记录某个会话最近一条回复是否以语音发出（仅内存，重启失效）。"""
        self._reply_state[umo] = {
            "voiced": bool(voiced),
            "ts": time.time(),
            "chars": int(chars),
            "mode": getattr(self.tts, "last_mode", "") or "",
        }

    def _last_voice_note(self, umo: str) -> str:
        """上一条回复是语音时，生成一段临时状态说明。"""
        if not self._cfg("inject_last_voice_note", True):
            return ""
        state = self._reply_state.get(umo)
        if not state or not state.get("voiced"):
            return ""
        window = self._int_cfg("last_voice_note_window_min", 30, 1) * 60
        age = time.time() - float(state.get("ts", 0.0))
        if age < 0 or age > window:
            return ""
        ago = "刚刚" if age < 90 else f"{int(round(age / 60))} 分钟前"
        template = str(self._cfg("last_voice_note", "") or "").strip()
        if not template:
            return ""
        try:
            return template.format(ago=ago, chars=state.get("chars", 0))
        except (KeyError, IndexError, ValueError):
            return template.replace("{ago}", ago)

    @staticmethod
    def _join_plain(chain) -> tuple[str, bool]:
        parts: list[str] = []
        found = False
        for comp in chain:
            if isinstance(comp, Comp.Plain):
                text = comp.text or ""
                if text.strip():
                    found = True
                parts.append(text)
        return "\n".join(parts), found

    def _strip_tags_in_chain(self, chain) -> None:
        tags = self._str_list("reply_tags")
        if not tags:
            return
        for comp in chain:
            if isinstance(comp, Comp.Plain) and comp.text:
                stripped = strip_tags(comp.text, tags)
                if stripped != comp.text:
                    comp.text = stripped

    def _strip_acting_in_chain(self, chain) -> None:
        """聊天文字里不显示 [style:]/[sfx:] 标记。"""
        styles, sfx = self._style_allow(), self._sfx_allow()
        for comp in chain:
            if isinstance(comp, Comp.Plain) and comp.text:
                cleaned = strip_acting_markers(comp.text, styles, sfx)
                if cleaned != comp.text:
                    comp.text = cleaned

    def _acting_prompt(self) -> str:
        template = str(self._cfg("acting_prompt", "") or "").strip()
        if not template:
            return ""
        styles = "、".join(self._style_allow()[:36])
        sfx = "、".join(self._sfx_allow()[:24])
        return template.replace("{styles}", styles).replace("{sfx}", sfx)

    # ------------------------------------------------------------ 命令
    @filter.command("语音")
    async def voice_status(self, event: AstrMessageEvent):
        info = self.tts.describe()
        stats = info["stats"]
        trigger = str(self._cfg("trigger_mode", "keyword"))
        if trigger == "always":
            trigger_text = "always（所有 LLM 回复）"
        else:
            on = str(self._cfg("trigger_on", "both"))
            pieces = []
            if on in ("user", "both"):
                pieces.append("用户消息含 " + " / ".join(self._str_list("user_keywords")))
            if on in ("reply", "both"):
                pieces.append("回复含 " + " / ".join(self._str_list("reply_tags")))
            trigger_text = "keyword（" + "；".join(pieces) + "）"
        if info["mode"] == "clone":
            voice_text = (
                f"克隆（样本 {info['sample'] or '未配置'}，"
                f"{'可用' if info['sample_ok'] else '不可用：' + str(info['sample_error'])}）"
            )
        elif info["mode"] == "voicedesign":
            voice_text = "音色设计（" + (info["description"] or "未填写 voice_description") + "）"
        else:
            voice_text = "预置音色 " + str(self._cfg("preset_voice", "冰糖"))
        fallback = info.get("fallback") or "none"
        lines = [
            "语音接管 v1.7.1",
            f"触发：{trigger_text}",
            f"音色：{voice_text}（429 兜底：{'无' if fallback == 'none' else fallback}）",
            f"API Key：{'已配置' if info['api_key_set'] else '未配置'}",
            f"能力说明注入：{'开' if self._cfg('inject_capability_prompt', True) else '关'}",
            f"状态注入：{'开' if self._cfg('inject_last_voice_note', True) else '关'}（窗口 {self._int_cfg('last_voice_note_window_min', 30, 1)} 分钟）",
            (
                f"语音转文字：{'开' if self._cfg('stt_enabled', True) else '关'}"
                f"（{info.get('stt_model', 'mimo-v2.5-asr')}，已转 {stats.get('transcribed', 0)} 条，"
                f"失败 {stats.get('stt_failures', 0)}）"
            ),
            f"上限：{self._int_cfg('min_chars', 2)}~{self._int_cfg('max_chars', 200)} 字（超长只发文字）",
            f"缓存：{info['cache_files']} 个文件 / {info['cache_bytes'] / 1024:.1f}KB",
            (
                f"统计：语音 {self.stats['spoken']} 次｜缓存命中 {stats['cache_hits']}｜"
                f"兜底 {stats.get('fallbacks', 0)}｜超长转文字 {self.stats['too_long']}｜"
                f"失败 {self.stats['failed']}｜最近 {stats['last_ms']}ms"
            ),
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("语音试听", alias={"语音测试"})
    async def voice_test(self, event: AstrMessageEvent, text: GreedyStr):
        content = clean_for_tts(
            str(text).strip(), strip_emoji=bool(self._cfg("strip_emoji", True))
        )
        if not content:
            yield event.plain_result("格式：/语音试听 要念的内容")
            return
        try:
            audio_path = await self.tts.synthesize(content)
        except Exception as exc:
            yield event.plain_result(f"合成失败：{exc}")
            return
        yield event.chain_result([Comp.Record.fromFileSystem(str(audio_path))])

    @filter.command("语音重载")
    @filter.permission_type(PermissionType.ADMIN)
    async def voice_reload(self, event: AstrMessageEvent):
        self._refresh_conf_from_disk()
        ok = self.tts.reload()
        if ok:
            yield event.plain_result(f"已重载。参考音频：{self.tts.sample_path or '（预置音色模式）'}")
        else:
            yield event.plain_result(f"重载失败：{self.tts.sample_error}")
