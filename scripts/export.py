#!/usr/bin/env python3
"""
飞书聊天记录导出 - 生成仿飞书样式HTML报告
- 支持群聊和P2P聊天
- 16线程并发下载附件
- 文件嵌入策略：图片无上限行内显示，音视频/PDF按大小限制嵌入，其他文件仅生成下载链接
- 增量导出：支持仅获取自上次导出后的新消息
"""

import json
import re
import subprocess
import time
import argparse
import shutil
import os
import base64
import xml.etree.ElementTree as ET
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime, timezone
from enum import Enum
from zoneinfo import ZoneInfo


# MiniMax API 配置
MINIMAX_API_HOST = os.environ.get("MINIMAX_API_HOST", "https://api.minimaxi.com")
MINIMAX_MODEL = "MiniMax-M2.7"
AI_SUMMARY_FILE = "ai_summary.json"


# 文件大小限制（字节）
SIZE_LIMITS = {
    "audio": 30 * 1024 * 1024,    # 30MB
    "video": 100 * 1024 * 1024,   # 100MB
    "pdf": 50 * 1024 * 1024,      # 50MB
}
SIZE_LIMITS["mp3"] = SIZE_LIMITS["audio"]
SIZE_LIMITS["m4a"] = SIZE_LIMITS["audio"]
SIZE_LIMITS["wav"] = SIZE_LIMITS["audio"]
SIZE_LIMITS["mp4"] = SIZE_LIMITS["video"]
SIZE_LIMITS["mov"] = SIZE_LIMITS["video"]

EMBEDDABLE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".mp3", ".m4a", ".wav", ".mp4", ".mov", ".pdf"}

# 增量导出常量
STATE_FILE = "last_export.json"
BACKUP_FILE = ".incremental_backup.json"
CURRENT_VERSION = 1
LOCAL_TZ = ZoneInfo("Asia/Shanghai")  # UTC+8, Feishu server time


@dataclass
class LastExportState:
    """增量导出的状态信息"""
    version: int = 1
    chat_id: str = ""
    exported_at: str = ""           # ISO 8601
    timezone: str = "Asia/Shanghai"
    last_message_time: str = ""      # YYYY-MM-DD HH:MM (from create_time)
    total_messages: int = 0
    message_ids: list[str] = field(default_factory=list)
    last_page_token: Optional[str] = None


@dataclass
class MergeResult:
    """合并结果统计"""
    added: int = 0
    updated: int = 0
    deleted: int = 0
    total: int = 0


class ExportMode(Enum):
    FULL = "full"
    INCREMENTAL = "incremental"


def local_time_to_iso8601(local_time_str: str) -> str:
    """Convert 'YYYY-MM-DD HH:MM' (UTC+8) to ISO 8601.
    Example: '2026-04-09 20:39' -> '2026-04-09T20:39:00+08:00'
    """
    if not local_time_str:
        return ""
    dt = datetime.strptime(local_time_str, "%Y-%m-%d %H:%M")
    dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.isoformat()


def load_state(state_file: Path) -> Optional[LastExportState]:
    """读取上次导出状态，返回 None 表示无效或不存在"""
    if not state_file.exists():
        return None
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return LastExportState(**data)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        print(f"警告: 状态文件无效 ({e})，将执行完整导出")
        if state_file.exists():
            state_file.rename(state_file.with_suffix(".bak"))
        return None


def save_state(state_file: Path, state: LastExportState) -> None:
    """保存导出状态"""
    state_file.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")


def save_checkpoint(output_dir: Path, page_token: str, page_num: int) -> None:
    """保存断点续传信息"""
    backup_file = output_dir / BACKUP_FILE
    data = {
        "created_at": datetime.now(LOCAL_TZ).isoformat(),
        "last_page_token": page_token,
        "last_successful_page": page_num
    }
    backup_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load_checkpoint(output_dir: Path) -> tuple[Optional[str], int]:
    """加载断点信息，返回 (page_token, page_num)"""
    backup_file = output_dir / BACKUP_FILE
    if not backup_file.exists():
        return None, 0
    try:
        data = json.loads(backup_file.read_text(encoding="utf-8"))
        return data.get("last_page_token"), data.get("last_successful_page", 0)
    except json.JSONDecodeError:
        return None, 0


def detect_export_mode(output_dir: Path, args) -> ExportMode:
    """自动检测导出模式"""
    messages_file = output_dir / "messages.json"
    state_file = output_dir / STATE_FILE

    if getattr(args, 'full', False):
        return ExportMode.FULL
    if getattr(args, 'incremental', False):
        return ExportMode.INCREMENTAL
    # Auto-detect: if state exists, use incremental
    if messages_file.exists() and state_file.exists():
        return ExportMode.INCREMENTAL
    return ExportMode.FULL


def merge_messages(existing: list, new: list) -> tuple[list, MergeResult]:
    """合并新旧消息，按 message_id 去重/更新/删除，按 create_time 排序"""
    emap = {msg["message_id"]: msg for msg in existing}
    result = MergeResult()

    for msg in new:
        msg_id = msg["message_id"]
        if msg.get("deleted"):
            if msg_id in emap:
                del emap[msg_id]
                result.deleted += 1
            continue
        if msg_id in emap:
            if msg.get("updated"):
                emap[msg_id] = msg
                result.updated += 1
        else:
            emap[msg_id] = msg
            result.added += 1

    merged = sorted(emap.values(), key=lambda m: m.get("create_time", ""))
    result.total = len(merged)
    return merged, result


def check_dependencies():
    """检查必要的依赖工具"""
    if not shutil.which("lark-cli"):
        raise RuntimeError("lark-cli 未找到，请先安装并配置飞书CLI工具")
    return True


def fetch_messages(chat_id: str, user_id: str, output_dir: Path, output_file: Path,
                  start_time: str | None = None) -> int:
    """通过lark-cli获取消息并保存到messages.json，返回消息总数"""
    is_p2p = bool(user_id)
    all_messages = []
    page_token = ""
    page_num = 0

    print("正在获取消息（分页中）...")
    while True:
        page_num += 1
        cmd = [
            "lark-cli", "im", "+chat-messages-list",
            "--sort", "asc",
            "--page-size", "50",
            "--format", "json",
        ]
        if is_p2p:
            cmd.extend(["--user-id", user_id])
        else:
            cmd.extend(["--chat-id", chat_id])

        if start_time:
            cmd.extend(["--start", start_time])

        if page_token:
            cmd.extend(["--page-token", page_token])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            print(f"获取消息失败（page {page_num}）: {result.stderr[:100]}")
            break

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            break

        msgs = data.get("data", {}).get("messages", [])
        has_more = data.get("data", {}).get("has_more", False)
        page_token = data.get("data", {}).get("page_token", "") or ""

        if not msgs:
            break

        all_messages.extend(msgs)
        print(f"  第 {page_num} 页: {len(msgs)} 条消息 (累计: {len(all_messages)})")

        if not has_more or not page_token:
            break

    if not all_messages:
        print("未获取到任何消息")
        return 0

    output_dir.mkdir(exist_ok=True, parents=True)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(all_messages, f, ensure_ascii=False, indent=2)

    print(f"消息获取完成: {len(all_messages)} 条，保存至 {output_file}")
    return len(all_messages)


