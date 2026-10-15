# 与其他形态的关系

同一套"按需取字节"的方法，目前有两种落地形态。

## 两种形态

| 形态 | 位置 | 消费者 | 状态 |
|---|---|---|---|
| **WorkBuddy skill**（本仓库） | `~/.workbuddy/skills/read-video/` | WorkBuddy 自己 —— 在对话里直接跑 Python | 可用 |
| **dsh 插件** | [dsh-read-video](https://github.com/RaganaBloodedge/dsh-read-video) | dsh / DeepSeek Harness 里的模型 —— 调 `defineTool` 注册的工具 | 已发到 npm：`dsh-readvideo` |
| MCP server | —— | 任何 MCP 客户端 | 未实现 |

## 为什么是两套代码，而不是一套

- **调用入口不同**。skill 是给 agent 用的：它在对话里直接执行 Python 命令。
  dsh 插件是给 dsh 里的模型用的：模型调用注册好的工具。
  两种入口没法共用同一份胶水代码。
- **能共用的部分已经共用**：`references/methodology.md` 与 `scripts/rvcore.py`
  是这套东西的真相来源（source of truth）。dsh 插件那边的对应实现应以这里的结论为准。
- **语言不同**：skill 是 Python，dsh 插件是 TypeScript。
  **模型文件可以共用**（两边都用 SenseVoiceSmall，同一份 SHA256 清单），但代码不能。

## 能力对照

| 能力 | 本 skill | dsh 插件 |
|---|---|---|
| 平台解析（网页链接 → 直链） | ✅ bilibili 手写 + yt-dlp 通用 | ❌ |
| 定点取帧 | ✅ | ❌ |
| 音频截取 | ✅ | ❌ |
| 本地转写 | ✅ SenseVoice | ❌ |
| 字节计量（证明没下整片） | ✅ | ❌ |
| URL 可跳读性探测 | ✅ | ✅ `video_probe` |

## 同步改动

改了方法论层面的东西（新发现的坑、换了探测策略），两边都该跟：

1. 本仓库：`references/methodology.md` + `scripts/rvcore.py`
2. dsh 插件：其 `src/` 下的对应实现

改完各跑一次实测（用 `https://media.w3.org/2010/05/bunny/movie.mp4` 即可）确认字节账对得上。
