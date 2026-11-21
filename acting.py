"""语音演技标记解析。

MiMo TTS 的原生语法：
- 行首 ``(风格)``  → 风格/情绪控制，不念出来（未知内容也会被吞）；
- 任意位置 ``[标签]`` → 音频标签（笑声/叹气/停顿等），不念出来。

为了避免模型把普通括号/方括号写进台词后被静默吞掉，本插件给模型一套专用标记：

    [style:温柔 俏皮]   放在开头 → 翻译为 （温柔 俏皮）
    [sfx:轻笑]          放在句中 → 翻译为 [轻笑]

解析后得到两份文本：
- payload：真正发给 MiMo 的文本（含原生风格/音频标签）；
- display：去掉所有标记后的台词（用于聊天文字/长度统计）。

白名单为空时使用 MiMo 文档里的推荐词表（自定义风格 MiMo 也接受，但只放行白名单内的更可控）。
"""

from __future__ import annotations

import re

DOC_STYLES = [
    "开心", "悲伤", "愤怒", "恐惧", "惊讶", "兴奋", "委屈", "平静", "冷漠",
    "怅然", "欣慰", "无奈", "愧疚", "释然", "嫉妒", "厌倦", "忐忑", "动情",
    "温柔", "高冷", "活泼", "严肃", "慵懒", "俏皮", "深沉", "干练", "凌厉",
    "磁性", "醇厚", "清亮", "空灵", "稚嫩", "苍老", "甜美", "沙哑", "醇雅",
    "夹子音", "御姐音", "正太音", "大叔音", "台湾腔", "东北话", "四川话",
    "河南话", "粤语", "孙悟空", "林黛玉", "唱歌",
]

DOC_SFX = [
    "吸气", "深呼吸", "叹气", "长叹一口气", "喘息", "屏息",
    "紧张", "害怕", "激动", "疲惫", "委屈", "撒娇", "心虚", "震惊", "不耐烦",
    "颤抖", "声音颤抖", "变调", "破音", "鼻音", "气声", "沙哑",
    "笑", "轻笑", "大笑", "冷笑", "抽泣", "呜咽", "哽咽", "嚎啕大哭",
    "停顿", "沉默片刻",
]

_STYLE_MARK = re.compile(r"[\[［]\s*style\s*[:：]\s*([^\[\]［］]{1,40}?)\s*[\]］]", re.I)
_SFX_MARK = re.compile(r"[\[［]\s*sfx\s*[:：]\s*([^\[\]［］]{1,20}?)\s*[\]］]", re.I)
_ANY_BRACKET = re.compile(r"[\[［]([^\[\]［］]{1,40})[\]］]")
_LEAD_PAREN = re.compile(r"^\s*[（(]\s*([^（）()]{1,30})\s*[)）]\s*")
_SPLIT_TOKENS = re.compile(r"[\s,，、/／+＋]+")
_SENTINEL = re.compile(r"@@SFX(\d+)@@")
_WS2 = re.compile(r"\s{2,}")


def _tokens(value: str) -> list[str]:
    return [t for t in _SPLIT_TOKENS.split(value.strip()) if t]


def _match_list(items: list[str], allow: list[str] | None) -> list[str] | None:
    """全部命中白名单才返回；否则返回 None。allow 为空表示不限制。"""
    if allow:
        allowed = set(allow)
        if not all(item in allowed for item in items):
            return None
    return items


def apply_acting(
    text: str,
    styles: list[str] | None = None,
    sfx: list[str] | None = None,
) -> tuple[str, str]:
    """解析演技标记，返回 (payload, display)。"""
    if not text:
        return "", ""
    out = text
    style_tokens: list[str] = []
    sfx_tags: dict[int, str] = {}

    def _take_style(match: re.Match) -> str:
        items = _tokens(match.group(1))
        valid = _match_list(items, styles)
        if valid:
            style_tokens.extend(valid)
        # 白名单外：模型表达的仍是"控制"意图，静默丢弃，不要念出来
        return ""

    out = _STYLE_MARK.sub(_take_style, out)

    def _take_sfx(match: re.Match) -> str:
        items = _tokens(match.group(1))
        valid = _match_list(items, sfx)
        if not valid:
            return ""
        parts: list[str] = []
        for item in valid:
            index = len(sfx_tags)
            sfx_tags[index] = item
            parts.append(f"@@SFX{index}@@")
        return "".join(parts)

    out = _SFX_MARK.sub(_take_sfx, out)

    def _take_bracket(match: re.Match) -> str:
        inner = match.group(1).strip()
        if re.fullmatch(r"[A-Za-z0-9_:：\-\. ]+", inner):
            return ""
        return inner

    out = _ANY_BRACKET.sub(_take_bracket, out)

    # 行首括号：模型可能写内心独白/动作，MiMo 会当风格吞掉 → 拆括号照念
    lead = _LEAD_PAREN.match(out)
    if lead:
        content = lead.group(1).strip()
        items = _tokens(content)
        as_style = _match_list(items, styles)
        as_sfx = _match_list(items, sfx) if not as_style else None
        if as_style:
            style_tokens.extend(as_style)
            out = out[lead.end():]
        elif as_sfx:
            out = out[lead.end():]
            prefix = []
            for item in as_sfx:
                index = len(sfx_tags)
                sfx_tags[index] = item
                prefix.append(f"@@SFX{index}@@")
            out = "".join(prefix) + out
        else:
            out = content + out[lead.end():]

    def _restore(match: re.Match) -> str:
        return f"[{sfx_tags.get(int(match.group(1)), '')}]"

    restored = _SENTINEL.sub(_restore, out)
    display = _SENTINEL.sub("", out)
    display = _WS2.sub(" ", display).strip()
    restored = _WS2.sub(" ", restored).strip()

    style_prefix = ""
    if style_tokens:
        seen: set[str] = set()
        uniq = [t for t in style_tokens if not (t in seen or seen.add(t))]
        style_prefix = "（" + " ".join(uniq) + "）"
    payload = (style_prefix + restored).strip()
    return payload, display


def strip_acting_markers(
    text: str, styles: list[str] | None = None, sfx: list[str] | None = None
) -> str:
    """只用于聊天文字展示：去掉标记，保留台词。"""
    if not text:
        return text
    _, display = apply_acting(text, styles, sfx)
    return display
