# 语音接管 · `astrbot_plugin_voice_reply`

> AstrBot 插件 · 版本 `1.7.1` · 作者 AcceleratorYue · MIT License  
> 仓库：<https://github.com/AcceleratorYue/astrbot_plugin_voice_reply>  
> 兼容：AstrBot `>=4.29.1,<5`，平台 `aiocqhttp`


关键词触发的整条语音回复。**不受 AstrBot 内置 TTS 的概率控制，也不受分段回复影响**：
命中关键词时，整条 LLM 回复一次合成为一条语音直接发送；超长回复按配置只发文字。

## 安装

### 插件市场

在 AstrBot WebUI →「插件」→「插件市场」搜索 **语音接管**。

### GitHub

```bash
cd <AstrBot根目录>/data/plugins
git clone https://github.com/AcceleratorYue/astrbot_plugin_voice_reply
```

然后在 AstrBot WebUI →「插件管理」重载插件，或重启 AstrBot。

## 原理

AstrBot 内置 TTS 在 `ResultDecorateStage` 里触发，位置在分段回复之后、只处理 `Plain` 段，
并且受 `provider_tts_settings.trigger_probability` 概率控制。

本插件改用更早的 `on_decorating_result` 钩子（`priority=500000`）：

1. 拿到整条回复的纯文本（尚未被分段）；
2. 命中关键词/标签后，调用 MiMo TTS 合成**一整段**音频；
3. 用 `Record` 替换消息链里的所有 `Plain` 组件（保留 At/引用/图片等非文本组件）。

替换后链里没有 `Plain`，于是分段回复、内置 TTS、文本转图片全部不会介入，
一次触发 = 一条语音消息。

## 触发方式

默认 `keyword` 模式，两种命中途径（`trigger_on=both`）：

| 来源 | 条件 |
|---|---|
| 用户消息 | 包含 `user_keywords` 里的任意词（默认 `语音`） |
| 模型回复 | 包含 `reply_tags` 里的任意标签（默认 `（语音）`/`[语音]`/`【语音】`） |

回复里的标签会被自动剥离，既不会被念出来，也不会原样发给用户。
也可以把 `trigger_mode` 改成 `always`，让所有 LLM 回复都发语音。

## 配置

在 AstrBot WebUI → 插件 → 语音接管里配置，重要项：

