# 飞书聊天导出

将飞书（Lark）聊天记录导出为带附件的 MHTML 本地报告，支持图片、音频、视频、PDF 等多媒体内容。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

## 特性

- **支持多种聊天类型**：群聊（Group Chat）和一对一私聊（P2P）
- **并行下载**：多线程并发拉取消息和附件，提升导出效率
- **内嵌多媒体**：自动下载并内嵌图片、音频、视频、PDF 等附件
- **MHTML 报告**：生成自包含的 `.html` 报告，可离线浏览
- **会话结构完整**：保留消息时间、发送者、回复关系等信息

## 前置要求

- [lark-cli](https://github.com/larksuite/lark-cli) — 飞书官方 CLI 工具，用于授权和 API 访问
- **Python 3.8+**
- **jq** — 用于解析 JSON 数据（macOS: `brew install jq`，Ubuntu/Debian: `sudo apt install jq`）

## 安装

```bash
# 克隆仓库
git clone <repository-url>
cd 飞书聊天记录导出

# 安装 Python 依赖（如有）
pip install -r requirements.txt
```

## 快速开始

### 1. 配置 lark-cli 授权

```bash
lark-cli auth login --recommend
```

浏览器打开授权页面，完成登录。

### 2. 获取 Chat ID

**方式一：从群设置页面获取**
- 在飞书客户端打开目标群聊 → 点击右上角「...」→「设置」→「群信息」
- 页面中可以看到 **群 ID（Chat ID）**

**方式二：通过机器人私聊获取**
- 与任意已添加的机器人开启私聊
- 机器人返回的 JSON 消息中包含 `chat_id` 字段

### 3. 运行导出

```bash
python3 scripts/export.py --chat-id <your-chat-id>
```

## 详细使用方法

```bash
python3 scripts/export.py [选项]

# 必选参数
--chat-id <id>        目标聊天会话的 ID

# 可选参数
--output <dir>        输出目录，默认 ./output
--parallel <n>        并行下载线程数，默认 4
--limit <n>           最大消息数量（调试用）
```

**示例：**

```bash
# 导出指定群聊
python3 scripts/export.py --chat-id oc_xxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 指定输出目录和并行数
python3 scripts/export.py --chat-id oc_xxxxxxxxxxxxxxxxxxxxxxxxxxxx --output ./my_export --parallel 8
```

## 输出说明

导出完成后，输出目录包含：

| 文件/目录 | 说明 |
|---|---|
| `report_with_images.html` | 主报告文件，可在浏览器中离线查看 |
| `images/` | 下载的图片、音频、视频、PDF 等附件 |

## License

MIT License — 详见 [LICENSE](LICENSE) 文件。
