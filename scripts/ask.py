#!/usr/bin/env python3
"""
飞书聊天记录问答工具 - 基于完整上下文的无检索式问答
用法: python3 scripts/ask.py --question "问题" --output ./results
"""

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None


def extract_keywords(question: str) -> list[str]:
    """
    从问题中提取 3-5 个关键词（中文分词用简单字符划分，提取 2-4 个字以上的词）
    """
    # 移除常见停用词
    stop_words = {"的", "了", "是", "在", "和", "与", "或", "有", "什么", "怎么",
                  "吗", "呢", "吧", "啊", "哦", "嗯", "我", "你", "他", "她", "它",
                  "我们", "你们", "他们", "她们", "这个", "那个", "一个", "哪些",
                  "哪些", "哪", "谁", "多少", "几", "为什么", "如何", "怎样",
                  "可以", "能", "会", "应该", "需要", "要", "到", "从", "被", "把",
                  "给", "对", "这", "那", "请", "能", "都", "也", "还", "很"}

    # 提取中文词（2-4个字符）
    chinese_words = re.findall(r'[\u4e00-\u9fff]{2,4}', question)

    # 过滤停用词
    keywords = [w for w in chinese_words if w not in stop_words]

    # 去重并限制数量
    seen = set()
    unique_keywords = []
    for kw in keywords:
        if kw not in seen:
            seen.add(kw)
            unique_keywords.append(kw)

    # 返回 3-5 个关键词
    return unique_keywords[:5]


def search_messages(messages: list, keywords: list) -> list[dict]:
    """
    用关键词在消息中搜索，按匹配次数加权，最多返回 10 条相关消息
    """
    if not keywords:
        return []

    scored = []
    for msg in messages:
        content = msg.get("content", "")
        score = 0
        for kw in keywords:
            if kw in content:
                score += 1
        if score > 0:
            scored.append((score, msg))

    # 按分数降序排序
    scored.sort(key=lambda x: -x[0])

    # 取前 10 条
    return [msg for _, msg in scored[:10]]


def format_context_messages(messages: list) -> str:
    """
    将消息格式化为 context 字符串
    """
    lines = []
    for msg in messages:
        sender = (msg.get("sender") or {}).get("name", "未知")
        ct = msg.get("create_time", "")[:16]
        content = msg.get("content", "")
        # 内容截断到 200 字
        if len(content) > 200:
            content = content[:200] + "..."
        lines.append(f"[{ct}] {sender}: {content}")
    return "\n".join(lines)


MAX_CONTEXT_CHARS = 400000  # MiniMax-M2.7 安全上下文字符上限（实测 400K 内稳定）


