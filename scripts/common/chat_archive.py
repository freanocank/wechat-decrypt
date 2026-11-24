"""Create a private, offline WeChat export with explicitly supplied images."""

from datetime import datetime
from hashlib import sha256
from html import escape
import json
import os
from pathlib import Path
import shutil
import stat
from tempfile import NamedTemporaryFile
from urllib.parse import quote
from uuid import uuid4

import appmsg
import contacts
from media_manifest import _image_suffix, write_manifest
import message


_MESSAGES = {
    "1": "text",
    "3": "image",
    "34": "audio",
    "43": "video",
    "47": "sticker",
    "10000": "system",
    "10002": "system",
}
_ATTACHMENT_LABELS = {
    "missing": "未找到图片",
    "ambiguous": "匹配到多个候选图片",
    "unreadable": "无法读取图片",
}


def _id(kind, *values):
    encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()
    return f"{kind}_{sha256(encoded).hexdigest()[:32]}"


def _source_id(conversation_id, row):
    server_id = str(row.get("server_id") or "0")
    if server_id != "0":
        return _id("message", conversation_id, "server", server_id)
    shard = Path(row.get("_db") or "").name
    return _id("message", conversation_id, "local", shard, str(row.get("local_id") or ""))


def _iso_time(timestamp):
    return datetime.fromtimestamp(int(timestamp)).astimezone().isoformat(timespec="seconds")


def _visible_name(name, identity=""):
    if not name or name == identity or name.startswith("wxid_") or name.endswith(("@chatroom", "@openim")):
        return ""
    if identity and name == f"群聊({identity.split('@', 1)[0]})":
        return ""
    return name


def _resolved_name(wxid):
    return _visible_name(contacts.resolve_nickname(wxid), wxid) if wxid else ""


def _quote_name(content):
    display, identities = appmsg.read_quote_sender_hints(content)
    if display not in identities and _visible_name(display):
        return display
    for identity in identities:
        if identity.endswith(("@chatroom", "@openim")):
            continue
        name = _resolved_name(identity)
        if name:
            return name
    return "未识别发送者"


def _sender(account, row, kind):
    if kind == "system":
        return {"id": None, "name": "系统"}
    wxid = account if row.get("is_me") == "1" else row.get("sender_wxid")
    if wxid:
        return {"id": _id("sender", account, wxid), "name": _resolved_name(wxid) or "未识别发送者"}
    real_id = str(row.get("real_sender_id") or "")
    shard = Path(row.get("_db") or "").name
    sender_id = _id("sender", account, "local", shard, real_id) if real_id else None
    return {"id": sender_id, "name": "未识别发送者"}


def _attachment(message_id, source, item=None):
    record_index = item["record_index"] if item is not None else None
    slot_id = _id("attachment", message_id, source, record_index)
    attachment = {"id": slot_id, "kind": "image", "source": source, "status": "missing"}
    if item is not None:
        attachment.update({field: item[field] for field in ("record_index", "sender_name", "source_time")})
    return attachment


def build_archive(rows, *, account, conversation, display, since, until, voice_map):
    conversation_id = _id("conversation", account, conversation)
    is_group = conversation.endswith(("@chatroom", "@openim"))
    conversation_name = _visible_name(display, conversation) or ("未命名群聊" if is_group else "未命名会话")
    archive = {
        "schema_version": 1,
        "conversation": {"id": conversation_id, "name": conversation_name},
        "range": {"start": _iso_time(since.timestamp()), "end_exclusive": _iso_time(until.timestamp())},
        "messages": [],
    }
    seen = set()
    identified = [(_source_id(conversation_id, row), row) for row in rows]
    identified.sort(key=lambda pair: (int(pair[1]["create_time"]), pair[0]))
    for message_id, row in identified:
        if message_id in seen:
            continue
        seen.add(message_id)
        archive["messages"].append(_record(account, row, message_id, voice_map, is_group))
    return archive


