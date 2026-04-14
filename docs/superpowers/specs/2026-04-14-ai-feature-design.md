# 飞书聊天导出 AI 功能扩展设计文档

## 概述

在 v0.3.0 基础上新增三类 AI 能力，将导出的 HTML 从静态记录变为可交互的 AI 助手：

1. **文字总结**（默认开启）：导出时预分析聊天内容，生成摘要
2. **图片理解**（可选）：AI 理解所有图片并建立索引，支持自然语言搜图
3. **智能问答**：基于聊天记录和图片理解的问答，支持 HTML 内嵌面板和 CLI 两种交互方式

**核心设计原则：理解结果持久化，避免重复 API 调用。**

---

## 整体架构

```
用户执行导出
     │
     ├─ fetch messages via lark-cli
     │
     ├─ download resources (images/audio/video/pdf)        [已有功能]
     │
     ├─ [新增] AI 文字摘要
     │        └─ results/ai_summary.json
     │
     ├─ [新增] AI 图片理解（可选）
     │        └─ results/ai_image_index.json
     │
     └─ generate HTML report
              ├─ 聊天记录（已有功能）
              ├─ 文字摘要（嵌入 HTML <header>）
              └─ AI 对话面板（内嵌 JS）

[可选] proxy.py          本地代理服务（API 认证）
[可选] ask.py            CLI 问答工具（读取已保存的理解结果）
```

---

## 新增文件

| 文件 | 用途 |
|------|------|
| `scripts/proxy.py` | 本地 HTTP 代理，保护 MiniMax API key |
| `scripts/ask.py` | CLI 问答工具 |
| `results/ai_summary.json` | AI 文字摘要结果 |
| `results/ai_image_index.json` | 图片理解索引 |

---

## 功能 1：AI 文字摘要

### 行为

导出时（`--ai-summary` flag，默认开启）调用 MiniMax 多模态模型，分析 messages.json，生成结构化摘要，写入 `results/ai_summary.json`，同时嵌入 HTML `<header>` 区域。

### 摘要内容

```json
{
  "chat_id": "oc_xxx",
  "generated_at": "2026-04-14T20:00:00+08:00",
  "summary": {
    "overview": "本群共2724条消息，主要讨论话题为...",
    "topics": [
      {"title": "话题1", "description": "...", "message_count": 123}
    ],
    "participants": [
      {"name": "张三", "message_count": 456, "contribution": "高频发言"}
    ],
    "key_moments": [
      {"time": "2026-04-13 15:00", "description": "讨论了..."}
    ]
  }
}
```

### HTML 嵌入位置

在 `<div class="header">` 下方追加 `<div class="ai-summary">`，折叠展示。点击展开显示完整摘要。

### API 调用

**端点**：`POST https://api.minimaxi.com/v1/text/chatcompletion_v2`

**认证**：`Authorization: Bearer ${MINIMAX_API_KEY}`（Token Plan key，格式 `sk-cp-xxx`，来自 `MINIMAX_API_KEY` 环境变量）。

**Base URL 来源**：参考同设备 `crayon-shinchan` 项目配置，Base URL 为 `https://api.minimaxi.com`（与 `platform.minimax.io` 等效）。

**模型**：`MiniMax-M2.7`（Token Plan 支持的模型，MiniMax-Text-01 不支持 Token Plan）

**消息分块策略**：
- MiniMax 单次请求有 token 上限
- 将 messages.json 按时间分段，每段不超过 200 条消息
- 摘要 prompt 包含"你是一个聊天记录分析助手"的角色设定
- 循环调用 API 聚合多段结果，最终合并为单一结构化摘要
- API 限速时：捕获 429 响应，指数退避重试（最多 3 次）

**Prompt 示例**：
```
你是一个聊天记录分析助手。请分析以下聊天记录，生成一份结构化摘要：
{
  "overview": "本群概况（不超过100字）",
  "topics": ["话题列表，最多5个"],
  "participants": ["参与者列表，最多10人"],
  "key_moments": ["关键时刻，最多5个"]
}
```

### 持久化

`results/ai_summary.json` 存在时，导出跳过 AI 摘要步骤（除非 `--force-ai`）。

**增量导出时的更新策略**：
- 增量导出（`--incremental`）时，messages.json 新增消息追加到末尾
- AI 摘要不自动重新生成（避免浪费 token），用户可手动 `--force-ai` 刷新
- `ai_summary.json` 的 `generated_at` 标注生成时间，用户可判断是否过期

---

## 功能 2：AI 图片理解

### 行为

用户传入 `--ai-images` flag 时，导出时对所有已下载的图片调用 MiniMax 多模态模型，生成每张图片的中文描述，写入 `results/ai_image_index.json`。

### 图片索引内容