def build_full_context(question: str, messages: list, ai_summary: str = "",
                       ai_image_index: dict = None) -> str:
    """
    构建包含聊天记录的完整上下文字符串（带截断保护）。
    末尾包含 {question} 占位符，由 proxy.py 替换为实际问题。
    """
    # 格式化所有消息
    msg_lines = []
    for msg in messages:
        sender = (msg.get("sender") or {}).get("name", "未知")
        ct = msg.get("create_time", "")[:16]
        content = msg.get("content", "")
        msg_lines.append(f"[{ct}] {sender}: {content}")

    msg_text = "\n".join(msg_lines)

    # 如果消息文本过长，从头尾各取一半（保留近期和早期上下文）
    if len(msg_text) > MAX_CONTEXT_CHARS:
        half = MAX_CONTEXT_CHARS // 2
        omitted = len(msg_text) - MAX_CONTEXT_CHARS
        msg_text = (
            msg_text[:half]
            + f"\n\n[... 约 {omitted} 字符的消息已省略 ...]\n\n"
            + msg_text[-half:]
        )

    parts = [
        "你是一个飞书聊天记录问答助手。基于以下全部聊天记录回答用户问题。",
        "如果用户问到图片相关的问题，请结合图片描述回答。",
        "如果无法从聊天记录中找到答案，请如实告知。",
        "",
        "=== AI 摘要 ===",
        ai_summary if ai_summary else "（无）",
        "",
        "=== 聊天记录 ===",
        msg_text,
    ]

    if ai_image_index and ai_image_index.get("images"):
        parts.extend(["", "=== 图片索引 ==="])
        for img in ai_image_index["images"]:
            parts.append(f"[{img['message_time']}] {img['sender']} [图片]: {img['description']}")

    parts.extend(["", "=== 用户问题 ===", "{question}"])

    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description="飞书聊天记录问答工具")
    parser.add_argument("--question", dest="question", required=True,
                        help="要提问的问题（必填）")
    parser.add_argument("--output", dest="output", default="./results",
                        help="结果目录 (默认 ./results)")
    parser.add_argument("--proxy-url", dest="proxy_url", default="http://localhost:8765",
                        help="proxy.py 代理地址 (默认 http://localhost:8765)")
    args = parser.parse_args()

    output_dir = Path(args.output).resolve()
    messages_file = output_dir / "messages.json"
    ai_summary_file = output_dir / "ai_summary.json"

    # 检查 messages.json 是否存在
    if not messages_file.exists():
        print(f"错误: 找不到 messages.json，请先运行 export.py 导出聊天记录", file=sys.stderr)
        return 1

    # 读取 messages.json
    try:
        with open(messages_file, encoding="utf-8") as f:
            messages = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"错误: 读取 messages.json 失败: {e}", file=sys.stderr)
        return 1

    # 读取 ai_summary.json（如果存在）
    ai_summary = ""
    if ai_summary_file.exists():
        try:
            with open(ai_summary_file, encoding="utf-8") as f:
                summary_data = json.load(f)
                ai_summary = summary_data.get("content", "")
        except Exception:
            pass

    # 读取 ai_image_index.json（如果存在）
    ai_image_index = None
    ai_image_index_file = output_dir / "ai_image_index.json"
    if ai_image_index_file.exists():
        try:
            with open(ai_image_index_file, encoding="utf-8") as f:
                ai_image_index = json.load(f)
        except Exception:
            pass

    # 检查 requests 是否可用
    if requests is None:
        print("错误: requests 库未安装，无法连接到 proxy.py", file=sys.stderr)
        return 1

    # 构建完整上下文（包含全部消息）
    full_context = build_full_context(
        question=args.question,
        messages=messages,
        ai_summary=ai_summary,
        ai_image_index=ai_image_index
    )

    try:
        resp = requests.post(
            f"{args.proxy_url}/ask",
            json={
                "question": args.question,
                "history": [],
                "context": full_context
            },
            stream=True,
            timeout=120
        )
    except requests.exceptions.ConnectionError:
        print(f"错误: 无法连接到 proxy.py（{args.proxy_url}），请先启动 python3 scripts/proxy.py", file=sys.stderr)
        return 1
    except requests.exceptions.RequestException as e:
        print(f"错误: 请求失败: {e}", file=sys.stderr)
        return 1

    if resp.status_code != 200:
        print(f"错误: API 返回错误状态码 {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
        return 1

    # 流式读取响应（解析 SSE 格式）
    print("回答：", end="", flush=True)
    answer_parts = []
    for line in resp.iter_lines():
        if not line:
            continue
        text = line.decode("utf-8", errors="replace").strip()
        if not text.startswith("data: "):
            continue
        try:
            data = json.loads(text[6:])
            content = data.get("content", "")
            if content:
                print(content, end="", flush=True)
                answer_parts.append(content)
            if data.get("done"):
                break
        except json.JSONDecodeError:
            pass
    print()

    # 打印上下文信息
    print(f"\n上下文：共 {len(messages)} 条消息")
    if ai_summary:
        print(f"AI 摘要：{ai_summary[:100]}{'...' if len(ai_summary) > 100 else ''}")
    if ai_image_index and ai_image_index.get("images"):
        print(f"图片索引：{len(ai_image_index['images'])} 张图片已理解")

    return 0


if __name__ == "__main__":
    sys.exit(main())