def fetch_messages_incremental(
    chat_id: str,
    user_id: str | None,
    start_time: str,
    page_token: str | None,
    output_dir: Path
) -> list[dict]:
    """带断点续传的增量消息获取，返回消息列表（不写入文件）"""
    all_messages = []
    is_p2p = bool(user_id)
    current_token = page_token
    page_num = 0

    print(f"增量获取消息 (从 {start_time} 开始)...")

    while True:
        page_num += 1
        cmd = [
            "lark-cli", "im", "+chat-messages-list",
            "--sort", "asc",
            "--page-size", "50",
            "--format", "json",
            "--start", start_time
        ]
        if is_p2p:
            cmd.extend(["--user-id", user_id])
        else:
            cmd.extend(["--chat-id", chat_id])

        if current_token:
            cmd.extend(["--page-token", current_token])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            print(f"获取消息失败 (page {page_num}): {result.stderr[:100]}")
            break

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            break

        msgs = data.get("data", {}).get("messages", [])
        has_more = data.get("data", {}).get("has_more", False)
        page_token = data.get("data", {}).get("page_token", "") or ""

        if not msgs:
            break

        all_messages.extend(msgs)
        print(f"  第 {page_num} 页: {len(msgs)} 条 (累计: {len(all_messages)})")

        # 断点保存
        save_checkpoint(output_dir, page_token, page_num)

        if not has_more or not page_token:
            break
        current_token = page_token

    # 成功完成，删除断点文件
    (output_dir / BACKUP_FILE).unlink(missing_ok=True)
    return all_messages


def download_resource(msg_id: str, file_key: str, file_type: str, dest_path: Path, export_dir: Path) -> bool:
    """下载单个资源文件"""
    try:
        result = subprocess.run(
            [
                "lark-cli", "im", "+messages-resources-download",
                "--message-id", msg_id,
                "--file-key", file_key,
                "--type", file_type,
                "--output", str(dest_path.name),
                "--as", "user"
            ],
            capture_output=True, text=True, cwd=str(export_dir), timeout=120
        )
        if result.returncode == 0:
            tmp = export_dir / dest_path.name
            if tmp.exists() and tmp != dest_path:
                tmp.rename(dest_path)
            elif not dest_path.exists():
                for f in export_dir.glob(f"{file_key}.*"):
                    if not dest_path.exists() or f.stat().st_mtime > dest_path.stat().st_mtime:
                        f.rename(dest_path)
                        break
            return dest_path.exists()
        return False
    except Exception:
        return False