def _record(account, row, message_id, voice_map, is_group):
    raw_type = row.get("local_type")
    type_code = message.normalize_type(raw_type)
    kind = _MESSAGES.get(type_code, "unknown")
    result = {
        "id": message_id,
        "sent_at": _iso_time(row["create_time"]),
        "sender": _sender(account, row, kind),
        "type": kind,
        "body": "",
        "attachments": [],
    }
    if kind == "image":
        result["attachments"].append(_attachment(message_id, "direct"))
        return result
    if kind == "audio":
        result["body"] = voice_map.get(str(row.get("server_id")), "")
        return result
    if kind not in {"text", "system"} and type_code != "49":
        return result

    try:
        body = message.decode_message_hex(row.get("msg_hex"), strip_sender_prefix=False)
    except (ValueError, RuntimeError, OSError):
        result["content_status"] = "unreadable"
        return result
    source_sender = row.get("sender_wxid")
    if is_group and source_sender:
        for separator in (":\r\n", ":\n", ": "):
            prefix = source_sender + separator
            if body.startswith(prefix):
                body = body[len(prefix):]
                break
    if kind == "system":
        system = message.parse_system_message(body, default_event="recall" if type_code == "10002" else "")
        result.update(body=system["text"], event=system["event"])
    elif type_code == "49":
        parsed = appmsg.parse_app_message(body, raw_type)
        result.update(type=parsed["kind"], body=parsed["title"] or parsed["description"] or parsed["label"])
        if parsed["kind"] == "quote":
            result["quote"] = {"sender_name": _quote_name(body), "body": parsed["quoted_content"]}
        if parsed["kind"] == "chat_history":
            items = appmsg.read_forwarded_items(body)
            for item in items:
                name = item["sender_name"]
                item["sender_name"] = _visible_name(name) or _resolved_name(name)
            result["forwarded_items"] = items
            result["attachments"].extend(
                _attachment(message_id, "forwarded", item)
                for item in items if item["type"] == "image"
            )
        if parsed["kind"] not in {"quote", "chat_history"}:
            result["body"] = parsed["summary"] or parsed["label"]
    else:
        result["body"] = body
    return result


def load_media_map(path):
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_json_object)
    if not isinstance(data, dict):
        raise ValueError("图片映射必须是附件 ID 到图片路径的 JSON 对象")
    mapping = {}
    for slot_id, paths in data.items():
        paths = paths if isinstance(paths, list) else [paths]
        if not paths or any(not isinstance(item, str) or not item.strip() for item in paths):
            raise ValueError("图片映射的值必须是路径字符串或非空路径数组")
        mapping[slot_id] = tuple(dict.fromkeys(path.parent / Path(item).expanduser() for item in paths))
    return mapping


def _unique_json_object(pairs):
    data = dict(pairs)
    if len(data) != len(pairs):
        raise ValueError("图片映射包含重复的附件 ID")
    return data


def _copy_image(source, asset_dir, slot_id):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        if not stat.S_ISREG(source.lstat().st_mode):
            return {"status": "unreadable", "reason": "not_regular_file"}
        fd = os.open(source, flags)
    except FileNotFoundError:
        return {"status": "missing", "reason": "mapped_file_missing"}
    except (OSError, ValueError):
        return {"status": "unreadable", "reason": "mapped_file_unreadable"}
    with os.fdopen(fd, "rb") as handle:
        try:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return {"status": "unreadable", "reason": "not_regular_file"}
            suffix = _image_suffix(handle.read(12))
            handle.seek(0)
        except OSError:
            return {"status": "unreadable", "reason": "mapped_file_unreadable"}
        if not suffix:
            return {"status": "unreadable", "reason": "unsupported_image_format"}
        asset_dir.mkdir(mode=0o700, exist_ok=True)
        dest = asset_dir / f"{slot_id}{suffix}"
        dest_fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(dest_fd, "wb") as output:
            shutil.copyfileobj(handle, output)
    return {"status": "resolved", "path": (Path(asset_dir.name) / dest.name).as_posix()}


