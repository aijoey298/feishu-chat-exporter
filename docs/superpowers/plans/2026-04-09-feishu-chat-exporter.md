# 飞书聊天导出 Skill 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 构建一个 Claude Code Skill "飞书聊天导出"，允许用户通过自然语言导出飞书群聊/P2P聊天记录为含内嵌多媒体的 HTML 报告。

**架构：**
- `scripts/export.py` — 核心 Python 脚本，调用 lark-cli 下载消息和附件，生成 HTML
- `SKILL.md` — Skill 入口，供 Claude Code 读取，提供工作流引导
- `README.md` — 用户安装使用文档
- `docs/SETUP.md` — lark-cli 依赖安装配置详细指南
- `LICENSE` — MIT 许可证

**技术栈：** Python 3, lark-cli, jq, HTML/CSS

---

## 任务分解

### 任务 1: 创建项目目录结构

**Files:**
- Create: `/Users/minlingzhi/openclaw/lume/workspace/飞书聊天记录导出/scripts/`
- Create: `/Users/minlingzhi/openclaw/lume/workspace/飞书聊天记录导出/docs/`

- [ ] 创建目录结构

```bash
mkdir -p "/Users/minlingzhi/openclaw/lume/workspace/飞书聊天记录导出/scripts"
mkdir -p "/Users/minlingzhi/openclaw/lume/workspace/飞书聊天记录导出/docs"
```

---

### 任务 2: 编写核心 export.py

**Files:**
- Create: `/Users/minlingzhi/openclaw/lume/workspace/飞书聊天记录导出/scripts/export.py`

**功能要求：**
- 并发下载图片（16 线程）
- 支持群聊和 P2P（通过 --chat-id / --user-id 参数）
- 文件嵌入策略：图片(无上限)、音频≤30MB、视频≤100MB、PDF≤50MB、其他仅下载链接
- 输出 HTML 报告（仿飞书样式）
- 统计信息：消息数、下载图片数、失败数

**核心逻辑（从之前的脚本改进）：**
```python
# 1. 解析参数（chat_id 或 user_id）
# 2. 获取消息列表（分页）
# 3. 提取所有图片/音频/视频/PDF 引用
# 4. 并发下载（排除已有文件）
# 5. 生成 HTML（替换 [Image: xxx] 为 <img>，音频/视频/ PDF 行内嵌入）
# 6. 输出统计
```

- [ ] 编写 export.py 完整代码

---

### 任务 3: 编写 SKILL.md

**Files:**
- Create: `/Users/minlingzhi/openclaw/lume/workspace/飞书聊天记录导出/SKILL.md`

**Frontmatter:**
```yaml
---
name: 飞书聊天导出
description: This skill should be used when the user asks to "导出飞书聊天记录", "导出飞书群聊", "备份飞书聊天", "导出飞书聊天" or similar requests to export Feishu/Lark group chat or P2P chat messages with images and files to an HTML report.
version: 0.1.0
---
```

**Body 需包含：**
1. 功能概述（3-4 句）
2. 前置依赖检查（lark-cli、Python 3、jq）
3. 使用流程（引导用户输入 chat_id）
4. 如何获取 chat_id / user_id
5. 输出说明
6. 引用 scripts/export.py
7. 引用 docs/SETUP.md（详细配置）

- [ ] 编写 SKILL.md

---

### 任务 4: 编写 README.md

**Files:**
- Create: `/Users/minlingzhi/openclaw/lume/workspace/飞书聊天记录导出/README.md`

**需包含：**
1. 特性介绍（bullet points）
2. 前置要求（lark-cli、Python 3、jq）
3. 安装步骤（克隆 + 依赖安装）
4. 快速开始（示例）
5. 使用方法（详细）
6. 输出说明
7. 获取 chat_id 方法（图示说明）
8. License

- [ ] 编写 README.md

---

### 任务 5: 编写 docs/SETUP.md

**Files:**
- Create: `/Users/minlingzhi/openclaw/lume/workspace/飞书聊天记录导出/docs/SETUP.md`

**需包含：**
1. lark-cli 安装（各种系统）
2. lark-cli 授权（device code flow 步骤）
3. 依赖验证命令
4. 常见问题（token 过期、权限不足等）
5. 刷新 token 方法

- [ ] 编写 docs/SETUP.md

---

### 任务 6: 添加 LICENSE 和项目初始化

**Files:**
- Create: `/Users/minlingzhi/openclaw/lume/workspace/飞书聊天记录导出/LICENSE`
- Create: `/Users/minlingzhi/openclaw/lume/workspace/飞书聊天记录导出/.gitignore`

- [ ] 添加 LICENSE (MIT)
- [ ] 添加 .gitignore

---

### 任务 7: 提交到 GitHub

**Files:**
- Modify: 创建 GitHub repo 并推送

**步骤：**
1. 在 GitHub 创建仓库 `feishu-chat-exporter`（公开 + MIT）
2. 本地初始化 git
3. 推送代码

- [ ] 创建 GitHub 仓库并推送

---

### 任务 8: 功能验证

**验证内容：**
1. 依赖检查脚本运行正常
2. export.py --help 正常
3. 用测试群 ID 运行完整导出
4. HTML 报告生成正确（图片行内显示）
5. SKILL.md 格式正确

- [ ] 运行 `python3 scripts/export.py --help`
- [ ] 运行 `python3 scripts/export.py --chat-id <test_id>` 验证完整流程
- [ ] 检查 HTML 报告中的图片嵌入

---

### 任务 9: 代码检查

**使用工具：**
- 使用 code-review 流程检查 export.py

- [ ] 代码检查 export.py
- [ ] 检查 SKILL.md 格式和描述质量
