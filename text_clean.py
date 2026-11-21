"""把模型回复清洗成适合朗读的纯文本。

清洗目标是"念出来不别扭"：去掉代码块、链接、Markdown 标记、表情符号，
把换行压成空格。中文括号内容保留（MiMo 会把开头的括号当作风格标签）。
"""

from __future__ import annotations

import re

_CODE_FENCE = re.compile(r"```.*?```", re.S)
_IMAGE_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_MD = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"(?:https?://|www\.)\S+")
_HTML = re.compile(r"<[^>\n]{1,40}>")
_EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002B00-\U00002BFF"
    "\U0000FE0F"
    "\U0000200D"
    "]+"
)
_LIST_MARK = re.compile(r"(?m)^\s*(?:[-*+]\s+|\d+[.、)]\s*)")
_MARKUP = re.compile(r"[*_~`#>|]+")
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_WS = re.compile(r"\s+")
_CONTROL_TAG = re.compile(
    r"\[(?:quote|at[^\]]*|plain|voice)\]|【\s*语音\s*】|（\s*语音\s*）", re.I
)


def strip_tags(text: str, tags: list[str] | None) -> str:
    """去掉控制标签（如「（语音）」）。"""
    out = text or ""
    for tag in tags or []:
        if tag:
            out = out.replace(tag, "")
    return out


def contains_any(text: str | None, needles: list[str] | None) -> bool:
    """text 中是否包含 needles 里任意一个非空字符串。"""
    if not text:
        return False
    for needle in needles or []:
        if needle and needle in text:
            return True
    return False


def clean_for_tts(text: str, *, strip_emoji: bool = True) -> str:
    """清洗成可直接朗读的文本。"""
    if not text:
        return ""
    out = _CODE_FENCE.sub(" ", text)
    out = _IMAGE_MD.sub(" ", out)
    out = _LINK_MD.sub(r"\1", out)
    out = _URL.sub(" ", out)
    out = _HTML.sub(" ", out)
    out = _ZERO_WIDTH.sub("", out)
    if strip_emoji:
        out = _EMOJI.sub(" ", out)
    out = _LIST_MARK.sub("", out)
    out = _MARKUP.sub(" ", out)
    out = _CONTROL_TAG.sub(" ", out)
    out = _WS.sub(" ", out)
    return out.strip()
