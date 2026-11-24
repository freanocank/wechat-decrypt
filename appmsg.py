"""Parse WeChat app-message cards such as music, links, files, and Channels."""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET


APP_MESSAGE_TYPES = {
    1: ("text_share", "文本分享"),
    2: ("image_share", "图片分享"),
    3: ("music", "音乐分享"),
    4: ("video_link", "视频分享"),
    5: ("link", "链接分享"),
    6: ("file", "文件"),
    7: ("link", "链接分享"),
    8: ("emoji", "表情分享"),
    17: ("live_location", "实时位置"),
    19: ("chat_history", "聊天记录"),
    21: ("mini_program", "小程序"),
    33: ("mini_program", "小程序"),
    36: ("mini_program", "小程序"),
    40: ("forwarded", "转发消息"),
    51: ("finder", "视频号"),
    57: ("quote", "引用消息"),
    63: ("finder_live", "视频号直播"),
    68: ("link", "链接分享"),
    87: ("group_notice", "群公告"),
    88: ("finder_live", "视频号直播"),
    2000: ("payment", "转账/收款"),
    2001: ("red_packet", "红包"),
    2003: ("red_packet", "红包"),
}

_UNKNOWN_KIND = ("app_message", "分享卡片")
_UNSUPPORTED_TITLES = ("当前版本不支持", "请升级至最新版本")


