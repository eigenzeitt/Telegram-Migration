"""Offline regression tests for telegram_migration.py.

These tests never sign in to Telegram and never call the live API. They use
real Telethon entity objects when dependencies are installed by requirements.txt.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lxml import etree
from telethon.tl import types as tl

import telegram_migration as migration


class TextAndEntityTests(unittest.TestCase):
    def test_utf16_length_counts_non_bmp_as_two_units(self):
        self.assertEqual(migration.u16len("A😀B"), 4)

    def test_sanitize_text_normalizes_newlines_and_removes_controls(self):
        self.assertEqual(migration.sanitize_text("a\r\nb\rc\x00\x07d"), "a\nb\ncd")

    def test_rich_text_preserves_italics_bold_link_and_line_break(self):
        root = etree.fromstring(
            '<div>plain <i>italic</i> and <b>bold</b> '
            '<a href="https://example.test/path">site</a><br/>end</div>'
        )
        text, entities = migration.parse_rich_text(root)
        self.assertEqual(text, "plain italic and bold site\nend")
        by_type = {type(entity).__name__: entity for entity in entities}
        self.assertIn("MessageEntityItalic", by_type)
        self.assertIn("MessageEntityBold", by_type)
        self.assertIn("MessageEntityTextUrl", by_type)
        italic = by_type["MessageEntityItalic"]
        bold = by_type["MessageEntityBold"]
        link = by_type["MessageEntityTextUrl"]
        self.assertEqual((italic.offset, italic.length), (6, 6))
        self.assertEqual((bold.offset, bold.length), (17, 4))
        self.assertEqual(link.url, "https://example.test/path")
        self.assertEqual(text[link.offset:link.offset + link.length], "site")

    def test_rich_text_measures_offsets_in_utf16_units(self):
        root = etree.fromstring("<div>😀<i>x</i></div>")
        text, entities = migration.parse_rich_text(root)
        self.assertEqual(text, "😀x")
        italic = next(e for e in entities if isinstance(e, tl.MessageEntityItalic))
        self.assertEqual((italic.offset, italic.length), (2, 1))

    def test_rich_text_handles_nested_bold_and_italic(self):
        root = etree.fromstring("<div><b>bold <i>both</i></b></div>")
        text, entities = migration.parse_rich_text(root)
        self.assertEqual(text, "bold both")
        bold = next(e for e in entities if isinstance(e, tl.MessageEntityBold))
        italic = next(e for e in entities if isinstance(e, tl.MessageEntityItalic))
        self.assertEqual((bold.offset, bold.length), (0, 9))
        self.assertEqual((italic.offset, italic.length), (5, 4))

    def test_strip_with_entities_rebases_offsets_using_utf16(self):
        entity = tl.MessageEntityItalic(offset=1, length=3)
        text, entities = migration.strip_with_entities(" 😀x ", [entity])
        self.assertEqual(text, "😀x")
        self.assertEqual((entities[0].offset, entities[0].length), (0, 3))

    def test_clean_invalid_entities_drops_invalid_and_clips_overrun(self):
        good = tl.MessageEntityBold(offset=0, length=2)
        overrun = tl.MessageEntityItalic(offset=1, length=8)
        negative = tl.MessageEntityUnderline(offset=-1, length=1)
        empty = tl.MessageEntityStrike(offset=1, length=0)
        outside = tl.MessageEntityCode(offset=8, length=1)
        result = migration.clean_invalid_entities("abc", [good, overrun, negative, empty, outside])
        self.assertEqual([type(item).__name__ for item in result], [type(good).__name__, type(overrun).__name__])
        self.assertEqual((result[0].offset, result[0].length), (0, 2))
        self.assertEqual((result[1].offset, result[1].length), (1, 2))

    def test_compose_message_shifts_body_entity_after_prefix(self):
        body_entity = tl.MessageEntityItalic(offset=0, length=4)
        text, entities = migration.compose_message(
            ("[date]\n", []), ("Alice:\n", []), ("", []), "word", [body_entity]
        )
        self.assertEqual(text, "[date]\nAlice:\nword")
        self.assertEqual((entities[0].offset, entities[0].length), (14, 4))

    def test_split_long_text_respects_limit_and_does_not_split_surrogate_pair(self):
        text = "abcdefgh😀ijklmnopqrs"
        chunks = migration.split_with_entities(text, [], first_limit=9, limit=9)
        pieces = [chunk for chunk, _entities in chunks]
        self.assertGreater(len(pieces), 1)
        self.assertEqual("".join(pieces), text)
        self.assertTrue(all(migration.u16len(piece) <= 9 for piece in pieces))
        self.assertTrue(all("\ud800" not in piece and "\udc00" not in piece for piece in pieces))

    def test_split_clips_entity_offsets_to_each_chunk(self):
        text = "abcdefghijABCDEFGHIJ"
        entity = tl.MessageEntityItalic(offset=2, length=16)
        chunks = migration.split_with_entities(text, [entity], first_limit=10, limit=10)
        self.assertGreaterEqual(len(chunks), 2)
        for piece, entities in chunks:
            for current in entities:
                self.assertGreater(current.length, 0)
                self.assertGreaterEqual(current.offset, 0)
                self.assertLessEqual(current.offset + current.length, migration.u16len(piece))

    def test_entity_error_detection_does_not_match_generic_word_entities(self):
        self.assertTrue(migration._is_entity_error(ValueError("invalid bounds for formatting")))
        self.assertFalse(migration._is_entity_error(RuntimeError("unrelated entities database failure")))

    def test_parse_export_date_accepts_desktop_timezone_suffix(self):
        self.assertEqual(
            migration.parse_export_date("24.10.2026 12:34:56 UTC+03:00"),
            datetime(2026, 10, 24, 12, 34, 56),
        )

    def test_parse_export_date_returns_none_for_invalid_input(self):
        self.assertIsNone(migration.parse_export_date("not a date"))


class ExportParsingTests(unittest.TestCase):
    def test_extract_message_data_reads_sender_time_text_and_reply(self):
        html = '''<div class="message default clearfix" id="message123">
          <div class="body">
            <div class="pull_right date details" title="24.10.2026 12:34:56 UTC+03:00">12:34</div>
            <div class="from_name">Alice</div>
            <div class="reply_to details"><a href="#go_to_message122">this message</a></div>
            <div class="text">hello <i>world</i></div>
          </div>
        </div>'''
        elem = etree.fromstring(html)
        with patch.object(migration, "BASE_DIR", tempfile.gettempdir()):
            data = migration.extract_message_data(elem)
        self.assertEqual(data["msg_id"], "123")
        self.assertEqual(data["sender"], "Alice")
        self.assertEqual(data["timestamp"], datetime(2026, 10, 24, 12, 34, 56))
        self.assertEqual(data["body_text"], "hello world")
        self.assertEqual(data["reply_src_id"], "122")
        self.assertTrue(any(isinstance(e, tl.MessageEntityItalic) for e in data["body_entities"]))

    def test_extract_message_data_skips_service_records(self):
        elem = etree.fromstring('<div class="message service" id="message-123"><div>joined</div></div>')
        self.assertIsNone(migration.extract_message_data(elem))

    def test_extract_message_data_rejects_bad_message_id(self):
        elem = etree.fromstring('<div class="message default" id="messageabc"><div class="text">hi</div></div>')
        self.assertIsNone(migration.extract_message_data(elem))

    def test_collect_source_ids_ignores_service_message_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            page = Path(temp) / "messages.html"
            page.write_text(
                '<div id="message1"></div><div id="message-2"></div>'
                '<div id=\'message30\'></div>', encoding="utf-8"
            )
            self.assertEqual(migration.collect_source_ids([str(page)]), {"1", "30"})

    def test_export_files_orders_split_html_pages_numerically(self):
        with tempfile.TemporaryDirectory() as temp:
            for name in ("messages10.html", "messages2.html", "messages.html", "not-messages.html"):
                (Path(temp) / name).write_text("", encoding="utf-8")
            with patch.object(migration, "BASE_DIR", temp):
                names = [Path(p).name for p in migration.export_files()]
            self.assertEqual(names, ["messages.html", "messages2.html", "messages10.html"])


class MediaTests(unittest.TestCase):
    def test_photo_and_document_classification_distinguishes_path_from_extension(self):
        self.assertEqual(migration._classify("files/scan.png", "media_wrap", "/tmp/scan.png", (), ""), "document")
        self.assertEqual(migration._classify("photos/photo.jpg", "media_wrap", "/tmp/photo.jpg", (), ""), "photo")
        self.assertEqual(migration._classify("image.png", "media_wrap", "/tmp/image.png", ("photo",), ""), "photo")

    def test_sticker_subtypes_are_classified_by_extension(self):
        self.assertEqual(migration._classify("stickers/a.webp", "media_wrap", "/tmp/a.webp"), "sticker_static")
        self.assertEqual(migration._classify("stickers/a.tgs", "media_wrap", "/tmp/a.tgs"), "sticker_animated")
        self.assertEqual(migration._classify("stickers/a.webm", "media_wrap", "/tmp/a.webm"), "sticker_video")

    def test_gif_classification_requires_explicit_evidence(self):
        self.assertEqual(migration._classify("video_files/a.gif", "media_wrap", "/tmp/a.gif"), "gif")
        self.assertEqual(migration._classify("video_files/a.mp4", "media_wrap", "/tmp/a.mp4", (), ""), "video")
        self.assertEqual(migration._classify("video_files/a.mp4", "media_wrap", "/tmp/a.mp4", (), "Animation, 00:04"), "gif")
        self.assertFalse(migration._is_gif("/tmp/my GIF collection.mp4", (), "ordinary video"))

    def test_voice_and_round_video_classification(self):
        self.assertEqual(migration._classify("voice_messages/a.ogg", "media_wrap", "/tmp/a.ogg"), "voice")
        self.assertEqual(migration._classify("round_video_messages/a.mp4", "media_wrap", "/tmp/a.mp4"), "round_video")

    def test_find_media_uses_export_root_and_finds_real_file(self):
        with tempfile.TemporaryDirectory() as temp:
            photos = Path(temp) / "photos"
            photos.mkdir()
            photo = photos / "photo.jpg"
            photo.write_bytes(b"fake image bytes")
            elem = etree.fromstring(
                '<div class="message" id="message1"><div class="media_wrap">'
                '<a href="photos/photo.jpg"><img class="photo" src="photos/photo_thumb.jpg"/></a>'
                '</div></div>'
            )
            with patch.object(migration, "BASE_DIR", temp):
                media = migration.find_media(elem)
            self.assertEqual(media, [(str(photo), "photo")])

    def test_album_compatibility_only_allows_supported_media_groups(self):
        self.assertTrue(migration._album_compatible([("a", "photo"), ("b", "video")]))
        self.assertTrue(migration._album_compatible([("a", "document"), ("b", "document")]))
        self.assertFalse(migration._album_compatible([("a", "photo"), ("b", "document")]))
        self.assertFalse(migration._album_compatible([("a", "sticker_static"), ("b", "photo")]))

    def test_upload_size_guard_only_flags_files_strictly_over_limit(self):
        with patch.object(migration, "MAX_UPLOAD_MB", 1):
            with patch.object(migration.os.path, "getsize", side_effect=lambda path: {
                "exact": 1024 * 1024,
                "over": 1024 * 1024 + 1,
                "under": 1024 * 1024 - 1,
            }[path]):
                found = migration.find_oversize_media([("exact", "document"), ("over", "document"), ("under", "photo")])
        self.assertEqual(found, [("over", "document", 1024 * 1024 + 1)])

    def test_sticker_upload_options_set_document_and_format_mime(self):
        options = migration._sticker_upload_options("sticker.webp", "sticker_static")
        self.assertTrue(options["force_document"])
        self.assertEqual(options["mime_type"], "image/webp")
        self.assertEqual(len(options["attributes"]), 1)
        self.assertIsInstance(options["attributes"][0], tl.DocumentAttributeSticker)

    def test_video_sticker_metadata_is_used_when_available(self):
        with patch.object(migration, "_probe_video_metadata", return_value={"width": 256, "height": 128, "duration": 2.5}):
            options = migration._sticker_upload_options("sticker.webm", "sticker_video")
        self.assertEqual(options["mime_type"], "video/webm")
        self.assertTrue(options["nosound_video"])
        video = next(a for a in options["attributes"] if isinstance(a, tl.DocumentAttributeVideo))
        self.assertEqual((video.w, video.h, video.duration), (256, 128, 2.5))


class StateAndAuditTests(unittest.TestCase):
    def test_id_map_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "id-map.jsonl")
            with patch.object(migration, "IDMAP_FILE", path):
                migration.append_id_map("10", 501)
                migration.append_id_map("11", 502)
                self.assertEqual(migration.load_id_map(), {"10": 501, "11": 502})

    def test_partial_send_state_round_trip_and_merges_latest_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "partial.jsonl")
            with patch.object(migration, "PARTIAL_FILE", path):
                migration.save_state("10", {"src": "10", "text_prog": {"done": 1, "first": 90, "last": 90}})
                migration.save_state("10", {"src": "10", "text_prog": {"done": 2, "first": 90, "last": 91}, "media_id": 92})
                state = migration.load_partial()["10"]
            self.assertEqual(state["text_prog"]["done"], 2)
            self.assertEqual(state["text_prog"]["last"], 91)
            self.assertEqual(state["media_id"], 92)
            self.assertNotIn("src", state)

    def test_pending_reply_file_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "pending.json")
            with patch.object(migration, "PENDING_REPLIES_FILE", path):
                self.assertTrue(migration.save_pending_replies({"20": "19", "21": "18"}))
                self.assertEqual(migration.load_pending_replies(), {"20": "19", "21": "18"})
                self.assertFalse(Path(path + ".tmp").exists())

    def test_manual_upload_ledger_reads_mapping_and_marks_once(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "manual.txt")
            with patch.object(migration, "MANUAL_UPLOADS_FILE", path):
                Path(path).write_text("# comment\n123 456 # user uploaded\n", encoding="utf-8")
                done, mapped = migration.load_manual_uploads()
                self.assertEqual(done, {"123"})
                self.assertEqual(mapped, {"123": 456})
                self.assertTrue(migration.mark_manual_upload("124", ["files/large.bin"]))
                self.assertTrue(migration.mark_manual_upload("124", ["files/large.bin"]))
                done, _ = migration.load_manual_uploads()
            self.assertEqual(done, {"123", "124"})

    def test_failure_log_flattens_embedded_newlines(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "failed.txt")
            with patch.object(migration, "FAILED_FILE", path):
                migration.log_failure("55", "bad\nmessage\twith whitespace")
            self.assertEqual(Path(path).read_text(encoding="utf-8"), "55\tbad message with whitespace\n")

    def test_manual_upload_instruction_includes_file_and_source_id(self):
        data = {"timestamp": datetime(2026, 10, 24, 12, 0), "sender": "Alice", "media": [("/tmp/video.mp4", "video")]}
        text = migration.format_oversize_instructions("77", data, "caption", [("/tmp/video.mp4", "video", 3 * 1024 * 1024)], 500, 20, auto_marked=True)
        self.assertIn("message id=77", text)
        self.assertIn(os.path.abspath("/tmp/video.mp4"), text)
        self.assertIn("caption", text)
        self.assertIn("upload the file(s) now", text.lower())


class ReactionMetadataTests(unittest.TestCase):
    def test_normalize_reactions_supports_list_nested_object_and_actor_names(self):
        normalized = migration.normalize_reactions([
            {"emoji": "👍", "count": 3, "recent": [{"username": "alice"}, "Bob", "Bob"]},
            {"❤️": 2},
        ])
        self.assertEqual([item["label"] for item in normalized], ["👍", "❤️"])
        self.assertEqual(normalized[0]["count"], 3)
        self.assertEqual(normalized[0]["actors"], ["alice", "Bob"])
        self.assertEqual(normalized[1]["count"], 2)

    def test_load_json_reactions_matches_source_ids_and_ignores_unrelated_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            payload = {
                "name": "Example export",
                "messages": [
                    {"id": 10, "text": "hello", "reactions": [{"emoji": "👍", "count": 4}]},
                    {"id": 20, "text": "other", "reactions": [{"emoji": "❤️", "count": 9}]},
                ],
            }
            (Path(temp) / "result.json").write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(migration, "BASE_DIR", temp):
                reactions = migration.load_json_reactions([], {"10"})
            self.assertIn("10", reactions)
            self.assertNotIn("20", reactions)
            self.assertEqual(reactions["10"][0]["label"], "👍")
            self.assertEqual(reactions["10"][0]["count"], 4)

    def test_reaction_note_is_explicitly_archival_not_live(self):
        note = migration.format_reaction_archive_note([{"label": "👍", "count": 5, "actors": []}])
        self.assertIn("not live Telegram reaction totals", note)
        self.assertIn("👍 × 5", note)


class AsyncSendTests(unittest.TestCase):
    def test_send_text_chunks_tracks_successful_chunks_and_returns_first_id(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as temp:
                calls = []
                async def send_message(channel, text, **kwargs):
                    calls.append((channel, text, kwargs))
                    return SimpleNamespace(id=100 + len(calls))
                client = SimpleNamespace(send_message=send_message)
                state = {"src": "123"}
                with patch.object(migration, "PARTIAL_FILE", str(Path(temp) / "partial.jsonl")):
                    result = await migration._send_text_chunks(
                        client, "channel", [("first", []), ("second", [])],
                        None, state, "text_prog", persist=True,
                    )
                    saved = migration.load_partial()["123"]
                self.assertEqual(result, 101)
                self.assertEqual([call[1] for call in calls], ["first", "second"])
                self.assertEqual(state["text_prog"]["done"], 2)
                self.assertEqual(state["text_prog"]["last"], 102)
                self.assertEqual(saved["text_prog"]["done"], 2)
                self.assertTrue(all(call[2]["parse_mode"] is None for call in calls))
        asyncio.run(scenario())

    def test_send_text_chunks_skips_already_checkpointed_chunks_on_resume(self):
        async def scenario():
            calls = []
            async def send_message(channel, text, **kwargs):
                calls.append(text)
                return SimpleNamespace(id=101 + len(calls))
            client = SimpleNamespace(send_message=send_message)
            state = {"src": "123", "text_prog": {"done": 2, "first": 50, "last": 51}}
            result = await migration._send_text_chunks(
                client, "channel", [("first", []), ("second", [])],
                None, state, "text_prog", persist=False,
            )
            self.assertEqual(result, 50)
            self.assertEqual(calls, [])
        asyncio.run(scenario())

    def test_document_upload_forces_document_and_disables_reparsing(self):
        async def scenario():
            calls = []
            async def send_file(channel, file, **kwargs):
                calls.append((channel, file, kwargs))
                return SimpleNamespace(id=777)
            client = SimpleNamespace(send_file=send_file)
            migration._UNSUPPORTED_SEND_KWARGS.clear()
            result = await migration._send_media_item(
                client, "channel", "files/scan.png", "document", None,
                caption="a *literal* caption", entities=None,
            )
            self.assertEqual(result.id, 777)
            self.assertEqual(len(calls), 1)
            kwargs = calls[0][2]
            self.assertTrue(kwargs["force_document"])
            self.assertIsNone(kwargs["parse_mode"])
            self.assertEqual(kwargs["caption"], "a *literal* caption")
        asyncio.run(scenario())

    def test_gif_upload_sets_animation_metadata(self):
        async def scenario():
            calls = []
            async def send_file(channel, file, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(id=778)
            client = SimpleNamespace(send_file=send_file)
            migration._UNSUPPORTED_SEND_KWARGS.clear()
            await migration._send_media_item(client, "channel", "animations/a.gif", "gif", None)
            kwargs = calls[0]
            self.assertTrue(kwargs["force_document"])
            self.assertEqual(kwargs["mime_type"], "image/gif")
            self.assertTrue(any(isinstance(attr, tl.DocumentAttributeAnimated) for attr in kwargs["attributes"]))
        asyncio.run(scenario())

    def test_album_send_persists_first_message_id_and_does_not_resend_on_resume(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as temp:
                calls = []
                async def send_file(channel, files, **kwargs):
                    calls.append((channel, files, kwargs))
                    return [SimpleNamespace(id=501), SimpleNamespace(id=502)]
                client = SimpleNamespace(send_file=send_file)
                state = {"src": "77"}
                with patch.object(migration, "PARTIAL_FILE", str(Path(temp) / "partial.jsonl")):
                    result = await migration._do_send(
                        client, "channel", [("caption", [])],
                        [("photos/a.jpg", "photo"), ("photos/b.jpg", "photo")],
                        None, None, state, None,
                    )
                    persisted = migration.load_partial()["77"]
                    result_after_resume = await migration._do_send(
                        client, "channel", [("caption", [])],
                        [("photos/a.jpg", "photo"), ("photos/b.jpg", "photo")],
                        None, None, state, None,
                    )
                self.assertEqual(result, 501)
                self.assertEqual(result_after_resume, 501)
                self.assertEqual(len(calls), 1)
                self.assertEqual(state["album_id"], 501)
                self.assertEqual(persisted["album_id"], 501)
        asyncio.run(scenario())

    def test_video_without_audio_probe_result_is_not_forced_to_silent_video(self):
        async def scenario():
            calls = []
            async def send_file(channel, file, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(id=779)
            client = SimpleNamespace(send_file=send_file)
            migration._UNSUPPORTED_SEND_KWARGS.clear()
            with patch.object(migration, "_video_has_audio", return_value=None):
                await migration._send_media_item(client, "channel", "video_files/clip.mp4", "video", None)
            self.assertNotIn("nosound_video", calls[0])
        asyncio.run(scenario())

    def test_send_file_retries_without_unsupported_optional_keyword(self):
        async def scenario():
            calls = []
            async def send_file(channel, file, **kwargs):
                calls.append(dict(kwargs))
                if "mime_type" in kwargs:
                    raise TypeError("send_file() got an unexpected keyword argument 'mime_type'")
                return "sent"
            client = SimpleNamespace(send_file=send_file)
            migration._UNSUPPORTED_SEND_KWARGS.clear()
            result = await migration._send_file(client, "channel", "file.webp", mime_type="image/webp", caption="x")
            self.assertEqual(result, "sent")
            self.assertEqual(len(calls), 2)
            self.assertIn("mime_type", calls[0])
            self.assertNotIn("mime_type", calls[1])
            self.assertIn("mime_type", migration._UNSUPPORTED_SEND_KWARGS)
        asyncio.run(scenario())

    def test_formatting_fallback_retries_only_without_entities(self):
        async def scenario():
            calls = []
            async def send(**kwargs):
                calls.append(dict(kwargs))
                if len(calls) == 1:
                    raise EntityBoundsError("invalid entity bounds")
                return "sent"
            class EntityBoundsError(Exception):
                pass
            with tempfile.TemporaryDirectory() as temp:
                with patch.object(migration, "FORMAT_DEBUG_FILE", str(Path(temp) / "format.jsonl")):
                    result = await migration._with_formatting_fallback(
                        send, {"formatting_entities": [tl.MessageEntityItalic(offset=0, length=1)]},
                        "x", [tl.MessageEntityItalic(offset=0, length=1)], "1",
                    )
                self.assertEqual(result, "sent")
                self.assertEqual(len(calls), 2)
                self.assertIsNone(calls[1]["formatting_entities"])
                self.assertTrue(Path(temp, "format.jsonl").exists())
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
