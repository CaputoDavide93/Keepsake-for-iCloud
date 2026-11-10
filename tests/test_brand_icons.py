"""The brand: the page's icons are served from the page's own origin with the right type.

The logo in the top bar is inline in index.html (no request), and the tab icon is the
same artwork: an SVG, a 32 px PNG for browsers that ignore SVG icons, and a full-bleed
180 px PNG for the iOS home screen. All three go through the same static route as the
page itself, so the content security policy (img-src 'self') allows them and nothing
is fetched from another site.
"""

from __future__ import annotations

import re
import struct
import unittest
from pathlib import Path

from tests.test_api import ApiBase

WEB = Path(__file__).resolve().parents[1] / "src" / "keepsake" / "web"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def png_size(body: bytes):
    """(width, height) from the IHDR chunk of a PNG."""
    return struct.unpack(">II", body[16:24])


class BrandIcons(ApiBase):
    def test_icon_svg_is_served_as_svg(self):
        status, body, headers = self.c.call("GET", "/favicon.svg")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("image/svg+xml"), headers["Content-Type"])
        self.assertIn(b"<svg", body)

    def test_icon_pngs_are_served_as_png_at_their_sizes(self):
        for path, size in (("/favicon-32.png", (32, 32)), ("/touch-icon.png", (180, 180))):
            status, body, headers = self.c.call("GET", path)
            self.assertEqual(status, 200, path)
            self.assertEqual(headers["Content-Type"], "image/png", path)
            self.assertEqual(body[:8], PNG_MAGIC, path)
            self.assertEqual(png_size(body), size, path)
            self.assertIn("img-src 'self'", headers["Content-Security-Policy"])

    def test_index_links_every_icon_and_the_brand_is_one_link_to_folders(self):
        status, body, _ = self.c.call("GET", "/")
        self.assertEqual(status, 200)
        page = body.decode()
        for href in ("/favicon.svg", "/favicon-32.png", "/touch-icon.png"):
            self.assertIn(f'href="{href}"', page, href)
        brand = re.search(r'<a class="brand"[^>]*>(.*?)</a>', page, re.S)
        self.assertIsNotNone(brand)
        self.assertIn('href="#/folders"', brand.group(0))
        self.assertIn('aria-label="Keepsake, Folders"', brand.group(0))
        self.assertIn("<svg", brand.group(1))          # the logo is inline, not another request
        self.assertIn('aria-hidden="true"', brand.group(1))
        self.assertIn("Keepsake</span>", brand.group(1))
        self.assertNotIn("<img", brand.group(1))

    def test_icon_the_logo_is_the_repos_own_artwork(self):
        svg = (WEB / "favicon.svg").read_text()
        page = (WEB / "index.html").read_text()
        # the same folder-with-a-check paths are in the tab icon and in the top bar
        for path in re.findall(r' d="([^"]+)"', svg):
            self.assertIn(f' d="{path}"', page)


if __name__ == "__main__":
    unittest.main()
