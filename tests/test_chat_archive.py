import base64
from datetime import datetime
from hashlib import md5
from html import escape
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
from urllib.parse import quote

import pytest


GROUP = "wxid_archive_test@chatroom"
SENDER = "wxid_archive_test_sender"
DISPLAY = "示例 <LQA>"
SENDER_NAME = "示例 <测试员>"
LONG_BODY = "今日完成\n" + "测试记录" * 180 + "\n<script>window.fake = 1</script>\n"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aEkcAAAAASUVORK5CYII="
)


@pytest.fixture
def run_export(win_backend, tmp_path, monkeypatch):
    import config
    import contacts
    import db
    import export_chat

    storage = Path(db.find_data_dir())
    monkeypatch.setattr(config, "CONTACTS_FILE", str(tmp_path / "missing-contact-overrides.json"))
    monkeypatch.setattr(contacts, "_merged_cache", None)
    monkeypatch.setattr(export_chat.message, "has_zstd_decoder", lambda: True)
    with sqlite3.connect(storage / "contact" / "contact.db") as contact_db:
        contact_db.executemany(
            "INSERT INTO contact VALUES (?, '', ?, '', 3)", [(GROUP, DISPLAY), (SENDER, SENDER_NAME)]
        )
    table = "Msg_" + md5(GROUP.encode()).hexdigest()
    timestamp = int(datetime(2026, 11, 24, 12, 30).timestamp())
    record = (
        '<recordinfo><datalist><dataitem datatype="1"><sourcename>转发作者</sourcename>'
        '<sourcetime>2026-11-24 10:30</sourcetime><datadesc>第一行\n第二行</datadesc></dataitem>'
        '<dataitem datatype="2"><sourcename>转发作者</sourcename><sourcetime>2026-11-24 10:31</sourcetime>'
        '<cdndatakey>synthetic-secret-must-not-export</cdndatakey></dataitem></datalist></recordinfo>'
    )
    forward = f"<msg><appmsg><type>19</type><title>合并转发</title><recorditem>{escape(record)}</recorditem></appmsg></msg>"
    reply = (
        "<msg><appmsg><type>57</type><title>这次已复测</title><refermsg>"
        f"<chatusr>{SENDER}</chatusr>"
        "<displayname>示例同事</displayname><content>原请求\n包含上一天</content></refermsg></appmsg></msg>"
    )
    server_seed = 8_100_000_000_000_000_000
    samples = [(1, server_seed + 1, timestamp, 1, f"{SENDER}:\n{LONG_BODY}")]
    samples.extend((n, server_seed + n, timestamp + n, 3, "") for n in range(2, 10))
    samples.extend([
        (10, server_seed + 10, timestamp + 10, (19 << 32) | 49, forward),
        (11, 0, timestamp + 11, (57 << 32) | 49, reply),
        (12, server_seed + 1, timestamp, 1, f"{SENDER}:\n{LONG_BODY}"),
    ])
    with sqlite3.connect(storage / "message" / "message_0.db") as msg_db:
        msg_db.executemany("INSERT INTO Name2Id VALUES (?)", [(GROUP,), (SENDER,)])
        sender_rowid = msg_db.execute("SELECT rowid FROM Name2Id WHERE user_name = ?", (SENDER,)).fetchone()[0]
        msg_db.execute(
            f"CREATE TABLE {table} (local_id INTEGER, server_id INTEGER, create_time INTEGER, "
            "local_type INTEGER, real_sender_id INTEGER, message_content TEXT)"
        )
        msg_db.executemany(
            f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?)",
            [(local_id, server_id, sent_at, kind, sender_rowid, body)
             for local_id, server_id, sent_at, kind, body in samples],
        )

    def run(output, *args):
        monkeypatch.setattr(sys, "argv", [
            "export_chat.py", DISPLAY, "--start", "2026-11-24", "--end", "2026-11-24",
            "--no-transcribe", "-o", str(output), *args,
        ])
        export_chat.main()

    return run


def test_json_preserves_sender_time_body_and_repeatable_slots(run_export, tmp_path):
    first, second = tmp_path / "one.json", tmp_path / "two.json"
    run_export(first, "--format", "json")
    run_export(second, "--format", "json")
    raw = first.read_text(encoding="utf-8")
    export = json.loads(raw)
    assert export == json.loads(second.read_text(encoding="utf-8"))
    assert all(value not in raw for value in (GROUP, SENDER, "8100000000000000001", "message_0.db", "synthetic-secret"))
    messages = export["messages"]
    assert len(messages) == 11
    assert messages[0]["body"] == LONG_BODY
    assert messages[0]["sender"]["name"] == SENDER_NAME
    assert all(row["sender"]["id"] == messages[0]["sender"]["id"] for row in messages)
    assert messages[0]["sent_at"].startswith("2026-11-24T12:30:00")
    assert messages[-1]["type"] == "quote"
    assert messages[-1]["body"] == "这次已复测"
    assert messages[-1]["quote"]["body"] == "原请求\n包含上一天"
    assert messages[-1]["quote"]["sender_name"] == "示例同事"
    forwarded = messages[-2]
    assert forwarded["forwarded_items"][0]["body"] == "第一行\n第二行"
    assert forwarded["attachments"][0]["record_index"] == 2
    assert forwarded["attachments"][0]["source_time"] == "2026-11-24 10:31"
    slots = [attachment for row in messages for attachment in row["attachments"]]
    assert len(slots) == len({slot["id"] for slot in slots}) == 9
    assert all(slot["status"] == "missing" and "path" not in slot for slot in slots)