```json
{
  "chat_id": "oc_xxx",
  "generated_at": "2026-04-14T20:00:00+08:00",
  "model": "MiniMax-M2.7 (vision via chat completions)",
  "images": [
    {
      "key": "img_v3_02mn_xxx",
      "filename": "img_v3_02mn_xxx.jpg",
      "message_id": "om_xxx",
      "message_time": "2026-04-13 15:23:00",
      "sender": "张三",
      "description": "一个女生坐在窗边弹吉他，阳光从窗户照进来",
      "tags": ["音乐", "室内", "弹吉他", "女生"]
    }
  ]
}
```

### API 调用

**端点**：`POST https://api.minimaxi.com/v1/text/chatcompletion_v2`

**认证**：同文字摘要。

**模型**：`MiniMax-M2.7`（支持 vision）

**调用方式**：通过 Chat Completions API 传图，content 中使用 `[Image base64:{img_base64}]` 语法。

```python
import base64, requests

with open(img_path, "rb") as f:
    img_b64 = base64.b64encode(f.read()).decode("utf-8")

resp = requests.post(
    "https://api.minimaxi.com/v1/chat/completions",
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    json={
        "model": "MiniMax-M2.7",
        "messages": [
            {"role": "user", "content": f'请描述这张图片，用中文回答并给出5个关键词标签。回答格式：\n描述：...\n标签：tag1,tag2,tag3,tag4,tag5\n[Image base64:{img_b64}]'}
        ]
    }
)
```

**批次处理**：
- 每张图片单独调用一次 API
- 每次生成描述 + 5 个标签
- 进度实时输出：`[3/1537] 处理中: img_v3_02mn_xxx.jpg`
- 限速处理（429 退避重试，最多 3 次）

### 持久化

`results/ai_image_index.json` 存在时，跳过图片理解步骤（除非 `--force-ai`）。

### HTML 中的展示

图片本身正常显示，鼠标悬停显示 AI 描述（`title` 属性）。

---

## 功能 3：智能问答

### 两种交互方式

#### 3A：HTML 内嵌对话面板

- HTML 底部追加 `<div class="ai-chat-panel">`
- 用户输入问题 → JS 发送到 `http://localhost:8765/ask`
- 代理转发到 MiniMax API → 返回结果显示在面板
- 支持搜索聊天文本和图片描述

**API 认证 + 安全说明**：本地代理 `proxy.py` 在 `localhost:8765` 运行，API key 只存在于代理进程内存中，不泄露到 HTML/JS。

**HTTPS mixed content 说明**：`file://` 协议打开 HTML 时，浏览器允许向 `http://localhost:8765` 发送请求（不受混合内容限制）。但若 HTML 通过 HTTPS 部署（如托管服务），则会触发混合内容阻止，需改为：
- 方案 A（推荐个人使用）：始终用 `file://` 打开 HTML，不部署到 HTTPS 环境
- 方案 B（需要 HTTPS 时）：proxy.py 启用自签名 HTTPS 证书（`ssl_context`），HTML 加载时需手动信任证书

#### 3B：CLI 工具 `ask.py`

```bash
python3 scripts/ask.py --question "那个弹吉他的照片是什么时候发的" --output ./results
```

直接读取 `results/ai_summary.json` 和 `results/ai_image_index.json`，无需网络请求（除非需要补充理解）。

### 代理服务 `proxy.py`

```
localhost:8765/ask
  POST, body: {"question": "...", "history": [...]}
  → 读取 MINIMAX_API_KEY from env
  → 调用 MiniMax Chat API
  → 返回 AI 回答

localhost:8765/health
  → 返回 {"status": "ok"}
```

启动：
```bash
python3 scripts/proxy.py
# 默认端口 8765
# API key 从环境变量 MINIMAX_API_KEY 读取
# MiniMax API Host: https://api.minimaxi.com（与 crayon-shinchan 项目一致）
```

### RAG 问答流程（CLI 工具 `ask.py`）

1. 接收用户问题
2. 从 `messages.json` 检索相关消息（关键词检索：python 内置 `re` 模块 + TF-IDF 加权）
3. 如果问题涉及图片，从 `ai_image_index.json` 按 `description` 和 `tags` 关键词匹配
4. 组装 context + 问题 → MiniMax Chat API（经 proxy.py）
5. 返回回答，并附上参考消息的 sender/time 和图片的 key/描述

**注意**：检索基于关键词匹配（无需额外 embedding 库），后续可升级为 embedding 向量检索。

### RAG 问答流程（HTML 面板）

1. 用户在 HTML 输入框提问
2. JS 将问题 POST 到 `http://localhost:8765/ask`，包含 `messages.json` 引用路径
3. proxy.py 读取 messages.json，执行同 CLI 的 RAG 检索流程
4. 调用 MiniMax Chat API，返回流式响应
5. JS 接收流式输出，实时显示在面板中

