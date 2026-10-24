import unittest

from lxml import etree
from telethon.tl import types as tl

import telegram_migration as migration


class MigrationHelperTests(unittest.TestCase):
    def test_utf16_length_counts_non_bmp_codepoints_as_two_units(self):
        self.assertEqual(migration.u16len("A😀B"), 4)

    def test_rich_text_parser_preserves_italic_and_bold_entities(self):
        root = etree.fromstring(
            "<div>plain <i>italic</i> and <b>bold</b></div>"
        )
        text, entities = migration.parse_rich_text(root)
        self.assertEqual(text, "plain italic and bold")
        self.assertTrue(any(isinstance(e, tl.MessageEntityItalic) for e in entities))
        self.assertTrue(any(isinstance(e, tl.MessageEntityBold) for e in entities))

    def test_image_extension_alone_does_not_force_photo_classification(self):
        kind = migration._classify(
            "files/scan.png", "media_wrap", "/tmp/scan.png", ("media_wrap",), ""
        )
        self.assertEqual(kind, "document")

    def test_export_photo_path_classifies_as_photo(self):
        kind = migration._classify(
            "photos/photo_1.jpg", "media_wrap", "/tmp/photo_1.jpg", ("media_wrap",), ""
        )
        self.assertEqual(kind, "photo")

    def test_explicit_gif_extension_classifies_as_gif(self):
        kind = migration._classify(
            "video_files/animation.gif", "media_wrap", "/tmp/animation.gif", ("media_wrap",), ""
        )
        self.assertEqual(kind, "gif")


if __name__ == "__main__":
    unittest.main()