- `api_key`：小米 MiMo 开放平台 API Key（`sk-...`，[控制台](https://platform.xiaomimimo.com/console/api-keys)）
- `voice_mode`：`clone` = 用参考音频克隆；`preset` = MiMo 预置音色
- `reference_audio`：克隆参考音频路径（wav/mp3，建议 5~15 秒单人干声，≤10MB）
  - 推荐使用 24kHz 单声道样本以降低上传量、加快合成；留空则使用预置音色/回退模式
- `max_chars`：超过这个字数只发文字（默认 200，约 45 秒语音）
- `whitelist` / `blacklist`：按 `unified_msg_origin` 或其中一部分（如群号）过滤会话

改完配置如果没生效，发 `/语音重载`（管理员）或重载插件。

## 命令

| 命令 | 说明 |
|---|---|
| `/语音` | 查看触发规则、音色、Key、缓存和统计 |
| `/语音试听 你好呀` | 直接合成一条语音，用于调音色/验证链路 |
| `/语音重载` | 重新读取配置与参考音频（管理员） |

## 数据

`data/plugin_data/astrbot_plugin_voice_reply/`

- `cache/`：相同文本的音频缓存（LRU，上限 `cache_max_files`）
- `tmp/`：未启用缓存时的临时音频，超过 2 小时自动清理

## 依赖与费用

- 只依赖 `httpx`（AstrBot 自带），无 GPU、无 ffmpeg
- MiMo TTS 三个模型目前**限时免费**，限制 100 RPM / 10M TPM；免费策略随时可能调整
- 克隆音色每次请求都会重传参考音频，文本越长合成越慢；建议长回复用 `max_chars` 兜底
- 仅供本人或已获授权的声音使用；请勿用于冒充、诈骗或未经同意的声音复制

## 常见问题

### 触发成功但只发文字，日志里是 `HTTP 429: Too many requests`

先看是哪个模型被限流：

- `mimo-v2.5-tts` / `mimo-v2.5-tts-voicedesign` 正常、只有 `mimo-v2.5-tts-voiceclone` 返回 429：
  说明克隆模型被平台侧限制（账号权限/集群放量/合规），和插件请求无关。
  这时把 `fallback_mode` 设成 `voicedesign` 或 `preset`，就能先用替代音色出声；
  等克隆权限恢复后再把 `fallback_mode` 改回 `none`，插件会自动优先用 `voicedesign`/`preset` 之外的
  `voice_mode`（默认 clone）。
- 三个模型都 429：账号整体被限流，等一会儿或检查控制台额度。

### 语音没有出现，也没有任何日志

- 确认插件已加载（插件管理里能看到「语音接管」）
- 确认全局 `provider_settings.streaming_response` 是 false（流式回复不接管）
- 确认消息确实触发了关键词（默认「语音」），且机器人被 @ 或带唤醒前缀

## 让角色知道自己能发语音

AstrBot 保存对话历史时只保存 LLM 输出的**文字**，语音形式不会进入上下文；记忆插件（livingmemory）
记录的也是 `completion_text`。所以角色默认不知道自己"刚才用语音说了话"，容易说"我只能打字"。

v1.2.0 起插件在每轮 LLM 请求时向 **system_prompt** 追加一段能力说明（不写入历史）：
告诉角色"你可以发语音、触发条件是什么、标记会被去掉"。文案可在插件配置的
`capability_prompt` 里改写，不想用就把 `inject_capability_prompt` 关掉。

想让角色更主动地用语音，可以把 `capability_prompt` 末尾加一句：
「遇到想撒娇或想强调语气的回复，你可以在开头加（语音）主动改用语音。」

### 动态状态说明（v1.3.0）

插件按会话记录"最近一条回复是否以语音发出"，在下一轮 LLM 请求的 system_prompt 里追加一句：

> 补充：你上一条回复是语音发出的（刚刚），用户听到的是你的声音；你能看到的对话记录里只有文字。

- 只注入提示词，不写历史、不占记忆；
- 之后如果又发了文字回复，这个状态会被覆盖，不会一直提示；
- `last_voice_note_window_min`（默认 30 分钟）控制有效期，超时不再提；
- `last_voice_note` 可改文案，支持 `{ago}`（刚刚 / N 分钟前）和 `{chars}` 占位符；
- `inject_last_voice_note` 可单独关闭。

## 语音转文字（v1.4.0）

收到语音消息时，插件用 MiMo-V2.5-ASR 把音频转成文字，再交给模型：

1. 音频由 AstrBot 预处理阶段归一化成 wav（QQ 的 SILK 也已在核心层解码，不需要 ffmpeg）；
2. 插件把它以 DataURL 形式发给 `mimo-v2.5-asr`（OpenAI Chat Completions 兼容的 `input_audio`）；
3. 转写结果会同时写入本轮 `req.prompt`、`event.message_str`，并把消息链里的语音换成文字，
   所以后续的记忆插件、关键词触发（包括本插件的语音回复）都能看到它。

配置项：

| 键 | 默认 | 说明 |
|---|---|---|
| `stt_enabled` | true | 总开关 |
| `stt_model` | `mimo-v2.5-asr` | 识别模型 |
| `stt_language` | `auto` | `auto` / `zh` / `en` |
| `stt_message_prefix` | 空 | 转写前缀，如填「（语音）」让模型知道这是语音输入 |

注意：

- **ASR 按音频时长计费**（¥0.5/小时，非免费），限制 100 RPM / 10K TPM；
- AstrBot 内置 STT（`provider_stt_settings.enable`）开启时，插件会自动跳过转写，避免重复；
- 引用消息里的语音也会转写（与核心行为一致）。

### v1.5.0：语音输入转码不再依赖系统 ffmpeg

QQ/NapCat 的语音有两个来源：本地缓存（实际是 SILK）和 multimedia URL（AMR）。
AstrBot 核心会优先用 URL 下载 AMR，再调用系统 `ffmpeg`；本机没有 ffmpeg 时转写必然失败。

插件改为：

1. 优先用 Record 的本地 `path`/`file`，按魔数识别格式（不看扩展名）：
   - SILK → 用 `pysilk` 直接解码（AstrBot 自带 `tencent_silk_to_wav`）；
   - AMR/MP3 等 → 用 AstrBot 环境里的 `imageio-ffmpeg` 二进制转码；
2. 只有本地文件缺失时才回退到 AstrBot 的下载/解析逻辑。

另外，为让 AstrBot 核心的语音预处理（引用语音、内置 STT）也恢复正常，已把
`imageio-ffmpeg` 自带的 ffmpeg 复制为 `%APPDATA%/npm/ffmpeg.exe`
（该目录本来就在用户 PATH 里，无需改环境变量）。删除这个文件即可撤销。

### v1.5.1：控制标签不再被念出来

角色回复里可能带 `[quote]`、`[at:某人]`（smart_reply_tag）或 `（语音）`（本插件触发标签）。
旧版里这些标记会漏进 TTS 文本：MiMo 会把开头的 `(...)` 当成风格标签，生成气声/笑声等"无意义杂音"；
`[quote]` 之类也可能被念出来。

修复：

1. 钩子优先级从 500000 调到 99000，排在 smart_reply_tag（100000）之后，标签已被剥离、Reply/At 已插入；
2. TTS 文本改用"剥离标签后的消息链"重新提取，而不是剥离前的原文；
3. `clean_for_tts` 增加兜底过滤：`[quote]`、`[at:...]`、`[plain]`、`[voice]`、`（语音）`、`【语音】`。

### v1.6.0：模型自主控制语气（演技标记）

MiMo TTS 的语法是：行首 `(风格)` 控制情绪/语调，任意位置 `[标签]` 插入笑声/叹气/停顿，
两者都不会被念出来；但**行首普通括号也会被当成风格吞掉**（内容丢失），这是"怪声/丢字"的来源。

本插件给模型一套安全标记，由插件翻译成 MiMo 原生语法：

| 模型写 | 插件翻译成 | 作用 |
|---|---|---|
| `[style:温柔]` / `[style:慵懒 俏皮]` | 行首 `（温柔）` | 情绪、语调、方言 |
| `[sfx:轻笑]` / `[sfx:叹气]` / `[sfx:停顿]` | 句中 `[轻笑]` …… | 语气音 |

规则：

- 只翻译白名单内的词（默认用 MiMo 文档推荐词表，可在 `acting_style_list` / `acting_sfx_list` 覆盖）；
- 白名单外的 `[style:x]` / `[sfx:x]` 静默丢弃，不会被念出来；
- 普通行首括号（如「（其实我也不知道）」）拆掉括号照念，**不再丢字**；
- 所有标记都不写进聊天文字，也不参与长度统计；
- 通过系统提示词把语法和词表告诉模型（`acting_prompt`，支持 `{styles}` / `{sfx}` 占位符）。

实测：`（温柔）你好呀[轻笑]，今天过得怎么样。` 合成正常，ASR 复听为「你好呀，今天过得怎么样？」——标签没有被念出来。

### v1.7.0：接管定时任务 / 主动消息

AstrBot 的定时任务（cron）和 `send_message_to_user` 工具**不经过发送前钩子**，而是直接调用
`Context.send_message`，所以 v1.6 之前 on_decorating_result 看不到它们，消息里的 `（语音）`
标记会原样发出去、也不会转语音。

v1.7.0 在插件加载时给 `Context.send_message` 包一层：

- 只处理**纯文本**主动消息（含图片/文件的不动）；
- 文本里带 `（语音）` 等触发标签时 → 合成整条语音发出，标签剥掉；
- 不带标签时原样发送（但会剥掉/清理演技标记，避免标记泄漏）；
- 合成失败/超长 → 退回文字（已清理标记）；
- 定时任务和主动消息也会更新"上一条回复是语音"的状态。

配置项：

| 键 | 默认 | 说明 |
|---|---|---|
| `proactive_voice_enabled` | true | 是否接管主动消息 |
| `proactive_always_voice` | false | 不开时只转换带触发标签的主动消息；开启后所有主动纯文本都转语音 |

注意：插件重载/停用时会自动恢复 `Context.send_message`，不会重复包装。

### v1.7.1：定时任务"只写回复不调用工具"的兜底

AstrBot 的 cron 任务依靠模型自己调用 `send_message_to_user` 发消息；如果模型把内容写在
最终回复里而没有调用工具，cron 管理器只会把它写进历史，**用户什么也收不到**（任务仍显示
completed、没有报错）。2026-11-02 / 11-03 的 23:00 催睡任务就是这样丢的。

修复：

1. `cron_tool_prompt`（默认开）：给 cron agent 注入强约束——必须调用 `send_message_to_user`，
   内容放进工具参数，否则用户收不到；要语音就在参数文本最前面写「（语音）」。
2. `cron_fallback_enabled`（默认开）：在 `on_agent_done` 检查 cron 事件的 `_has_send_oper`，
   若整轮没有发送过消息，就把最终回复补发出去（经过本插件的语音接管，带标记会转语音），
   并用 `_voice_reply_cron_fallback_done` 防止重复补发。

## 许可证

MIT License，详见 [LICENSE](LICENSE)。
