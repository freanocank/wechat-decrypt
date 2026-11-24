"""Message formatting, type mapping, and sender detection."""
from datetime import datetime
from functools import lru_cache
import html
import re
import xml.etree.ElementTree as ET

import db

MSG_TYPES = {
    "1": "文本", "3": "图片", "34": "语音", "42": "名片",
    "43": "视频", "47": "表情", "48": "位置", "49": "链接/文件",
    "50": "通话", "10000": "系统消息", "10002": "撤回",
}

SYSTEM_EVENTS = {
    "pat": "拍一拍",
    "recall": "撤回",
    "group_join": "加入群聊",
    "group_remove": "移出群聊",
    "group_leave": "退出群聊",
    "group_rename": "修改群名",
    "group_notice": "群公告",
    "group_admin": "群管理员",
    "group_owner": "群主变更",
    "group_disband": "解散群聊",
    "friend_added": "添加好友",
    "red_packet": "红包",
    "payment": "转账/收款",
    "call": "通话",
    "chat_pinned": "置顶",
    "system": "系统消息",
}

_SYSTEM_RULES = (
    ("pat", ("拍了拍", "拍一拍")),
    ("recall", ("撤回了一条消息", "撤回了")),
    ("group_disband", ("解散该群聊", "解散了群聊", "群聊已解散")),
    ("group_remove", ("移出群聊", "移除群聊")),
    ("group_leave", ("退出群聊", "退出了群聊", "已退出群聊")),
    ("group_join", ("加入群聊", "加入了群聊", "进入群聊")),
    ("group_rename", ("修改群名", "群名改为", "修改群聊名称", "群聊名称修改为")),
    ("group_notice", ("群公告", "发布了新公告")),
    ("group_admin", ("群管理员", "设为管理员", "设置成为管理员", "取消管理员")),
    ("group_owner", ("成为新群主", "转让群主", "群主已转让")),
    ("friend_added", ("你已添加了", "朋友验证", "添加你为朋友")),
    ("red_packet", ("红包",)),
    ("payment", ("转账", "收款", "付款")),
    ("call", ("通话", "语音聊天", "视频聊天")),
    ("chat_pinned", ("置顶",)),
)

_XML_EVENT_TYPES = {
    "pat": "pat",
    "revokemsg": "recall",
}

_SYSTEM_EVENT_ALIASES = {
    "拍了拍": "pat",
    "拍一拍": "pat",
    "撤回": "recall",
    "进群": "group_join",
    "入群": "group_join",
    "加群": "group_join",
    "移出群聊": "group_remove",
    "退群": "group_leave",
    "群名": "group_rename",
    "公告": "group_notice",
    "管理员": "group_admin",
    "群主": "group_owner",
    "解散": "group_disband",
    "好友": "friend_added",
    "红包": "red_packet",
    "转账": "payment",
    "收款": "payment",
    "通话": "call",
    "置顶": "chat_pinned",
}

_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_SENDER_PREFIX = re.compile(r"^[^\s:]{1,60}:[\n ]")


def _extract_xml_system_text(content: str) -> tuple[str, str]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        for tag in ("replacemsg", "template", "content", "title"):
            match = re.search(
                rf"<{tag}>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</{tag}>",
                content,
            )
            if match:
                return "", html.unescape(match.group(1)).strip()
        return "", ""

    xml_type = root.attrib.get("type", "").strip().lower()
    fields = {}
    for node in root.iter():
        value = (node.text or "").strip()
        if value and len(node) == 0:
            fields[node.tag] = html.unescape(value)

    text = ""
    for tag in ("replacemsg", "template", "content", "title"):
        if fields.get(tag):
            text = fields[tag]
            break
    if text:
        text = re.sub(
            r"\$\{([A-Za-z0-9_]+)\}",
            lambda match: fields.get(match.group(1), match.group(0)),
            text,
        )
    return xml_type, text.strip()


def parse_system_message(
    content: str | bytes | None,
    default_event: str = "",
) -> dict[str, str]:
    """Return a stable event code, display label, and readable system text."""
    if content is None:
        text = ""
    elif isinstance(content, bytes):
        text = content.decode("utf-8", errors="replace")
    else:
        text = str(content)
    text = html.unescape(text).strip("\x00\r\n ")
    xml_type = ""
    if re.match(r"^(?:<\?xml[\s\S]*?\?>\s*)?<[^\s>]+", text):
        parsed_type, parsed_text = _extract_xml_system_text(text)
        if parsed_type or parsed_text:
            xml_type = parsed_type
            if parsed_text:
                text = parsed_text

    event = _XML_EVENT_TYPES.get(xml_type, "")
    if not event:
        for candidate, needles in _SYSTEM_RULES:
            if any(needle in text for needle in needles):
                event = candidate
                break
    event = event or (default_event if default_event in SYSTEM_EVENTS else "system")
    return {"event": event, "label": SYSTEM_EVENTS[event], "text": text}


def normalize_system_event_filter(value: str) -> str:
    """Map a stable event code or common Chinese label to an event code."""
    normalized = value.strip().lower()
    if not normalized:
        return ""
    if normalized in SYSTEM_EVENTS:
        return normalized
    for code, label in SYSTEM_EVENTS.items():
        if normalized == label.lower():
            return code
    return _SYSTEM_EVENT_ALIASES.get(value.strip(), normalized)


