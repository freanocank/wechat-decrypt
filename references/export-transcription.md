# Export and voice transcription

Read this file for chat exports, local workflow handoffs, or voice transcription.

## Privacy and scope

Export only the requested contact and date range. If matching returns multiple plausible contacts, stop and ask the user to select one. Output files and voice caches contain private conversation data; keep them local and do not attach or upload them unless the user explicitly requests that destination.

## Export

```bash
"$PYTHON" "$SKILL_DIR/scripts/common/export_chat.py" CONTACT --year 2026 -o /path/to/chat.txt
"$PYTHON" "$SKILL_DIR/scripts/common/export_chat.py" CONTACT --start 2026-01-01 --end 2026-06-30 -o /path/to/chat.txt
```

Omitting dates exports all available history. The exporter merges message shards, orders messages chronologically, and writes the output with private permissions.

## Image manifest and transcript links

Add `--with-media` to associate image messages with the local resource index and
export only images belonging to the selected conversation and date window:

```bash
"$PYTHON" "$SKILL_DIR/scripts/common/export_chat.py" CONTACT --year 2026 --with-media --no-transcribe -o /path/to/chat.html
```

This creates `chat.html`, `chat.media.json` and a fresh `chat.assets-<export-id>/`
directory named in the manifest's `assets_directory` field. Repeated exports keep
their assets separate, so a new date window does not inherit older images. TXT shows relative
paths, Markdown and HTML preview supported plaintext images. Format is inferred
from `.md`/`.html` or selected with `--format txt|markdown|html`. Keep the three
outputs together when moving them. The manifest explicitly distinguishes resolved,
missing and encrypted/unsupported images. Encrypted originals are not decoded.
The initial adapter covers images; video/file/voice messages keep their existing
export behavior. See [the format and supported index profile](../docs/media-manifest-proposal.md).

## Structured JSON and saved-image HTML

Set `--format json` with an explicit `.json` output for structured records. Use `--media-map` with JSON or HTML to include images saved from the client. `--with-media` uses the local index workflow described above and is separate from the saved-image workflow. Keep the date range small and select a unique contact or group.

```bash
"$PYTHON" "$SKILL_DIR/scripts/common/export_chat.py" GROUP --start 2026-11-24 --end 2026-11-24 --no-transcribe --format json -o ./chat.json
"$PYTHON" "$SKILL_DIR/scripts/common/export_chat.py" GROUP --start 2026-11-24 --end 2026-11-24 --no-transcribe --format html --media-map ./image-map.json -o ./chat.html
```

JSON includes the conversation, the inclusive start and exclusive end, and each message's ID, timestamp with timezone offset, sender, type, full decoded text, and attachment slots. Replies keep the current `body` and the original `quote` separate. Recognized merged-forward records include their original display time and sender label where available. IDs recur across exports of the same account and conversation. Messages with no server ID use a shard and local ID fallback, so those IDs may change after moving a message to a different shard. The JSON does not include the raw source IDs as metadata or the original database paths.

The exporter assigns attachment IDs to direct image messages and identifiable image items in merged forwards. To add pictures, first export JSON, save the images you need from the normal WeChat client, then make a JSON object mapping each selected attachment ID to its saved image path. Relative paths are resolved from the mapping file. The exporter does not infer a mapping from a name or time.

```json
{
  "attachment_ID_FROM_JSON": "saved-images/report.png"
}
```

A single mapped local image with a JPEG, PNG, GIF or WebP signature is copied into the new export's `<stem>.assets-<id>/` directory. This validates the signature; the browser decodes the image when displaying it. Missing or unreadable inputs remain visible as placeholders, and a path array with multiple distinct candidates is marked `ambiguous`. The JSON records the status and reason. Out-of-range mapping entries are counted without opening their files. Images saved from WeChat or supplied by the user are accepted as provided. The exporter does not retrieve remote assets or decode encrypted caches.

HTML displays local images inline and links to its own `<stem>.media-<id>.json` attachment manifest. Keep the HTML, the linked manifest and its assets directory together when moving the export. JSON mode places the attachment manifest in the message records and only creates an assets directory if there are mapped images. Each run uses a fresh directory for its assets.

## Local workflow handoff

1. Resolve the requested group and export a separate JSON file for each group and date range. Use explicit `--start` and `--end` and `--no-transcribe` for text and image-metadata collection. Both date arguments are inclusive calendar dates in the exporting machine's local timezone. Use the offsets in `sent_at` when assigning dates in the downstream workflow. If group matching is ambiguous, resolve the group before exporting.
2. Give the downstream Codex or Hermes workflow the local path to the scoped `--format json` export. It provides stable message IDs, a date-bounded conversation, and separate quote and attachment records. Keep raw databases, device keys, caches, and original saved images on the source machine. Pass the selected JSON and any explicitly needed copies of mapped images.
3. Use `conversation.id` and each `messages[].id` for source traceability and repeat detection. Use `sender.id` when distinguishing participants with the same display name. For a reply, `body` is the current message and `quote.body` belongs to the earlier message.
4. An image slot records that the message contains an image. Use the `status` and relative `path` to tell whether the export includes a mapped image. If image content is needed, save the selected image from the WeChat client and map that file to its attachment ID before exporting again. Leave missing or ambiguous images for review. The downstream workflow owns report interpretation, previews, and any separately authorized write.

## Voice model consent

Check both `voice-backend` and `voice-model` in `doctor.py --json` before any export that may include audio.

- Both are `ok`: transcription may run automatically unless the user asks to preserve `[Audio]`; use `--no-transcribe` to disable it.
- `voice-backend` is `warn`: install the optional local stack with the platform setup flag shown by doctor. This installs sizeable libraries but not the model.
- `voice-model` is `warn`: ordinary export leaves `[Audio]` and does not download a model. Ask whether the user wants the approximately 3 GB large-v3 download.
- User approves: rerun with `--transcribe`; the first run downloads the platform model.
- User declines: use `--no-transcribe`.

macOS uses `mlx-whisper`; Windows uses `faster-whisper` CPU int8. Both read SILK v3 blobs from `VoiceInfo.voice_data`, decode locally, and align transcripts through `svr_id == server_id`.

To transcribe independently or reuse a prior map:

```bash
"$PYTHON" "$SKILL_DIR/scripts/common/transcribe_db.py" CONTACT -o /path/to/voice-map.json
"$PYTHON" "$SKILL_DIR/scripts/common/export_chat.py" CONTACT --voice-map /path/to/voice-map.json -o /path/to/chat.txt
```

The persistent `voice_cache.json` avoids repeat transcription and is written with private permissions. Messages whose audio was never downloaded locally remain `[Audio]`.
