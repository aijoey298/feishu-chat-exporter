#!/usr/bin/env python3
"""
飞书聊天记录导出 - 生成仿飞书样式HTML报告
- 支持群聊和P2P聊天
- 16线程并发下载附件
- 文件嵌入策略：图片无上限行内显示，音视频/PDF按大小限制嵌入，其他文件仅生成下载链接
"""

import json
import os
import re
import subprocess
import time
import argparse
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading


# 文件大小限制（字节）
SIZE_LIMITS = {
    "audio": 30 * 1024 * 1024,   # 30MB
    "video": 100 * 1024 * 1024,  # 100MB
    "pdf": 50 * 1024 * 1024,     # 50MB
}
SIZE_LIMITS["mp3"] = SIZE_LIMITS["audio"]
SIZE_LIMITS["m4a"] = SIZE_LIMITS["audio"]
SIZE_LIMITS["wav"] = SIZE_LIMITS["audio"]
SIZE_LIMITS["mp4"] = SIZE_LIMITS["video"]
SIZE_LIMITS["mov"] = SIZE_LIMITS["video"]

EMBEDDABLE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".mp3", ".m4a", ".wav", ".mp4", ".mov", ".pdf"}


def check_dependencies():
    """检查必要的依赖工具"""
    if not shutil.which("lark-cli"):
        raise RuntimeError("lark-cli 未找到，请先安装并配置飞书CLI工具")
    return True


def fetch_messages(chat_id: str, user_id: str, output_dir: Path, output_file: Path) -> int:
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
            # 临时文件可能在 export_dir 下，需要移到目标位置
            tmp = export_dir / dest_path.name
            if tmp.exists() and tmp != dest_path:
                tmp.rename(dest_path)
            elif not dest_path.exists():
                # 尝试查找下载的文件
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
        return "&#9881;"  # gear
    return name[0].upper()


def get_file_size_limit(ext: str) -> int | None:
    """返回文件大小限制（字节），None表示无限制"""
    return SIZE_LIMITS.get(ext.lower().lstrip("."))


def is_file_embeddable(path: Path, file_type: str) -> bool:
    """判断文件是否满足嵌入条件"""
    if not path.exists():
        return False
    if file_type in ("image",):
        return True  # 图片无上限
    ext = path.suffix.lower()
    limit = get_file_size_limit(ext)
    if limit is None:
        return False
    return path.stat().st_size <= limit