def extract_text_from_blob(data: bytes) -> str:
    """Extract UTF-8 text from a WeChat 4.x container when zstd decoding fails."""
    offsets = [10, *(offset for offset in range(min(16, len(data))) if offset != 10)]
    for offset in offsets:
        chunk = data[offset:].split(b"\x01\x00", 1)[0]
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            continue
        text = "".join(
            char for char in text
            if char in "\r\n\t" or ord(char) >= 32 and ord(char) != 127
        ).strip()
        if not text or not any(char.isalnum() for char in text):
            continue
        printable = sum(char.isprintable() or char in "\r\n\t" for char in text)
        if printable / len(text) >= 0.85:
            return text
    return ""


@lru_cache(maxsize=1)
def _zstd_decompressors() -> tuple:
    backends = []
    try:
        import zstd

        backends.append(zstd.decompress)
    except ImportError:
        pass
    try:
        import zstandard

        backends.append(zstandard.ZstdDecompressor().decompress)
    except ImportError:
        pass
    try:
        import pyzstd

        backends.append(pyzstd.decompress)
    except ImportError:
        pass

    return tuple(backends)


def _decompress_zstd(data: bytes) -> bytes:
    backends = _zstd_decompressors()

    error = None
    for decompress in backends:
        try:
            return decompress(data)
        except Exception as exc:
            error = exc
    if error is not None:
        raise error
    raise RuntimeError("zstd decompressor is unavailable")


def has_zstd_decoder() -> bool:
    return bool(_zstd_decompressors())


def decode_message_content(content: str | bytes | bytearray | None, *, strip_sender_prefix=True) -> str:
    """Decode a message, with legacy sender-prefix handling by default."""
    compressed = False
    if content is None:
        return ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, (bytes, bytearray)):
        raw = bytes(content)
        compressed = raw.startswith(_ZSTD_MAGIC)
        if compressed:
            try:
                raw = _decompress_zstd(raw)
            except Exception:
                text = extract_text_from_blob(raw)
            else:
                text = raw.decode("utf-8", errors="replace")
        else:
            text = raw.decode("utf-8", errors="replace")
    else:
        text = str(content)

    prefix = _SENDER_PREFIX.match(text) if strip_sender_prefix else None
    if prefix and (compressed or prefix.group(0).startswith("wxid_")):
        text = text[prefix.end():]
    return text


def decode_message_hex(value: str | None, *, strip_sender_prefix=True) -> str:
    """Decode a hexadecimal ``message_content`` value."""
    if not value:
        return ""
    return decode_message_content(bytes.fromhex(value), strip_sender_prefix=strip_sender_prefix)


_my_sender_id_cache: int | None = None
_my_sender_id_detected: bool = False


def detect_my_sender_id(db_path: str) -> int | None:
    """Detect the real_sender_id that represents 'me' (the account owner).

    The 'me' sender_id appears in the majority of chat tables — it's the sender
    that shows up most consistently across tables, not necessarily all tables
    (some chats may be receive-only).
    """
    tables_raw = db.query_raw(
        db_path, "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%';"
    )
    msg_tables = [t.strip() for t in tables_raw if t.strip().startswith("Msg_")]

    if not msg_tables:
        return None

    # Count how many tables each sender_id appears in
    from collections import Counter
    sender_table_count = Counter()
    for table in msg_tables:
        rows = db.query(
            db_path,
            f"SELECT DISTINCT real_sender_id FROM {table} "
            f"WHERE local_type NOT IN (10000, 10002) LIMIT 20;",
        )
        ids = {int(r.get("real_sender_id", 0)) for r in rows if r.get("real_sender_id")}
        ids.discard(0)
        for sid in ids:
            sender_table_count[sid] += 1

    if not sender_table_count:
        return None

    # The 'me' sender is the one appearing in the most tables
    top = sender_table_count.most_common(1)
    if not top:
        return None

    winner_id, winner_count = top[0]
    threshold = max(len(msg_tables) * 0.4, 2)
    if winner_count >= threshold:
        return winner_id

    return None


def get_my_sender_id() -> int | None:
    """Get the cached 'my' sender_id, detecting on first call."""
    global _my_sender_id_cache, _my_sender_id_detected
    if _my_sender_id_detected:
        return _my_sender_id_cache
    dbs = db.get_message_dbs()
    if dbs:
        _my_sender_id_cache = detect_my_sender_id(dbs[0])
    _my_sender_id_detected = True
    return _my_sender_id_cache


def is_my_message(real_sender_id: str | int) -> bool:
    """Check if a message was sent by 'me' based on real_sender_id."""
    my_id = get_my_sender_id()
    if my_id is None:
        return False
    try:
        return int(real_sender_id) == my_id
    except (ValueError, TypeError):
        return False


def format_time(ts: str | int) -> str:
    """Format unix timestamp to readable string."""
    try:
        t = int(ts)
        if t > 0:
            return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OSError):
        pass
    return "未知时间"


def normalize_type(raw_type: str) -> str:
    """Normalize local_type for MSG_TYPES lookup.

    WeChat Mac uses high bits for subtypes; extract low 16 bits.
    """
    try:
        return str(int(raw_type) & 0xFFFF) if raw_type else ""
    except (ValueError, TypeError):
        return raw_type