### Streaming 支持

proxy.py 的 `/ask` 端点支持 `text/event-stream` 流式响应，JS 使用 `fetch()` + `ReadableStream` 处理，无需等待完整响应。

---

## 命令行接口扩展

```bash
# 完整导出 + AI 摘要 + 图片理解
python3 scripts/export.py \
  --chat-id oc_xxx \
  --output ./results \
  --ai-summary           # 开启 AI 摘要（默认）
  --ai-images            # 开启图片理解
  --force-ai             # 强制重新生成 AI 结果（忽略缓存）
  --workers 16

# 只导出消息和资源（不调用 AI）
python3 scripts/export.py --chat-id oc_xxx --output ./results

# 启动本地代理
python3 scripts/proxy.py --port 8765

# CLI 问答
python3 scripts/ask.py \
  --question "那个弹吉他的女生是什么时候发的？" \
  --output ./results
```

---

## 数据流

```
messages.json
     │
     ├──→ export.py ──→ generate_html() ──→ report_with_images.html
     │                                    (含 AI 摘要 + 对话面板 JS)
     │
     ├──→ export.py + MiniMax API (直接调用) ──→ ai_summary.json
     │
     ├──→ export.py + MiniMax API (直接调用) ──→ ai_image_index.json
     │
     └──→ ask.py + MiniMax API (经 proxy.py) ──→ 用户回答
              ↑
              │
         (HTML 面板 JS → proxy.py)
```

---

## 依赖

| 依赖 | 用途 | 安装方式 |
|------|------|---------|
| `requests` | MiniMax API HTTP 调用 | pip |
| MiniMax API key | 认证 | 环境变量 `MINIMAX_API_KEY`（Token Plan key，格式 `sk-cp-xxx`，参考同设备 `crayon-shinchan` 项目配置） |
| lark-cli | 消息获取（已有） | 已有 |
| Python 内置 `re`, `json`, `http.server` | 关键词检索、RAG、本地代理 | Python 3.14 标准库 |

---

## 安全性

- `proxy.py` 运行在 `localhost`，不暴露到外网
- API key 只存在于 proxy.py 进程内存和系统环境变量（`MINIMAX_API_KEY`），不写入任何文件
- HTML 中的 JS 只和 `localhost:8765` 通信，不会泄露 key
- 聊天记录和理解结果全部存在本地，不经过第三方服务器
- 启动时检查 `MINIMAX_API_KEY` 环境变量，不存在则 fallback 到 crayon-shinchan/config.js
- API host 使用 `https://api.minimaxi.com`（与 `crayon-shinchan` 项目一致，Token Plan key 直接支持）

---

## 实施顺序

> **重要更新**：Token Plan key 可直接调用 MiniMax REST API（`https://api.minimaxi.com`），无需 MCP 或 Claude Code。`export.py` 可直接持有 key 调用 API，不依赖 proxy.py。

1. **第一阶段 [已完成]**：文字摘要（`export.py` 直调 API）+ `ai_summary.json`
   - `export.py` 直接持有 `MINIMAX_API_KEY` 环境变量，调用 `POST /v1/text/chatcompletion_v2`
   - 模型使用 `MiniMax-M2.7`（Token Plan 支持）
   - 生成 `results/ai_summary.json`，嵌入 HTML `<div class="ai-summary">`（折叠展示）
   - CLI 参数：`--ai-summary`（默认开启）、`--no-ai-summary`、`--force-ai`
   - 摘要生成后自动保存在 `ai_summary.json`，下次导出跳过（除非 `--force-ai`）

2. **第二阶段 [已完成]**：`proxy.py` + `ask.py` CLI 工具
   - proxy.py 持有 key，提供 `/ask` 流式端点（`text/event-stream`）
   - 端点：`GET /health`（健康检查）、`POST /ask`（流式问答）
   - ask.py CLI 工具：关键词检索 + MiniMax API（经 proxy.py）
   - 检索策略：Python 内置 `re` 模块，关键词匹配，加权排序，最多返回 10 条参考消息

3. **第三阶段 [已完成]**：图片理解（`--ai-images`）+ HTML 对话面板
   - `export.py` 直调 API，逐图理解，生成 `ai_image_index.json`
   - `--ai-images` CLI 参数开启，默认关闭（需要时手动启用）
   - 图片预处理：PIL 压缩至最大宽度 800px，JPEG 质量 85，base64 传入 API
   - HTML 面板：右下角固定悬浮球，点击展开后可对话（流式 SSE 输出）
   - 面板 JS 连接 `http://localhost:8765/ask`，解析 SSE 流式显示
   - `proxy.py` 修复：`get_api_key()` 返回 None 代替 `exit(1)`（避免杀死进程）；新增 `_load_key_from_config()` fallback 从 crayon-shinchan/config.js 读取 key