def format_content(content: str, resource_map: dict, subdir: Path) -> str:
    """替换消息内容中的图片引用为<img>标签，并处理链接和@"""
    if not content:
        return ""

    # 替换 [Image: img_v3_xxx] 或直接嵌入 img_v3_xxx 引用
    def replace_image(match):
        key = match.group(1)
        path = resource_map.get(key)
        if path and path.exists():
            rel = f"{subdir.name}/{path.name}"
            return f'<br><img src="{rel}" alt="图片" loading="lazy"><br>'
        return f'<br><div class="loading-img">[图片未下载: {escape_html(key[:20])}...]</div><br>'

    content = re.sub(r'\[Image: (img_v3_[a-zA-Z0-9_-]+)\]', replace_image, content)

    # 飞书 post 内容中的图片 key
    content = re.sub(r'(img_v3_[a-zA-Z0-9_-]+)', replace_image, content)

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
) -> str:
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
.msg-content a {{ color: #fe6803; text-decoration: none; }}
.msg-content a:hover {{ text-decoration: underline; }}
.system {{ background: #f8f8f8; border: 1px dashed #ddd; }}
.system .msg-content {{ color: #888; font-size: 13px; }}
.mention {{ color: #007aff; font-weight: 500; }}
.footer {{ text-align: center; color: #bbb; font-size: 12px; padding: 30px; }}
.loading-img {{ background: #f0f0f0; border-radius: 8px; padding: 40px; text-align: center; color: #999; font-size: 13px; }}
</style>
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
<div class="container">
{messages}
</div>
<div class="footer">
  由 Claude Code 自动生成
</div>
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
    )


def extract_resources(messages: list) -> tuple[dict, dict, dict]:
    """
    从消息中提取所有资源引用。
    返回: (image_refs, audio_refs, video_refs, other_refs)
    其中 refs = {file_key: msg_id}
    """
    image_refs = {}
    file_refs = {}  # file_key -> (msg_id, file_name, file_type)
    media_refs = {}  # file_key -> msg_id

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
            # 匹配图片 key
            keys = re.findall(r'img_v3_[a-zA-Z0-9_-]+', content)
            for key in keys:
                if key not in image_refs:
                    image_refs[key] = msg_id
            # 匹配文件 key 和 name
            file_matches = re.findall(r'file_key["\s:]+([^"]+)', content)
            name_matches = re.findall(r'file_name["\s:]+([^"]+)', content)
            for i, key in enumerate(file_matches):
                if key.startswith("msg_file_"):
                    fname = name_matches[i] if i < len(name_matches) else f"{key}.bin"
                    if key not in file_refs:
                        file_refs[key] = (msg_id, fname, "file")

        elif msg_type == "media":
            # 封面图片
            cover_match = re.search(r'cover_image_key="(img_v3_[a-zA-Z0-9_-]+)"', content)
            if cover_match:
                key = cover_match.group(1)
                if key not in image_refs:
                    image_refs[key] = msg_id
            # 文件 key
            media_keys = re.findall(r'(msg_file_[a-zA-Z0-9_-]+)', content)
            for key in media_keys:
                if key not in media_refs:
                    media_refs[key] = msg_id

        elif msg_type == "file":
            keys = re.findall(r'(msg_file_[a-zA-Z0-9_-]+)', content)
            names = re.findall(r'file_name["\s:]+([^"]+)', content)
            for i, key in enumerate(keys):
                fname = names[i] if i < len(names) else f"{key}.bin"
                if key not in file_refs:
                    file_refs[key] = (msg_id, fname, "file")

    return image_refs, file_refs, media_refs


def build_existing_map(res_dir: Path) -> dict:
    """构建已下载文件映射: file_key -> Path"""
    mapping = {}
    for p in res_dir.rglob("*"):
        if p.is_file():
            # 从文件名提取 key (去掉扩展名)
            key = p.stem
            if key not in mapping:
                mapping[key] = p
    return mapping


def resolve_file_type(file_key: str) -> str:
    """根据 file_key 猜测资源类型"""
    if file_key.startswith("img_"):
        return "image"
    if file_key.startswith("audio_"):
        return "audio"
    if file_key.startswith("video_"):
        return "video"
    if file_key.startswith("msg_file_"):
        return "file"
    return "file"


def main():
    parser = argparse.ArgumentParser(description="飞书聊天记录导出工具")
    parser.add_argument("--chat-id", dest="chat_id", help="群聊ID (oc_xxx)")
    parser.add_argument("--user-id", dest="user_id", help="P2P用户ID (ou_xxx)")
    parser.add_argument("--output", dest="output", default=".", help="输出目录 (默认当前目录)")
    parser.add_argument("--workers", dest="workers", type=int, default=16, help="并发下载线程数 (默认16)")
    parser.add_argument("--fetch", dest="fetch", action="store_true", help="自动获取消息（无需手动准备messages.json）")
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
    if not messages_file.exists():
        if args.fetch:
            fetched = fetch_messages(chat_id, args.user_id, output_dir, messages_file)
            if fetched == 0:
                print("错误: 无法获取消息，请检查 ID 是否正确或 lark-cli 授权状态")
                return 1
        else:
            print(f"错误: 找不到 messages.json，请使用 --fetch 自动获取，或先运行 lark-cli im +chat-messages-list 获取消息数据")
            return 1

    with open(messages_file, encoding="utf-8") as f:
        messages = json.load(f)

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
    for key, msg_id in media_refs.items():
        if key not in existing:
            to_download_files[key] = (msg_id, f"{key}.bin", "file")

    print(f"需要下载: {len(to_download_images)} 张图片, {len(to_download_files)} 个文件")

    # 下载统计
    counter_lock = threading.Lock()
    stats = {"images_downloaded": 0, "images_failed": 0, "files_downloaded": 0, "files_failed": 0}
    progress = [0]
    total_to_download = len(to_download_images) + len(to_download_files)

    def download_task(item):
        nonlocal progress
        idx = 0
        if isinstance(item, tuple) and len(item) == 2:
            # 图片
            key, msg_id = item
            dest = images_dir / f"{key}.jpg"
            idx_offset = 0
        else:
            # 文件
            key, (msg_id, fname, ftype) = item
            dest = files_dir / fname
            idx_offset = len(to_download_images)

        ftype = resolve_file_type(key) if isinstance(item, tuple) else ftype
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

    # 统计嵌入数
    embedded_count = 0
    for key, path in resource_map.items():
        if not path.exists():
            continue
        ext = path.suffix.lower()
        if ext in EMBEDDABLE_EXTS:
            limit = get_file_size_limit(ext)
            if limit is None or path.stat().st_size <= limit:
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
    return 0


if __name__ == "__main__":
    exit(main())
