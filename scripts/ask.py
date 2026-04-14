#!/usr/bin/env python3
"""
飞书聊天记录问答工具 - 基于关键词检索的 RAG 问答
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


def build_prompt(question: str, context: str, ai_summary: str = "") -> str:
    """
    组装完整的 prompt
    """
    system = "你是一个聊天记录问答助手，根据提供的聊天记录回答用户问题。"
    if ai_summary:
        system += f"\n\n以下是 AI 生成的聊天摘要供参考：\n{ai_summary}"

    return f"""system: {system}
context: {context}
user: {question}"""


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

    # 提取关键词
    keywords = extract_keywords(args.question)
    if not keywords:
        print("警告: 未能从问题中提取关键词，将使用前 10 条消息作为上下文")
        context_messages = messages[:10]
    else:
        # 搜索相关消息
        context_messages = search_messages(messages, keywords)

    # 组装 context
    context = format_context_messages(context_messages)

    # 检查 requests 是否可用
    if requests is None:
        print("错误: requests 库未安装，无法连接到 proxy.py", file=sys.stderr)
        return 1

    # 调用 proxy.py
    prompt = build_prompt(args.question, context, ai_summary)

    try:
        resp = requests.post(
            f"{args.proxy_url}/ask",
            json={"question": args.question, "history": []},
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

    # 打印参考消息
    if context_messages:
        print("\n参考消息：")
        for msg in context_messages:
            sender = (msg.get("sender") or {}).get("name", "未知")
            ct = msg.get("create_time", "")[:16]
            content = msg.get("content", "")
            # 内容截断到 200 字
            if len(content) > 200:
                content = content[:200] + "..."
            print(f"[{ct}] {sender}: {content}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
