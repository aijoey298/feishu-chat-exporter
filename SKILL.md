---
name: 飞书聊天导出
description: This skill should be used when the user asks to "导出飞书聊天记录", "导出飞书群聊", "备份飞书聊天", "导出飞书聊天", "导出飞书聊天记录到HTML" or similar requests to export Feishu/Lark group chat or P2P chat messages with images and files to an HTML report.
version: 0.1.0
---

# 飞书聊天导出

将飞书群聊或 P2P 私聊消息导出为含内嵌多媒体文件的 HTML 报告，便于离线阅读和存档。

## 功能概述

此 skill 通过 `lark-cli` 调用飞书开放消息历史 API，获取指定会话的消息列表，自动下载聊天中的图片、文件和表情回应，最终渲染为独立的 `report.html` 文件（仿飞书样式，含内嵌多媒体）。所有资源以相对路径引用，无须网络连接即可浏览。

## 前置依赖检查

执行导出前，依次检查以下依赖是否就绪：

1. **lark-cli** — 飞书命令行工具，必须已安装并完成授权。运行 `lark-cli auth status` 验证。
2. **Python 3** — 导出脚本以 Python 编写，确保 `python3` 可用。
3. **jq** — JSON 解析工具，用于处理 lark-cli 返回的结构化数据。macOS 可通过 `brew install jq` 安装。

任意一项缺失时，先参考 `docs/SETUP.md` 完成安装和配置，再继续后续步骤。

## 使用流程

### 步骤 1：检查 lark-cli 授权状态

```bash
lark-cli auth status
```

确认输出包含 `Logged in as` 且显示正确的用户身份。如提示未登录或 Token 过期，执行 `lark-cli auth login` 重新授权。

### 步骤 2：获取会话 ID

**群聊 (Group Chat)：**

1. 在飞书客户端打开目标群聊
2. 点击右上角群设置图标 → 进入「群信息」页面
3. 向下滚动找到「群 ID」，复制 `oc_xxx` 格式的 ID

**P2P 私聊：**

1. 打开与对方的私聊窗口
2. 浏览器版飞书：在地址栏 URL 中找到 `open_chat_id` 参数（格式类似 `oc_xxxx`）
3. 移动端：长按对方头像 → 「更多信息」中查看用户 ID（`ou_xxx` 格式）

### 步骤 3：导出消息并生成报告

导出分为两个阶段：**数据拉取** 和 **报告生成**。脚本 `scripts/export.py` 负责第二阶段，接收 `messages.json`（由 lark-cli 导出的原始消息数据）并生成 HTML 报告。

首先通过 lark-cli 拉取消息历史：

```bash
lark-cli im +messages-export --chat-id <id> --output <dir>
```

将消息数据保存为 JSON 格式到指定目录。然后执行报告生成：

```bash
python3 scripts/export.py --chat-id <id> --output <dir> --workers 16
```

**参数说明：**

- `--chat-id <id>` — 群聊 ID（`oc_xxx`）
- `--user-id <id>` — P2P 用户 ID（`ou_xxx`），与 `--chat-id` 二选一
- `--output <dir>` — 输出目录（默认当前目录），包含 `messages.json`
- `--workers <n>` — 并发下载线程数（默认 16），根据网络条件调整

### 步骤 4：查看报告

导出完成后，在指定输出目录下生成以下文件：

- **`report.html`** — 主报告文件，包含完整消息时间线、内嵌图片和文件下载链接，采用仿飞书样式渲染
- **`resources/`** — 下载到本地的多媒体资源目录
  - `resources/images/` — 图片文件
  - `resources/files/` — 附件文件

报告路径会在脚本输出末尾显示。确认路径后，询问用户是否需要使用默认浏览器打开预览：

```
报告已生成：/path/to/report.html
是否打开浏览器预览？(y/n)
```

如用户确认，执行 `open /path/to/report.html`（macOS）或对应平台的浏览器打开命令。

## 输出说明

| 文件/目录 | 说明 |
|---|---|
| `report.html` | 主报告文件，仿飞书样式渲染，内嵌图片/音视频/PDF（按大小限制），其余文件提供下载链接 |
| `resources/images/` | 下载到本地的图片，HTML 中通过相对路径引用 |
| `resources/files/` | 下载到本地的附件文件（文档、音视频等） |

报告包含：消息数量统计、下载成功/失败计数、文件嵌入数量，以及表情回应 Reactions 信息。

## 引用资源

- **`scripts/export.py`** — 报告生成脚本：读取 messages.json（lark-cli 导出的原始消息数据），并发下载多媒体文件，渲染仿飞书样式的 HTML 报告
- **`docs/SETUP.md`** — lark-cli 安装、授权及环境配置详细指南；包含多平台安装命令、Device Code 授权流程和常见错误处理

## 常见问题

**Q: 提示"找不到 messages.json"**
→ 确保先通过 `lark-cli im +messages-export` 导出消息数据，再运行 export.py。

**Q: lark-cli 提示 Permission denied**
→ 参考 `docs/SETUP.md`，确保已申请 `im:chat:readonly`、`im:message:readonly` 等必要权限范围，且应用已发布或获得管理员审批。

**Q: 图片下载失败**
→ 部分图片可能因发送者设置了访问限制而无法下载，脚本会用占位符替代并在报告中记录失败项。

**Q: 如何提高下载速度？**
→ 使用 `--workers` 参数增加并发线程数，默认 16，网络条件好时可设为 32 或更高。