def _write_private(path, content):
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_archive(archive, output, export_format, media_map):
    export_id = uuid4().hex[:12]
    asset_dir = output.with_name(f"{output.stem}.assets-{export_id}")
    manifest_path = output.with_name(f"{output.stem}.media-{export_id}.json")
    manifest_rows = []
    matched = set()
    committed = False
    try:
        for row in archive["messages"]:
            for attachment in row["attachments"]:
                slot_id = attachment["id"]
                attachment.pop("path", None)
                attachment.pop("reason", None)
                paths = media_map.get(slot_id, ())
                if paths:
                    matched.add(slot_id)
                if len(paths) == 1:
                    attachment.update(_copy_image(paths[0], asset_dir, slot_id))
                elif len(paths) > 1:
                    attachment.update(status="ambiguous", reason="multiple_mapped_candidates")
                else:
                    attachment.update(status="missing", reason="no_mapping")
                manifest_rows.append({"message_id": row["id"], "sent_at": row["sent_at"], **attachment})

        archive["assets_directory"] = asset_dir.name if asset_dir.is_dir() else None
        archive["unmatched_mapping_count"] = len(media_map.keys() - matched)
        if export_format == "html":
            manifest = {key: value for key, value in archive.items() if key != "messages"}
            manifest["attachments"] = manifest_rows
            write_manifest(manifest_path, manifest)
            _write_private(output, _render_html(archive, manifest_path.name))
        else:
            write_manifest(output, archive)
        committed = True
    finally:
        if not committed:
            manifest_path.unlink(missing_ok=True)
            if asset_dir.is_dir():
                shutil.rmtree(asset_dir)


def _render_attachment(attachment):
    if attachment["status"] == "resolved":
        url = escape(quote(attachment["path"]), quote=True)
        return f'<figure><a href="{url}"><img src="{url}" alt="聊天图片" loading="lazy"></a></figure>'
    label = _ATTACHMENT_LABELS[attachment["status"]]
    return f'<p class="attachment-status">{label} <code>{escape(attachment["id"])}</code></p>'


def _render_message(row):
    name = escape(row["sender"]["name"])
    sent_at = escape(row["sent_at"])
    body = f'<div class="text">{escape(row["body"])}</div>' if row["body"] else ""
    if row.get("quote"):
        quoted = row["quote"]
        body += f'<blockquote class="text">{escape(quoted["sender_name"])}\n{escape(quoted["body"])}</blockquote>'
    if row.get("forwarded_items"):
        slots = {attachment["record_index"]: attachment for attachment in row["attachments"]}
        for item in row["forwarded_items"]:
            attribution = escape(f'{item["sender_name"]} {item["source_time"]}'.strip())
            if item["type"] == "image":
                item_html = _render_attachment(slots[item["record_index"]])
            else:
                item_html = escape(item["body"] or "[未解析的转发条目]")
            body += f'<blockquote class="text"><small>{attribution}</small><div>{item_html}</div></blockquote>'
    else:
        body += "".join(_render_attachment(attachment) for attachment in row["attachments"])
    if not body:
        body = f'<div class="text">[{escape(row["type"])}]</div>'
    return f'<article id="{escape(row["id"])}"><header><b>{name}</b> <time datetime="{sent_at}">{sent_at}</time></header>{body}</article>\n'


def _render_html(archive, manifest_name):
    title = escape(archive["conversation"]["name"])
    manifest_url = escape(quote(manifest_name), quote=True)
    messages = "".join(_render_message(row) for row in archive["messages"])
    return f'''<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' file:; style-src 'unsafe-inline'">
<title>{title}</title>
<style>
  body {{ color: #20242a; background: #f4f5f7; font: 1rem/1.6 system-ui, sans-serif; }}
  main {{ max-width: 62rem; margin: 2rem auto; padding: 0 1rem; }}
  article {{ background: white; border: 1px solid #d8dde3; border-radius: .6rem; margin: .85rem 0; padding: 1rem; }}
  time, small, .attachment-status {{ color: #525e6b; }}
  .text {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
  blockquote {{ margin: .5rem 0; padding: .5rem 1rem; border-left: .2rem solid #a5b1bf; }}
  figure {{ margin: .6rem 0; }}
  img {{ display: block; max-width: 100%; height: auto; }}
</style>
<main>
<h1>{title}</h1>
<p>{len(archive["messages"])} 条消息 · <a href="{manifest_url}">图片清单</a></p>
{messages}</main>
</html>
'''