def test_html_embeds_mapped_images_and_reports_unavailable_slots(run_export, tmp_path):
    source_index, html_path = tmp_path / "index.json", tmp_path / "chat #1.html"
    run_export(source_index, "--format", "json")
    archive = json.loads(source_index.read_text(encoding="utf-8"))
    slots = [attachment for row in archive["messages"] for attachment in row["attachments"]]
    mapped_image = tmp_path / "client saved.png"
    mapped_image.write_bytes(PNG)
    encrypted = tmp_path / "cannot-open.dat"
    encrypted.write_bytes(b"\x07\x08V2" + bytes(15))
    map_path = tmp_path / "image-map.json"
    map_path.write_text(json.dumps({
        slots[0]["id"]: mapped_image.name,
        slots[1]["id"]: [mapped_image.name, str(tmp_path / "unknown.png")],
        slots[2]["id"]: encrypted.name,
        slots[3]["id"]: "absent.png",
        slots[-1]["id"]: str(mapped_image),
        "attachment_other_export": "not-to-be-read.png",
    }), encoding="utf-8")

    run_export(html_path, "--format", "html", "--media-map", str(map_path))
    page = html_path.read_text(encoding="utf-8")
    manifest_file, = tmp_path.glob("chat #1.media-*.json")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert quote(manifest_file.name) in page
    assert manifest["unmatched_mapping_count"] == 1
    rows = manifest["attachments"]
    assert [row["status"] for row in rows] == ["resolved", "ambiguous", "unreadable", *(["missing"] * 5), "resolved"]
    assert len(list((tmp_path / manifest["assets_directory"]).iterdir())) == 2
    assert "&lt;LQA&gt;" in page and "&lt;测试员&gt;" in page
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "未找到图片" in page and "匹配到多个候选图片" in page and "无法读取图片" in page
    assert "client saved.png" not in page and "client saved.png" not in manifest_file.read_text(encoding="utf-8")
    for row in (rows[0], rows[-1]):
        relative_path = row["path"]
        assert f'src="{quote(relative_path)}"' in page
        assert (tmp_path / relative_path).read_bytes() == PNG
        if os.name != "nt":
            assert stat.S_IMODE((tmp_path / relative_path).stat().st_mode) == 0o600
    if os.name != "nt":
        assert stat.S_IMODE(html_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(manifest_file.stat().st_mode) == 0o600
        assert stat.S_IMODE((tmp_path / manifest["assets_directory"]).stat().st_mode) == 0o700


def test_default_txt_and_invalid_mapping(run_export, tmp_path):
    default_path = tmp_path / "legacy.txt"
    run_export(default_path)
    assert default_path.read_text(encoding="utf-8").count("[Image]") == 8
    mapping = tmp_path / "map.json"
    mapping.write_text('{"attachment_id": "first.png", "attachment_id": "second.png"}', encoding="utf-8")
    result = tmp_path / "result.json"
    with pytest.raises(SystemExit) as error:
        run_export(result, "--format", "json", "--media-map", str(mapping))
    assert error.value.code == 2
    assert not result.exists()


def test_format_routing_preserves_upstream_transcripts(run_export, tmp_path):
    legacy_html, legacy_markdown = tmp_path / "legacy.html", tmp_path / "legacy.md"
    run_export(legacy_html)
    run_export(legacy_markdown)
    assert "<title>WeChat transcript</title>" in legacy_html.read_text(encoding="utf-8")
    assert "> " in legacy_markdown.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("legacy.media*.json"))

    mapping = tmp_path / "map.json"
    mapping.write_text("{}", encoding="utf-8")
    mapped_html = tmp_path / "mapped.html"
    run_export(mapped_html, "--media-map", str(mapping))
    assert "图片清单" in mapped_html.read_text(encoding="utf-8")
    assert len(list(tmp_path.glob("mapped.media-*.json"))) == 1
    with pytest.raises(SystemExit) as error:
        run_export(mapped_html, "--media-map", str(mapping), "--with-media")
    assert error.value.code == 2


def test_structured_text_strips_only_the_resolved_group_envelope(monkeypatch):
    import chat_archive
    import export_chat
    import message

    literal = "Tester: Alice\nToday's test: continuity check"
    frame = b"\x28\xb5\x2f\xfd"
    monkeypatch.setattr(message, "_decompress_zstd", lambda payload: payload[len(frame):])
    monkeypatch.setattr(chat_archive.contacts, "resolve_nickname", lambda _: SENDER_NAME)
    envelope = f"{SENDER}:\n{literal}"
    since = datetime(2026, 11, 24)
    until = datetime(2026, 11, 25)
    base = {"create_time": int(since.timestamp()), "local_type": 1, "sender_wxid": SENDER}
    rows = [
        {**base, "server_id": number, "msg_hex": (frame + value.encode()).hex()}
        for number, value in enumerate((literal, envelope), start=1)
    ]

    group = chat_archive.build_archive(
        rows, account="archive-account", conversation=GROUP, display=f"群聊({GROUP.split('@', 1)[0]})",
        since=since, until=until, voice_map={},
    )
    direct = chat_archive.build_archive(
        rows[1:], account="archive-account", conversation=SENDER, display=SENDER_NAME,
        since=since, until=until, voice_map={},
    )
    assert [row["body"] for row in group["messages"]] == [literal, literal]
    assert group["conversation"]["name"] == "未命名群聊"
    assert direct["messages"][0]["body"] == envelope
    assert export_chat._decode_msg(rows[0]["msg_hex"]) == literal.removeprefix("Tester: ")