def escape_html(text: str) -> str:
    if not text:
        return ""
    return (text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;"))


def get_avatar(name: str) -> str:
    if not name or name in ("系统消息", ""):
        return "&#9881;"
    return name[0].upper()


def get_file_size_limit(ext: str) -> int | None:
    """返回文件大小限制（字节），None表示无限制"""
    return SIZE_LIMITS.get(ext.lower().lstrip("."))


def is_file_embeddable(path: Path) -> bool:
    """判断文件是否满足嵌入条件（图片无条件，其他按大小限制）"""
    if not path.exists():
        return False
    ext = path.suffix.lower()
    if ext in (".jpg", ".jpeg", ".png", ".gif"):
        return True  # 图片无上限
    limit = get_file_size_limit(ext)
    if limit is None:
        return False
    return path.stat().st_size <= limit


def resolve_file_type(file_key: str) -> str:
    """根据 file_key 前缀猜测资源类型"""
    if file_key.startswith("img_"):
        return "image"
    if file_key.startswith("audio_"):
        return "audio"
    if file_key.startswith("video_"):
        return "video"
    return "file"


def make_download_link(file_key: str, fname: str, subdir: Path, resource_map: dict) -> str:
    """为无法嵌入的文件生成下载链接"""
    path = resource_map.get(file_key)
    if path and path.exists():
        rel = f"{subdir.name}/{path.name}"
        size = path.stat().st_size
        size_str = _format_size(size)
        return f'<br><a class="file-link" href="{rel}" download="{escape_html(fname)}">&#128196; {escape_html(fname)} ({size_str})</a>'
    else:
        return f'<br><span class="file-missing">&#128196; {escape_html(fname)} (未下载)</span>'


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    elif size < 1024 * 1024:
        return f"{size/1024:.1f}KB"
    else:
        return f"{size/1024/1024:.1f}MB"


def format_content(content: str, resource_map: dict, subdir: Path) -> str:
    """替换消息内容中的图片引用为<img>标签，处理音视频/PDF嵌入，处理文件下载链接"""
    if not content:
        return ""

    # 建立 key → 正确相对路径 的映射（解决 subdir.name 丢失子目录的问题）
    _key_to_relpath = {}
    for key, path in resource_map.items():
        if path.exists():
            if "images" in path.parts:
                _key_to_relpath[key] = f"resources/images/{path.name}"
            else:
                _key_to_relpath[key] = f"resources/files/{path.name}"

    # 替换 [Image: img_v3_xxx]
    def replace_image(match):
        key = match.group(1)
        path = resource_map.get(key)
        if path and path.exists():
            ext = path.suffix.lower()
            rel = _key_to_relpath.get(key, f"resources/images/{path.name}")
            if ext in (".jpg", ".jpeg", ".png", ".gif"):
                return f'<br><img src="{rel}" alt="图片" loading="lazy"><br>'
            elif ext in (".mp3", ".m4a", ".wav"):
                return f'<br><audio controls src="{rel}"></audio><br>'
            elif ext in (".mp4", ".mov"):
                return f'<br><video controls src="{rel}"></video><br>'
            elif ext == ".pdf":
                return f'<br><object data="{rel}" type="application/pdf" width="100%" height="500"><a href="{rel}">下载PDF</a></object><br>'
            else:
                return make_download_link(key, path.name, subdir, resource_map)
        return f'<br><div class="loading-img">[图片未下载: {escape_html(key[:20])}...]</div><br>'

    content = re.sub(r'\[Image: (img_v3_[a-zA-Z0-9_-]+)\]', replace_image, content)

    # 处理 <file key="..." name="..."/> XML格式
    def replace_file_tag(match):
        xml_str = match.group(0)
        try:
            root = ET.fromstring(xml_str)
            file_key = root.get("key", "").strip()
            fname = root.get("name", file_key).strip()
        except ET.ParseError:
            file_key = re.search(r'key="([^"]+)"', xml_str)
            fname = re.search(r'name="([^"]+)"', xml_str)
            file_key = file_key.group(1).strip() if file_key else ""
            fname = fname.group(1).strip() if fname else file_key

        if not file_key:
            return ""

        path = resource_map.get(file_key)
        if path and path.exists():
            ext = path.suffix.lower()
            rel = _key_to_relpath.get(file_key, f"resources/files/{path.name}")
            if ext in (".mp3", ".m4a", ".wav"):
                return f'<br><audio controls src="{rel}"></audio>'
            elif ext in (".mp4", ".mov"):
                return f'<br><video controls src="{rel}"></video>'
            elif ext == ".pdf":
                return f'<br><object data="{rel}" type="application/pdf" width="100%" height="500"><a href="{rel}">下载PDF</a></object>'
            else:
                return make_download_link(file_key, fname, subdir, resource_map)
        else:
            return make_download_link(file_key, fname, subdir, resource_map)

    content = re.sub(r'<file[^>]+>', replace_file_tag, content)

    # 处理 <video ... cover_image_key="..." ...> 中的封面图
    def replace_video_cover(match):
        xml_str = match.group(0)
        cover_key = re.search(r'cover_image_key="(img_v3_[a-zA-Z0-9_-]+)"', xml_str)
        if not cover_key:
            return match.group(0)
        key = cover_key.group(1)
        path = resource_map.get(key)
        if path and path.exists():
            ext = path.suffix.lower()
            if ext in (".jpg", ".jpeg", ".png", ".gif"):
                rel = _key_to_relpath.get(key, f"resources/images/{path.name}")
                return f'<br><img src="{rel}" alt="视频封面" loading="lazy">'
        return ""

    content = re.sub(r'<video[^>]+>', replace_video_cover, content)

    # 链接
    content = re.sub(r'(https?://[^\s<]+)', r'<a href="\1" target="_blank">\1</a>', content)

    # @提及
    content = re.sub(r'@([^\s,，\uff0c\n]+)', r'<span class="mention">@\1</span>', content)

    return content


def message_to_html(msg: dict, resource_map: dict, subdir: Path) -> str:
    msg_type = msg.get("msg_type", "")
    sender = msg.get("sender") or {}
    sender_name = sender.get("name", "系统消息") if sender else "系统消息"
    create_time = msg.get("create_time", "")
    content = msg.get("content", "")

    is_system = (msg_type == "system")
    formatted_content = format_content(content, resource_map, subdir)
    css_class = "system" if is_system else ""
    avatar = get_avatar(sender_name)
    type_label = msg_type.upper() if msg_type else ""

    return f"""  <div class="message {css_class}">
    <div class="msg-header">
      <div class="msg-avatar">{avatar}</div>
      <div class="msg-info">
        <div class="msg-name">{escape_html(sender_name)}</div>
        <div class="msg-time">{escape_html(create_time)}</div>
      </div>
      <div class="msg-type">{type_label}</div>
    </div>
    <div class="msg-content">{formatted_content}</div>
  </div>"""


def generate_html(
    messages: list,
    resource_map: dict,
    subdir: Path,
    chat_id: str,
    chat_name: str,
    msg_count: int,
    downloaded_count: int,
    embedded_count: int,
    failed_count: int,
    ai_summary: Optional[dict] = None,
    ai_image_index: Optional[dict] = None,
    include_context: bool = False,
) -> str:
    # AI 摘要 HTML 片段
    ai_summary_html = ""
    if ai_summary and ai_summary.get("content"):
        content = escape_html(ai_summary["content"]).replace("\n", "<br>")
        generated_at = ai_summary.get("generated_at", "")[:19]
        model = ai_summary.get("model", "")
        ai_summary_html = f"""
<div class="ai-summary">
  <div class="ai-summary-header" onclick="toggleAiSummary()">
    <span class="ai-summary-title">&#x1F4AC; AI 摘要</span>
    <span class="ai-summary-meta">{generated_at} via {model}</span>
    <span class="ai-summary-toggle" id="aiSummaryToggle">&#9660; 展开</span>
  </div>
  <div class="ai-summary-content" id="aiSummaryContent" style="display:none">
    <div class="ai-summary-text">{content}</div>
  </div>
</div>"""

    # 构建完整上下文字符串（嵌入 HTML 供 AI 面板使用）
    context_str = ""
    if include_context:
        msg_lines = []
        for msg in messages:
            sender = (msg.get("sender") or {{}}).get("name", "未知")
            ct = msg.get("create_time", "")[:16]
            content = msg.get("content", "")
            msg_lines.append(f"[{ct}] {sender}: {content}")
        msg_text = "\n".join(msg_lines)

        parts = [
            "你是一个飞书聊天记录问答助手。基于以下全部聊天记录回答用户问题。",
            "如果用户问到图片相关的问题，请结合图片描述回答。",
            "如果无法从聊天记录中找到答案，请如实告知。",
            "",
            "=== AI 摘要 ===",
            (ai_summary.get("content", "") if ai_summary else "（无）"),
            "",
            "=== 聊天记录 ===",
            msg_text,
        ]
        if ai_image_index and ai_image_index.get("images"):
            parts.extend(["", "=== 图片索引 ==="])
            for img in ai_image_index["images"]:
                parts.append(f"[{img['message_time']}] {img['sender']} [图片]: {img['description']}")
        parts.extend(["", "=== 用户问题 ===", "{{question}}"])
        context_str = "\n".join(parts)

    HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{chat_name}</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif; background: #f5f5f5; }}
.header {{ background: linear-gradient(135deg, #fe6803 0%, #ff8533 100%); color: white; padding: 24px 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.15); position: sticky; top: 0; z-index: 100; }}
.header h1 {{ font-size: 22px; font-weight: 600; margin-bottom: 8px; }}
.header .meta {{ font-size: 13px; opacity: 0.9; }}
.stats {{ display: flex; gap: 16px; margin-top: 12px; font-size: 13px; flex-wrap: wrap; }}
.stats span {{ background: rgba(255,255,255,0.2); padding: 3px 10px; border-radius: 12px; }}
.container {{ max-width: 900px; margin: 20px auto; padding: 0 16px; }}
.message {{ background: white; border-radius: 12px; padding: 14px 16px; margin-bottom: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); transition: box-shadow 0.2s; }}
.message:hover {{ box-shadow: 0 2px 12px rgba(0,0,0,0.12); }}
.msg-header {{ display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }}
.msg-avatar {{ width: 40px; height: 40px; border-radius: 50%; background: linear-gradient(135deg, #fe6803, #ff8533); color: white; display: flex; align-items: center; justify-content: center; font-weight: 600; font-size: 15px; flex-shrink: 0; overflow: hidden; }}
.msg-info {{ flex: 1; min-width: 0; }}
.msg-name {{ font-weight: 600; color: #333; font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.msg-time {{ color: #999; font-size: 11px; margin-top: 2px; }}
.msg-type {{ font-size: 10px; color: #bbb; background: #f5f5f5; padding: 2px 6px; border-radius: 4px; flex-shrink: 0; }}
.msg-content {{ font-size: 14px; line-height: 1.75; color: #333; word-break: break-word; }}
.msg-content img {{ max-width: 100%; border-radius: 8px; margin: 10px 0; border: 1px solid #eee; display: block; cursor: pointer; transition: transform 0.2s; }}
.msg-content img:hover {{ transform: scale(1.02); box-shadow: 0 4px 20px rgba(0,0,0,0.15); }}
.msg-content audio {{ display: block; margin: 8px 0; width: 100%; }}
.msg-content video {{ display: block; margin: 8px 0; max-width: 100%; border-radius: 8px; }}
.msg-content object {{ border-radius: 8px; margin: 8px 0; }}
.msg-content a {{ color: #fe6803; text-decoration: none; }}
.msg-content a:hover {{ text-decoration: underline; }}
.system {{ background: #f8f8f8; border: 1px dashed #ddd; }}
.system .msg-content {{ color: #888; font-size: 13px; }}
.mention {{ color: #007aff; font-weight: 500; }}
.file-link {{ display: inline-flex; align-items: center; gap: 4px; background: #f0f4ff; padding: 6px 12px; border-radius: 6px; color: #0066cc; font-size: 13px; text-decoration: none; margin: 4px 0; }}
.file-link:hover {{ background: #e0e8ff; text-decoration: none; }}
.file-missing {{ color: #999; font-size: 13px; }}
.footer {{ text-align: center; color: #bbb; font-size: 12px; padding: 30px; }}
.loading-img {{ background: #f0f0f0; border-radius: 8px; padding: 40px; text-align: center; color: #999; font-size: 13px; }}
/* AI 摘要 */
.ai-summary {{ background: #fff8e6; border: 1px solid #ffe58a; border-radius: 12px; margin-bottom: 16px; overflow: hidden; }}
.ai-summary-header {{ display: flex; align-items: center; gap: 8px; padding: 12px 16px; cursor: pointer; user-select: none; }}
.ai-summary-header:hover {{ background: #fff3cc; }}
.ai-summary-title {{ font-weight: 600; color: #b37600; font-size: 14px; }}
.ai-summary-meta {{ font-size: 11px; color: #999; margin-left: auto; }}
.ai-summary-toggle {{ font-size: 12px; color: #b37600; }}
.ai-summary-content {{ border-top: 1px solid #ffe58a; padding: 12px 16px; }}
.ai-summary-text {{ font-size: 13px; line-height: 1.8; color: #555; }}
/* AI 对话面板 */
.ai-chat-panel {{ position: fixed; bottom: 20px; right: 20px; width: 380px; background: white; border-radius: 16px; box-shadow: 0 4px 24px rgba(0,0,0,0.15); overflow: hidden; z-index: 1000; font-size: 14px; }}
.chat-header {{ background: linear-gradient(135deg, #fe6803, #ff8533); color: white; padding: 12px 16px; cursor: pointer; display: flex; justify-content: space-between; align-items: center; user-select: none; }}
.chat-header:hover {{ background: linear-gradient(135deg, #e55a00, #e57500); }}
.chat-body {{ border-top: 1px solid #eee; }}
.chat-messages {{ height: 300px; overflow-y: auto; padding: 12px; background: #f9f9f9; }}
.chat-message {{ margin-bottom: 10px; line-height: 1.6; }}
.chat-message.user {{ color: #fe6803; font-weight: 600; }}
.chat-message.assistant {{ color: #333; }}
.chat-message.assistant:before {{ content: "AI: "; color: #999; }}
.chat-message.error {{ color: #e55a00; font-size: 12px; }}
.chat-empty {{ color: #999; text-align: center; padding-top: 80px; font-size: 13px; }}
.chat-input-area {{ display: flex; gap: 8px; padding: 10px; border-top: 1px solid #eee; }}
.chat-input-area input {{ flex: 1; padding: 8px 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 13px; outline: none; }}
.chat-input-area input:focus {{ border-color: #fe6803; }}
.chat-input-area button {{ padding: 8px 16px; background: #fe6803; color: white; border: none; border-radius: 8px; cursor: pointer; font-size: 13px; }}
.chat-input-area button:hover {{ background: #e55a00; }}
.chat-input-area button:disabled {{ background: #ccc; cursor: not-allowed; }}
</style>
<script>
function toggleAiSummary() {{
  var content = document.getElementById('aiSummaryContent');
  var toggle = document.getElementById('aiSummaryToggle');
  if (content.style.display === 'none') {{
    content.style.display = 'block';
    toggle.textContent = '▲ 收起';
  }} else {{
    content.style.display = 'none';
    toggle.textContent = '▼ 展开';
  }}
}}
</script>
</head>
<body>
<div class="header">
  <h1>{chat_name}</h1>
  <div class="meta">{msg_count} 条消息</div>
  <div class="stats">
    <span>&#10003; {downloaded_count} 下载成功</span>
    <span>&#10007; {failed_count} 下载失败</span>
    <span>&#128247; {embedded_count} 文件嵌入</span>
    <span>{export_time}</span>
  </div>
</div>
{ai_summary_html}
<div class="container">
{messages}
</div>
<div class="footer">
  由 Claude Code 自动生成
</div>
<!-- AI 对话面板 -->
<div class="ai-chat-panel" id="aiChatPanel">
  <div class="chat-header" onclick="toggleChatPanel()">
    <span>&#x1F4AC; AI 问答</span>
    <span class="chat-toggle" id="chatToggle">&#9660;</span>
  </div>
  <div class="chat-body" id="chatBody" style="display:none">
    <div class="chat-messages" id="chatMessages"></div>
    <div class="chat-input-area">
      <input type="text" id="chatInput" placeholder="输入问题，按 Enter 发送..." onkeydown="handleChatKeydown(event)" />
      <button onclick="sendChatMessage()">发送</button>
    </div>
  </div>
</div>
<script>
var __CHAT_CONTEXT__ = {json.dumps(context_str, ensure_ascii=False)};
function toggleChatPanel() {{
  var body = document.getElementById('chatBody');
  var toggle = document.getElementById('chatToggle');
  if (body.style.display === 'none') {{
    body.style.display = 'block';
    toggle.innerHTML = '&#9660;';
  }} else {{
    body.style.display = 'none';
    toggle.innerHTML = '&#9650;';
  }}
}}
function sendChatMessage() {{
  var input = document.getElementById('chatInput');
  var question = input.value.trim();
  if (!question) return;
  var messagesDiv = document.getElementById('chatMessages');
  var userDiv = document.createElement('div');
  userDiv.className = 'chat-message user';
  userDiv.textContent = question;
  messagesDiv.appendChild(userDiv);
  input.value = '';
  messagesDiv.scrollTop = messagesDiv.scrollHeight;
  var btn = input.nextElementSibling;
  btn.disabled = true;
  input.disabled = true;
  var assistantDiv = document.createElement('div');
  assistantDiv.className = 'chat-message assistant';
  assistantDiv.textContent = '';
  messagesDiv.appendChild(assistantDiv);
  fetch('http://localhost:8765/ask', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{question: question, history: [], context: window.__CHAT_CONTEXT__ || null}})
  }})
  .then(function(resp) {{
    var reader = resp.body.getReader();
    var decoder = new TextDecoder();
    function read() {{
      reader.read().then(function(result) {{
        if (result.done) {{
          btn.disabled = false;
          input.disabled = false;
          return;
        }}
        var text = decoder.decode(result.value);
        var lines = text.split('\\n');
        for (var i = 0; i < lines.length; i++) {{
          var line = lines[i].trim();
          if (line.startsWith('data: ')) {{
            try {{
              var data = JSON.parse(line.substring(6));
              if (data.content) {{
                assistantDiv.textContent += data.content;
                messagesDiv.scrollTop = messagesDiv.scrollHeight;
              }}
            }} catch (e) {{}}
          }}
        }}
        read();
      }});
    }}
    read();
  }})
  .catch(function(err) {{
    assistantDiv.className = 'chat-message error';
    assistantDiv.textContent = '错误: ' + err.message;
    btn.disabled = false;
    input.disabled = false;
  }});
}}
function handleChatKeydown(e) {{
  if (e.key === 'Enter') sendChatMessage();
}}
document.addEventListener('DOMContentLoaded', function() {{
  var footer = document.querySelector('.footer');
  if (footer) footer.style.paddingBottom = '20px';
}});
</script>
</body>
</html>"""
    messages_html = [message_to_html(msg, resource_map, subdir) for msg in messages]
    return HTML_TEMPLATE.format(
        chat_name=escape_html(chat_name),
        chat_id=escape_html(chat_id),
        msg_count=msg_count,
        downloaded_count=downloaded_count,
        failed_count=failed_count,
        embedded_count=embedded_count,
        export_time=time.strftime("%Y-%m-%d %H:%M:%S"),
        messages="\n".join(messages_html),
        ai_summary_html=ai_summary_html,
    )


def extract_resources(messages: list) -> tuple[dict, dict, dict]:
    """
    从消息中提取所有资源引用。
    返回: (image_refs, file_refs, media_refs)
    image_refs: key -> msg_id
    file_refs: key -> (msg_id, fname, ftype)
    media_refs: key -> msg_id
    """
    image_refs = {}
    file_refs = {}
    media_refs = {}

    for msg in messages:
        msg_id = msg.get("message_id", "")
        msg_type = msg.get("msg_type", "")
        content = msg.get("content", "")

        if msg_type == "image":
            keys = re.findall(r'img_v3_[a-zA-Z0-9_-]+', content)
            for key in keys:
                if key not in image_refs:
                    image_refs[key] = msg_id

        elif msg_type == "post":
            keys = re.findall(r'img_v3_[a-zA-Z0-9_-]+', content)
            for key in keys:
                if key not in image_refs:
                    image_refs[key] = msg_id
            # 处理 <file key="..." name="..."/> XML格式
            for m in re.finditer(r'<file\s+[^>]+>', content):
                xml_str = m.group(0)
                try:
                    root = ET.fromstring(xml_str)
                    file_key = root.get("key", "").strip()
                    fname = root.get("name", "").strip()
                except ET.ParseError:
                    k = re.search(r'key="([^"]+)"', xml_str)
                    n = re.search(r'name="([^"]+)"', xml_str)
                    file_key = k.group(1).strip() if k else ""
                    fname = n.group(1).strip() if n else file_key
                if file_key and file_key not in file_refs:
                    ftype = resolve_file_type(file_key)
                    file_refs[file_key] = (msg_id, fname or file_key, ftype)

        elif msg_type == "media":
            cover_match = re.search(r'cover_image_key="(img_v3_[a-zA-Z0-9_-]+)"', content)
            if cover_match:
                key = cover_match.group(1)
                if key not in image_refs:
                    image_refs[key] = msg_id
            for m in re.finditer(r'<video\s+[^>]+>', content):
                xml_str = m.group(0)
                video_key_match = re.search(r'key="(msg_file_[a-zA-Z0-9_-]+)"', xml_str)
                video_name_match = re.search(r'name="([^"]+)"', xml_str)
                if video_key_match:
                    vk = video_key_match.group(1).strip()
                    vn = video_name_match.group(1).strip() if video_name_match else vk
                    if vk not in media_refs:
                        media_refs[vk] = (msg_id, vn, "video")
                for k in re.findall(r'(msg_file_[a-zA-Z0-9_-]+)', xml_str):
                    if k not in media_refs:
                        media_refs[k] = (msg_id, k, "file")

        elif msg_type == "file":
            for m in re.finditer(r'<file\s+[^>]+>', content):
                xml_str = m.group(0)
                try:
                    root = ET.fromstring(xml_str)
                    file_key = root.get("key", "").strip()
                    fname = root.get("name", "").strip()
                except ET.ParseError:
                    k = re.search(r'key="([^"]+)"', xml_str)
                    n = re.search(r'name="([^"]+)"', xml_str)
                    file_key = k.group(1).strip() if k else ""
                    fname = n.group(1).strip() if n else file_key
                if file_key and file_key not in file_refs:
                    ftype = resolve_file_type(file_key)
                    file_refs[file_key] = (msg_id, fname or file_key, ftype)

    return image_refs, file_refs, media_refs


def build_existing_map(res_dir: Path) -> dict:
    """构建已下载文件映射: file_key -> Path"""
    mapping = {}
    for p in res_dir.rglob("*"):
        if p.is_file():
            key = p.stem
            if key not in mapping:
                mapping[key] = p
    return mapping


# ---------------------------------------------------------------------------
# AI 摘要功能
# ---------------------------------------------------------------------------

def _get_minimax_key() -> str:
    """从环境变量或 crayon-shinchan 项目读取 MiniMax API Key"""
    key = os.environ.get("MINIMAX_API_KEY", "")
    if key:
        return key
    # fallback: 从 crayon-shinchan config.js 读取
    cfgs = [
        Path.home() / "openclaw/lume/workspace/漫画生成/crayon-shinchan/config.js",
        Path.home() / "openclaw/lume/workspace/漫画生成/crayon-shinchan/config.js",
    ]
    for cfg_path in [
        Path.home() / "openclaw/lume/workspace/漫画生成/crayon-shinchan/config.js",
        Path(__file__).parent.parent / "漫画生成/crayon-shinchan/config.js",
    ]:
        try:
            if cfg_path.exists():
                text = cfg_path.read_text()
                m = re.search(r"apiKey:\s*'([^']+)'", text)
                if m:
                    return m.group(1)
        except Exception:
            pass
    return ""


def _call_minimax_chat(prompt: str, api_key: str, retry: int = 3) -> Optional[str]:
    """调用 MiniMax Chat API，返回文本内容。失败返回 None。"""
    try:
        import requests as _requests
    except ImportError:
        print("警告: requests 库未安装，AI 摘要功能不可用")
        return None

    for attempt in range(retry):
        try:
            resp = _requests.post(
                f"{MINIMAX_API_HOST}/v1/text/chatcompletion_v2",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": MINIMAX_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=60,
            )
            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices")
                if choices and choices[0].get("message", {}).get("content"):
                    return choices[0]["message"]["content"]
                br = data.get("base_resp", {})
                if br.get("status_code") != 0:
                    print(f"  MiniMax API 错误 ({br.get('status_code')}): {br.get('status_msg', '')}")
            elif resp.status_code == 529 or resp.status_code == 429:
                print(f"  MiniMax 服务过载（第 {attempt+1} 次重试）...")
                time.sleep(5 * (attempt + 1))
                continue
            else:
                print(f"  MiniMax API 错误 {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"  MiniMax 请求异常: {e}")
    return None


def generate_ai_summary(messages: list, output_dir: Path, force: bool = False) -> Optional[dict]:
    """
    为聊天记录生成 AI 摘要。
    成功返回摘要 dict，失败返回 None。
    """
    summary_file = output_dir / AI_SUMMARY_FILE

    # 读取已有摘要
    if summary_file.exists() and not force:
        try:
            data = json.loads(summary_file.read_text(encoding="utf-8"))
            print(f"  已存在 AI 摘要（{summary_file}），跳过生成（用 --force-ai 强制重新生成）")
            return data
        except Exception:
            pass

    api_key = _get_minimax_key()
    if not api_key:
        print("警告: 未找到 MiniMax API Key，跳过 AI 摘要")
        return None

    # 提取消息文本
    total = len(messages)
    print(f"开始生成 AI 摘要（{total} 条消息）...")

    # 分块：每块最多 300 条消息，避免超出 context window
    chunks = []
    for i in range(0, total, 300):
        chunk_msgs = messages[i : i + 300]
        texts = []
        for msg in chunk_msgs:
            sender = (msg.get("sender") or {}).get("name", "未知")
            ct = msg.get("create_time", "")[:16]
            content = msg.get("content", "")[:500]
            if content:
                texts.append(f"[{ct}] {sender}: {content}")
        chunks.append("\n".join(texts))

    # 逐块生成摘要，再合并
    chunk_summaries = []
    system_prompt = (
        "你是一个聊天记录分析助手。请分析用户提供的聊天记录片段，生成一段结构化摘要。"
        "回复格式为纯文本，包含：\n"
        "1. 参与者列表（最多5人）和发言数量\n"
        "2. 主要话题（最多3个）\n"
        "3. 核心内容概述（50字以内）\n"
        "不要添加额外说明，直接输出摘要内容。"
    )

    for idx, chunk_text in enumerate(chunks):
        print(f"  处理第 {idx+1}/{len(chunks)} 个片段...")
        prompt = f"{system_prompt}\n\n---聊天记录片段---\n{chunk_text[:3000]}"
        result = _call_minimax_chat(prompt, api_key)
        if result:
            chunk_summaries.append(result.strip())
        time.sleep(1)

    if not chunk_summaries:
        print("警告: 所有片段摘要均失败，跳过 AI 摘要")
        return None

    # 最终聚合摘要（如果有多块）
    if len(chunk_summaries) == 1:
        final = chunk_summaries[0]
    else:
        prefix = "下面是同一个聊天记录的多段摘要，请合并为一份简洁的最终摘要，包含：参与者列表、主要话题、核心概述（100字以内）。\n\n"
        join_str = "\n---\n".join(f"[片段{i+1}]\n{s}" for i, s in enumerate(chunk_summaries))
        merge_prompt = prefix + join_str
        final = _call_minimax_chat(merge_prompt, api_key) or "\n".join(chunk_summaries)

    summary_data = {
        "chat_id": (messages[0].get("chat_id", "") if messages else ""),
        "generated_at": datetime.now().isoformat(),
        "model": MINIMAX_MODEL,
        "total_messages": total,
        "chunks": len(chunks),
        "content": final,
    }

    summary_file.write_text(json.dumps(summary_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  AI 摘要已保存: {summary_file}")
    return summary_data


def _load_key_from_config():
    """从 crayon-shinchan config.js 读取 API Key"""
    import re
    from pathlib import Path
    for cfg_path in [
        Path.home() / "openclaw/lume/workspace/漫画生成/crayon-shinchan/config.js",
    ]:
        try:
            if cfg_path.exists():
                text = cfg_path.read_text()
                m = re.search(r"apiKey:\s*'([^']+)'", text)
                if m:
                    return m.group(1)
        except Exception:
            pass
    return ""


def _get_minimax_key() -> str:
    """从环境变量或 crayon-shinchan 项目读取 MiniMax API Key"""
    key = os.environ.get("MINIMAX_API_KEY", "")
    if key:
        return key
    return _load_key_from_config()


def _compress_image_for_api(img_path: Path, max_width=800) -> Optional[str]:
    """压缩图片并返回 base64 编码，用于 API 调用"""
    import io
    try:
        from PIL import Image
    except ImportError:
        print("警告: PIL 未安装，无法压缩图片，跳过该图片")
        return None
    try:
        with Image.open(img_path) as img:
            if img.width > max_width:
                ratio = max_width / img.width
                new_height = int(img.height * ratio)
                img = img.resize((max_width, new_height), Image.LANCZOS)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "JPEG", quality=85)
            return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        print(f"  图片压缩失败 {img_path.name}: {e}")
        return None


def generate_ai_image_index(messages: list, output_dir: Path, images_dir: Path, resource_map: dict, force: bool = False) -> Optional[dict]:
    """
    为所有已下载的图片生成 AI 理解索引。
    成功返回索引 dict，失败返回 None。
    """
    import requests as _requests

    index_file = output_dir / "ai_image_index.json"

    # 读取已有索引
    if index_file.exists() and not force:
        try:
            data = json.loads(index_file.read_text(encoding="utf-8"))
            print(f"  已存在图片索引（{index_file}），跳过生成（用 --force-ai 强制重新生成）")
            return data
        except Exception:
            pass

    api_key = _get_minimax_key()
    if not api_key:
        print("警告: 未找到 MiniMax API Key，跳过图片理解")
        return None

    # 收集所有图片文件
    image_files = []
    for p in images_dir.iterdir():
        if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif"):
            image_files.append(p)

    if not image_files:
        print("  未找到已下载的图片，跳过图片理解")
        return None

    print(f"开始生成图片理解索引（{len(image_files)} 张图片）...")

    # 建立 key → (message_id, sender, create_time) 的映射
    key_to_msg = {}
    for msg in messages:
        msg_id = msg.get("message_id", "")
        sender = (msg.get("sender") or {}).get("name", "未知")
        ct = msg.get("create_time", "")[:16]
        content = msg.get("content", "")
        for key in re.findall(r'img_v3_[a-zA-Z0-9_-]+', content):
            if key not in key_to_msg:
                key_to_msg[key] = (msg_id, sender, ct)

    results = []
    for idx, img_path in enumerate(image_files, 1):
        key = img_path.stem  # e.g. "img_v3_02mn_xxx"
        msg_info = key_to_msg.get(key, ("未知", "未知", ""))
        msg_id, sender, msg_time = msg_info

        print(f"  [{idx}/{len(image_files)}] 处理中: {img_path.name}...")

        img_b64 = _compress_image_for_api(img_path)
        if not img_b64:
            continue

        prompt = (
            f"请描述这张图片，用中文回答并给出5个关键词标签。\n"
            f"回答格式：\n描述：...\n标签：tag1,tag2,tag3,tag4,tag5\n"
            f"[Image base64:{img_b64}]"
        )

        for attempt in range(3):
            try:
                resp = _requests.post(
                    f"{MINIMAX_API_HOST}/v1/text/chatcompletion_v2",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"model": MINIMAX_MODEL, "messages": [{"role": "user", "content": prompt}]},
                    timeout=60,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    choices = data.get("choices")
                    if choices and choices[0].get("message", {}).get("content"):
                        text = choices[0]["message"]["content"]
                        # 解析描述和标签
                        desc_match = re.search(r"描述[：:]\s*(.+?)(?:\n|$)", text, re.DOTALL)
                        tags_match = re.search(r"标签[：:]\s*(.+?)(?:\n|$)", text, re.DOTALL)
                        description = desc_match.group(1).strip() if desc_match else text[:100]
                        tags_text = tags_match.group(1).strip() if tags_match else ""
                        tags = [t.strip() for t in re.split(r"[,，、\n]", tags_text) if t.strip()][:5]
                        results.append({
                            "key": key,
                            "filename": img_path.name,
                            "message_id": msg_id,
                            "message_time": msg_time,
                            "sender": sender,
                            "description": description,
                            "tags": tags,
                        })
                        print(f"    完成: {img_path.name} - 描述: {description[:50]}...")
                        break
                    br = data.get("base_resp", {})
                    if br.get("status_code") != 0:
                        print(f"  API 错误 ({br.get('status_code')}): {br.get('status_msg', '')}")
                elif resp.status_code in (429, 529):
                    print(f"  服务过载（第 {attempt+1} 次重试）...")
                    import time as _time
                    _time.sleep(5 * (attempt + 1))
                    continue
                else:
                    print(f"  API 错误 {resp.status_code}: {resp.text[:100]}")
            except Exception as e:
                print(f"  请求异常: {e}")

        import time as _time
        _time.sleep(1)  # 避免过快调用

    index_data = {
        "chat_id": (messages[0].get("chat_id", "") if messages else ""),
        "generated_at": datetime.now().isoformat(),
        "model": MINIMAX_MODEL,
        "images": results,
    }

    index_file.write_text(json.dumps(index_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  图片索引已保存: {index_file}（{len(results)} 张图片）")
    return index_data


def main():
    parser = argparse.ArgumentParser(description="飞书聊天记录导出工具")
    parser.add_argument("--chat-id", dest="chat_id", help="群聊ID (oc_xxx)")
    parser.add_argument("--user-id", dest="user_id", help="P2P用户ID (ou_xxx)")
    parser.add_argument("--output", dest="output", default=".", help="输出目录 (默认当前目录)")
    parser.add_argument("--workers", dest="workers", type=int, default=16, help="并发下载线程数 (默认16)")
    parser.add_argument("--fetch", dest="fetch", action="store_true", help="自动获取消息（无需手动准备messages.json）")
    # 增量导出参数
    parser.add_argument("--incremental", dest="incremental", action="store_true",
                        help="增量导出：仅获取自上次导出后的新消息")
    parser.add_argument("--full", dest="full", action="store_true",
                        help="强制完整导出（忽略已有状态）")
    parser.add_argument("--since", dest="since", type=str, default=None,
                        help="增量起始时间 (ISO 8601 或 'YYYY-MM-DD HH:MM', 默认使用上次导出时间)")
    parser.add_argument("--timezone", dest="timezone", default="Asia/Shanghai",
                        help="时区 (IANA格式, 默认 Asia/Shanghai)")
    # AI 功能参数
    parser.add_argument("--ai-summary", dest="ai_summary", action="store_true", default=True,
                        help="生成 AI 文字摘要（默认开启）")
    parser.add_argument("--no-ai-summary", dest="ai_summary", action="store_false",
                        help="关闭 AI 文字摘要")
    parser.add_argument("--force-ai", dest="force_ai", action="store_true",
                        help="强制重新生成 AI 结果（忽略缓存）")
    parser.add_argument("--ai-images", dest="ai_images", action="store_true",
                        help="开启 AI 图片理解")
    args = parser.parse_args()

    if not args.chat_id and not args.user_id:
        print("错误: 必须提供 --chat-id 或 --user-id")
        parser.print_help()
        return 1

    check_dependencies()

    output_dir = Path(args.output).resolve()
    chat_id = args.chat_id or args.user_id
    chat_name = f"飞书聊天记录_{chat_id}"
    res_dir = output_dir / "resources"
    images_dir = res_dir / "images"
    files_dir = res_dir / "files"

    output_dir.mkdir(exist_ok=True, parents=True)
    images_dir.mkdir(exist_ok=True, parents=True)
    files_dir.mkdir(exist_ok=True, parents=True)

    messages_file = output_dir / "messages.json"

    # 读取已有消息
    existing_messages = []
    if messages_file.exists():
        with open(messages_file, encoding="utf-8") as f:
            existing_messages = json.load(f)

    # 决定导出模式
    export_mode = detect_export_mode(output_dir, args)
    messages = []

    if args.fetch:
        if export_mode == ExportMode.INCREMENTAL:
            # 增量导出模式
            state = load_state(output_dir / STATE_FILE)

            # 如果是增量模式但缺少 state 文件，从现有 messages.json 创建基线
            if state is None and messages_file.exists():
                print("检测到已有 messages.json，创建增量基线...")
                ids = [msg["message_id"] for msg in existing_messages]
                last_time = max((msg["create_time"] for msg in existing_messages), default="")
                state = LastExportState(
                    version=CURRENT_VERSION,
                    chat_id=chat_id,
                    exported_at=datetime.now(LOCAL_TZ).isoformat(),
                    timezone=args.timezone,
                    last_message_time=last_time,
                    total_messages=len(existing_messages),
                    message_ids=ids,
                    last_page_token=None
                )
                save_state(output_dir / STATE_FILE, state)
                print(f"基线创建完成: {len(existing_messages)} 条消息")

            # 确定 start_time
            start_time = None
            if getattr(args, 'since', None):
                since_val = getattr(args, 'since', None)
                if since_val:
                    if "T" in since_val:
                        start_time = since_val
                    else:
                        start_time = local_time_to_iso8601(since_val)
            elif state:
                start_time = local_time_to_iso8601(state.last_message_time)

            # 检查断点续传
            resume_token, resume_page = load_checkpoint(output_dir)
            if resume_token:
                print(f"检测到中断的增量导出，从第 {resume_page} 页继续...")

            # 增量 fetch
            new_messages = fetch_messages_incremental(
                chat_id, args.user_id,
                start_time or "1970-01-01T00:00:00+08:00",
                resume_token, output_dir
            )

            if new_messages and existing_messages:
                merged, merge_result = merge_messages(existing_messages, new_messages)
                print(f"合并: +{merge_result.added} 新, ~{merge_result.updated} 更新, "
                      f"-{merge_result.deleted} 删除, 共 {merge_result.total} 条")
                messages = merged
                with open(messages_file, "w", encoding="utf-8") as f:
                    json.dump(merged, f, ensure_ascii=False, indent=2)
            elif new_messages:
                messages = new_messages
                with open(messages_file, "w", encoding="utf-8") as f:
                    json.dump(new_messages, f, ensure_ascii=False, indent=2)
            else:
                print("无新消息")
                messages = existing_messages
        else:
            # 完整导出模式
            fetched = fetch_messages(chat_id, args.user_id, output_dir, messages_file)
            if fetched == 0:
                print("错误: 无法获取消息，请检查 ID 是否正确或 lark-cli 授权状态")
                return 1
            with open(messages_file, encoding="utf-8") as f:
                messages = json.load(f)
    else:
        # 不获取，只处理已有
        if not existing_messages:
            print(f"错误: 找不到 messages.json，请使用 --fetch 自动获取")
            return 1
        messages = existing_messages

    print("=" * 50)
    print("飞书聊天记录导出")
    print(f"会话ID: {chat_id}")
    print(f"输出目录: {output_dir}")
    print(f"并发线程: {args.workers}")
    print(f"消息数量: {len(messages)}")
    print("=" * 50)

    # 提取资源引用
    image_refs, file_refs, media_refs = extract_resources(messages)
    total_image_refs = len(image_refs)
    total_file_refs = len(file_refs) + len(media_refs)
    print(f"发现 {total_image_refs} 个图片引用, {total_file_refs} 个文件引用")

    # 构建已下载映射
    existing = build_existing_map(res_dir)
    print(f"已有 {len(existing)} 个文件在本地")

    # 需要下载的资源
    to_download_images = {k: v for k, v in image_refs.items() if k not in existing}
    to_download_files = {}
    for key, (msg_id, fname, ftype) in file_refs.items():
        if key not in existing:
            to_download_files[key] = (msg_id, fname, ftype)
    for key, (msg_id, fname, ftype) in media_refs.items():
        if key not in existing:
            to_download_files[key] = (msg_id, fname, ftype)

    print(f"需要下载: {len(to_download_images)} 张图片, {len(to_download_files)} 个文件")

    # 下载统计
    counter_lock = threading.Lock()
    stats = {"images_downloaded": 0, "images_failed": 0, "files_downloaded": 0, "files_failed": 0}
    progress = [0]
    total_to_download = len(to_download_images) + len(to_download_files)

    def download_task(item):
        nonlocal progress
        if isinstance(item, tuple) and len(item) == 2:
            key, msg_id = item
            dest = images_dir / f"{key}.jpg"
            ftype = "image"
        else:
            key, (msg_id, fname, ftype) = item
            dest = files_dir / fname

        ok = download_resource(msg_id, key, ftype, dest, output_dir)

        with counter_lock:
            progress[0] += 1
            cur = progress[0]
            if ok:
                if isinstance(item, tuple) and len(item) == 2:
                    stats["images_downloaded"] += 1
                else:
                    stats["files_downloaded"] += 1
                print(f"[{cur}/{total_to_download}] 成功 {key[:30]}...")
            else:
                if isinstance(item, tuple) and len(item) == 2:
                    stats["images_failed"] += 1
                else:
                    stats["files_failed"] += 1
                print(f"[{cur}/{total_to_download}] 失败 {key[:30]}...")

    all_tasks = (
        list(to_download_images.items()) +
        [(k, v) for k, v in to_download_files.items()]
    )

    if all_tasks:
        print("\n开始下载...")
        start = time.time()
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(download_task, item) for item in all_tasks]
            for future in as_completed(futures):
                pass
        elapsed = time.time() - start
        print(f"\n下载完成: "
              f"{stats['images_downloaded']} 图片成功, {stats['images_failed']} 图片失败, "
              f"{stats['files_downloaded']} 文件成功, {stats['files_failed']} 文件失败, "
              f"耗时 {elapsed:.1f}s")
    else:
        print("\n所有资源已存在，跳过下载")

    # 重建完整资源映射
    resource_map = build_existing_map(res_dir)
    print(f"本地资源映射: {len(resource_map)} 个文件")

    # AI 摘要生成
    ai_summary_data = None
    if getattr(args, 'ai_summary', True):
        ai_summary_data = generate_ai_summary(
            messages,
            output_dir,
            force=getattr(args, 'force_ai', False)
        )

    # AI 图片理解生成
    ai_image_index = None
    if getattr(args, 'ai_images', False):
        ai_image_index = generate_ai_image_index(
            messages, output_dir, images_dir, resource_map,
            force=getattr(args, 'force_ai', False)
        )

    # 统计嵌入数
    embedded_count = 0
    for key, path in resource_map.items():
        if not path.exists():
            continue
        if is_file_embeddable(path):
            embedded_count += 1

    total_downloaded = stats["images_downloaded"] + stats["files_downloaded"]
    total_failed = stats["images_failed"] + stats["files_failed"]

    # 生成HTML
    print("\n生成HTML报告...")
    html = generate_html(
        messages=messages,
        resource_map=resource_map,
        subdir=res_dir,
        chat_id=chat_id,
        chat_name=chat_name,
        msg_count=len(messages),
        downloaded_count=total_downloaded,
        embedded_count=embedded_count,
        failed_count=total_failed,
        ai_summary=ai_summary_data,
        ai_image_index=ai_image_index,
        include_context=True,
    )

    output_html = output_dir / "report_with_images.html"
    with open(output_html, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n{'='*50}")
    print(f"导出完成!")
    print(f"HTML报告: {output_html}")
    print(f"消息总数: {len(messages)}")
    print(f"下载成功: {total_downloaded}")
    print(f"下载失败: {total_failed}")
    print(f"文件嵌入: {embedded_count}")
    print(f"{'='*50}")

    # 增量模式：更新 state 文件
    if export_mode == ExportMode.INCREMENTAL and messages:
        new_state = LastExportState(
            version=CURRENT_VERSION,
            chat_id=chat_id,
            exported_at=datetime.now(LOCAL_TZ).isoformat(),
            timezone=args.timezone,
            last_message_time=messages[-1].get("create_time", "") if messages else "",
            total_messages=len(messages),
            message_ids=[msg["message_id"] for msg in messages],
            last_page_token=None
        )
        save_state(output_dir / STATE_FILE, new_state)
        print(f"状态已更新: {STATE_FILE}")

    return 0


if __name__ == "__main__":
    exit(main())
