"""阶段 2 静态资源边界与页面交付约束。"""

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


class AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.resources = []
        self.inline_handlers = []
        self.ids = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "id" in attributes:
            self.ids.append(attributes["id"])
        for name, value in attrs:
            if name.startswith("on"):
                self.inline_handlers.append(name)
            if name == "src" or (tag == "link" and name == "href"):
                self.resources.append(value)


class FrontendDeliveryTests(unittest.TestCase):
    def test_resources_are_local_and_event_bindings_are_separate(self):
        root = Path(__file__).resolve().parent.parent / "templates"
        parser = AssetParser()
        parser.feed((root / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(len(parser.ids), len(set(parser.ids)), "页面含重复 ID")
        self.assertEqual(parser.inline_handlers, [])
        self.assertIn("/assets/app.css", parser.resources)
        self.assertIn("/assets/app.js", parser.resources)
        self.assertTrue(
            all(url.startswith(("/assets/", "data:")) for url in parser.resources)
        )
        css = (root / "app.css").read_text(encoding="utf-8")
        self.assertNotRegex(css, r"@import|url\(\s*['\"]?https?://")
        self.assertNotIn("gradient(", css)
        self.assertIn("tabular-nums", css)
        self.assertIn('"Microsoft YaHei"', css)

    def test_application_has_no_static_frontend_package_imports(self):
        script = (
            Path(__file__).resolve().parent.parent / "templates" / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"\b(?:require|import)\s*\(", script))