def _clean(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return html.unescape(str(value)).strip("\x00\r\n \t")


def _first(*values) -> str:
    for value in values:
        cleaned = _clean(value)
        if cleaned:
            return cleaned
    return ""


def _local_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def _node_text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return _clean("".join(node.itertext()))


def _direct_node(node: ET.Element | None, *names: str) -> ET.Element | None:
    if node is None:
        return None
    wanted = {name.lower() for name in names}
    return next((child for child in node if _local_name(child.tag) in wanted), None)


def _descendant_node(node: ET.Element | None, *names: str) -> ET.Element | None:
    if node is None:
        return None
    wanted = {name.lower() for name in names}
    return next((child for child in node.iter() if _local_name(child.tag) in wanted), None)


def _direct_text(node: ET.Element | None, *names: str) -> str:
    return _node_text(_direct_node(node, *names))


def _descendant_text(node: ET.Element | None, *names: str) -> str:
    return _node_text(_descendant_node(node, *names))


def _extract_block(text: str, tag: str) -> str:
    match = re.search(
        rf"<{re.escape(tag)}\b[^>]*>([\s\S]*?)</{re.escape(tag)}>",
        text,
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else ""


def _extract_tag_text(text: str, tag: str) -> str:
    block = _extract_block(text, tag)
    if not block:
        return ""
    cdata = re.fullmatch(r"\s*<!\[CDATA\[([\s\S]*?)\]\]>\s*", block)
    if cdata:
        return _clean(cdata.group(1))
    return _clean(re.sub(r"<[^>]+>", "", block))


def _xml_fragment(content: str | bytes | None) -> str:
    if content is None:
        return ""
    if isinstance(content, bytes):
        text = content.decode("utf-8", errors="replace")
    else:
        text = str(content)
    text = text.strip("\x00\r\n \t")
    if not text:
        return ""
    text = "".join(
        char for char in text
        if char in "\r\n\t" or ord(char) >= 32
    )
    positions = [position for marker in ("<?xml", "<msg", "<appmsg")
                 if (position := text.lower().find(marker)) >= 0]
    if positions:
        text = text[min(positions):]
    lower = text.lower()
    root_probe = re.sub(r"^<\?xml[\s\S]*?\?>\s*", "", lower).lstrip()
    closings = ("</msg>",) if root_probe.startswith("<msg") else ("</appmsg>", "</msg>")
    for closing in closings:
        end = lower.rfind(closing)
        if end >= 0:
            text = text[:end + len(closing)]
            break
    return text.strip()


class _Payload:
    def __init__(self, content: str | bytes | None):
        self.text = _xml_fragment(content)
        self.root: ET.Element | None = None
        self.app: ET.Element | None = None
        if self.text:
            try:
                self.root = ET.fromstring(self.text)
            except ET.ParseError:
                self.root = None
        if self.root is not None:
            self.app = (
                self.root
                if _local_name(self.root.tag) == "appmsg"
                else _descendant_node(self.root, "appmsg")
            )
        self.app_text = _extract_block(self.text, "appmsg") or self.text

    def direct(self, *names: str) -> str:
        value = _direct_text(self.app, *names)
        if value or self.app is not None:
            return value
        for name in names:
            value = _extract_tag_text(self.app_text, name)
            if value:
                return value
        return ""

    def nested_node(self, *names: str) -> ET.Element | None:
        return _descendant_node(self.app, *names)

    def nested_text(self, scope: ET.Element | None, *names: str) -> str:
        value = _descendant_text(scope, *names)
        if value:
            return value
        block = ""
        for scope_name in ("finderFeed", "finderLive", "weappinfo", "wxaappinfo", "refermsg"):
            if scope_name.lower() in {_local_name(scope.tag) if scope is not None else ""}:
                block = _extract_block(self.app_text, scope_name)
                break
        for name in names:
            value = _extract_tag_text(block or self.app_text, name)
            if value:
                return value
        return ""


def _integer(value, default: int = 0) -> int:
    try:
        return int(str(value or "0").strip())
    except (TypeError, ValueError):
        return default


def _raw_subtype(raw_type) -> int:
    try:
        return (int(raw_type or 0) >> 32) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return 0


def _human_size(value: str) -> str:
    size = _integer(value)
    if size <= 0:
        return ""
    number = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if number < 1024 or unit == "TB":
            return f"{number:.0f} {unit}" if unit == "B" else f"{number:.1f} {unit}"
        number /= 1024
    return ""


def _is_placeholder_title(value: str) -> bool:
    return any(marker in value for marker in _UNSUPPORTED_TITLES)


def _base_result(app_type: int, payload: _Payload) -> dict:
    kind, label = APP_MESSAGE_TYPES.get(app_type, _UNKNOWN_KIND)
    return {
        "app_type": app_type,
        "kind": kind,
        "label": label,
        "title": payload.direct("title"),
        "description": payload.direct("des", "description"),
        "source": payload.direct("sourcedisplayname", "appname"),
        "source_username": payload.direct("sourceusername"),
        "url": payload.direct("url"),
        "thumb_url": payload.direct("thumburl", "cdnthumburl"),
    }


def parse_app_message(content: str | bytes | None, raw_type=0) -> dict:
    """Return stable metadata for a WeChat ``local_type=49`` app message."""
    payload = _Payload(content)
    xml_type = _integer(payload.direct("type"))
    app_type = xml_type or _raw_subtype(raw_type)
    result = _base_result(app_type, payload)
    kind = result["kind"]

    if kind == "music":
        result["artist"] = result["description"]
        result["url"] = _first(
            payload.direct("dataurl"),
            payload.direct("lowdataurl"),
            result["url"],
        )
        result["thumb_url"] = _first(
            payload.direct("songalbumurl", "mvcoverurl"),
            result["thumb_url"],
        )

    elif kind == "file":
        result["file_name"] = result["title"]
        result["file_ext"] = payload.direct("fileext")
        result["file_size"] = _integer(payload.direct("totallen"))
        result["file_md5"] = payload.direct("md5", "filemd5", "file_md5")

    elif kind == "mini_program":
        weapp = payload.nested_node("weappinfo", "wxaappinfo")
        result["mini_program_username"] = payload.nested_text(weapp, "username")
        result["mini_program_appid"] = payload.nested_text(weapp, "appid")
        result["page_path"] = payload.nested_text(weapp, "pagepath", "path")
        result["thumb_url"] = _first(
            payload.nested_text(weapp, "weappiconurl", "iconurl"),
            result["thumb_url"],
        )

    elif kind == "finder":
        finder = payload.nested_node("finderfeed")
        finder_desc = payload.nested_text(finder, "desc", "description")
        nickname = payload.nested_text(finder, "nickname", "findernickname")
        result["finder_username"] = payload.nested_text(
            finder, "username", "finderusername"
        )
        result["object_id"] = payload.nested_text(finder, "objectid")
        result["object_nonce_id"] = payload.nested_text(finder, "objectnonceid")
        result["description"] = _first(finder_desc, result["description"])
        result["source"] = _first(nickname, result["source"], "视频号")
        result["url"] = _first(
            result["url"],
            payload.nested_text(finder, "url", "playurl", "dataurl"),
        )
        result["thumb_url"] = _first(
            payload.nested_text(finder, "coverurl", "thumburl", "avatar"),
            result["thumb_url"],
        )
        if not result["title"] or _is_placeholder_title(result["title"]):
            result["title"] = result["description"]

    elif kind == "finder_live":
        live = payload.nested_node("finderlive")
        nickname = payload.nested_text(live, "nickname", "findernickname")
        result["finder_username"] = payload.nested_text(
            live, "finderusername", "username"
        )
        result["live_id"] = payload.nested_text(live, "liveid", "finderliveid")
        result["object_id"] = payload.nested_text(live, "objectid")
        result["description"] = _first(
            payload.nested_text(live, "desc", "description"),
            result["description"],
        )
        result["source"] = _first(nickname, result["source"], "视频号")
        result["thumb_url"] = _first(
            payload.nested_text(live, "coverurl", "headurl", "avatar"),
            result["thumb_url"],
        )
        if not result["title"] or _is_placeholder_title(result["title"]):
            result["title"] = result["description"]

    elif kind == "quote":
        refer = payload.nested_node("refermsg")
        result["quoted_sender"] = payload.nested_text(
            refer, "displayname", "chatusr", "fromusr"
        )
        result["quoted_content"] = payload.nested_text(refer, "content")
        result["quoted_type"] = _integer(payload.nested_text(refer, "type"))

    elif kind == "payment":
        result["description"] = _first(
            payload.direct("feedesc", "pay_memo", "sendertitle", "receivertitle"),
            result["description"],
        )

    elif kind == "red_packet":
        result["description"] = _first(
            payload.direct("sendertitle", "receivertitle", "wishing"),
            result["description"],
        )

    result["summary"] = format_app_message(result)
    return result


def read_quote_sender_hints(content: str) -> tuple[str, tuple[str, str]]:
    payload = _Payload(content)
    refer = payload.nested_node("refermsg")
    display = payload.nested_text(refer, "displayname")
    return display, (payload.nested_text(refer, "fromusr"), payload.nested_text(refer, "chatusr"))


def read_forwarded_items(content: str) -> list[dict]:
    """Read immediate records of a type-19 merged chat card."""
    record_item = _direct_node(_Payload(content).app, "recorditem")
    if record_item is None:
        return []
    record = _direct_node(record_item, "recordinfo")
    data_list = _direct_node(record if record is not None else record_item, "datalist")
    if data_list is None:
        encoded = (record_item.text or "").strip()
        for value in dict.fromkeys((encoded, html.unescape(encoded))):
            try:
                record = ET.fromstring(value)
            except ET.ParseError:
                continue
            data_list = _direct_node(record, "datalist")
            if data_list is not None:
                break
    if data_list is None:
        return []

    items = []
    for index, item in enumerate(data_list, start=1):
        if _local_name(item.tag) != "dataitem":
            continue
        kind = {"1": "text", "2": "image"}.get(item.get("datatype"), "unknown")
        items.append({
            "record_index": index,
            "type": kind,
            "sender_name": _direct_text(item, "sourcename"),
            "source_time": _direct_text(item, "sourcetime"),
            "body": _direct_text(item, "datadesc") if kind == "text" else "",
        })
    return items


def format_app_message(data: dict, include_url: bool = True) -> str:
    """Build a concise, loss-resistant human-readable summary."""
    kind = data.get("kind", "app_message")
    title = _clean(data.get("title"))
    description = _clean(data.get("description"))
    source = _clean(data.get("source"))
    url = _clean(data.get("url")) if include_url else ""

    if kind == "quote":
        quoted_sender = _clean(data.get("quoted_sender"))
        quoted_content = _clean(data.get("quoted_content"))
        base = title
        if quoted_sender or quoted_content:
            quote = f"{quoted_sender}: {quoted_content}".strip(": ")
            base = f"{base} [↩ {quote}]".strip()
        return base

    parts = []
    if title:
        parts.append(title)
    if description and description != title:
        parts.append(description)
    if source and source not in parts:
        parts.append(f"来源：{source}")

    if kind == "file":
        file_size = _human_size(str(data.get("file_size", "")))
        file_ext = _clean(data.get("file_ext"))
        detail = " / ".join(value for value in (file_ext, file_size) if value)
        if detail:
            parts.append(detail)
    elif kind == "mini_program":
        page_path = _clean(data.get("page_path"))
        if page_path:
            parts.append(f"页面：{page_path}")

    if include_url and url:
        parts.append(url)
    if not parts:
        app_type = _integer(data.get("app_type"))
        return f"类型 {app_type}" if app_type else "未识别分享内容"
    return " | ".join(dict.fromkeys(parts))
