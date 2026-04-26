import asyncio
import sys
import unittest
from pathlib import Path

from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app


class ProxyRewriteTests(unittest.TestCase):
    def test_proxy_target_url_keeps_dash_template_tokens(self):
        self.assertTrue(callable(getattr(app, "_proxy_target_url", None)))

        proxied = app._proxy_target_url(
            "https://cdn.example.com/live/chunk-$Number$.m4s?token=a%2Fb&x=1",
            "cricfy:test:id",
        )

        self.assertIn("/proxy/media?id=cricfy%3Atest%3Aid&url=", proxied)
        self.assertIn("chunk-$Number$.m4s", proxied)
        self.assertIn("token%3Da%2Fb%26x%3D1", proxied)

    def test_rewrite_hls_manifest_proxies_segments_and_key_uri(self):
        manifest = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-KEY:METHOD=AES-128,URI=\"key.key\"
#EXTINF:5.0,
segment1.ts
sub/playlist.m3u8
"""

        rewritten = asyncio.run(
            app._rewrite_hls_manifest(
                manifest,
                "https://origin.example.com/live/master.m3u8",
                "cricfy:test:id",
            )
        )

        self.assertIn("/proxy/media?id=cricfy%3Atest%3Aid&url=https://origin.example.com/live/segment1.ts", rewritten)
        self.assertIn("URI=\"/proxy/media?id=cricfy%3Atest%3Aid&url=https://origin.example.com/live/key.key\"", rewritten)
        self.assertIn("/proxy/media?id=cricfy%3Atest%3Aid&url=https://origin.example.com/live/sub/playlist.m3u8", rewritten)

    def test_rewrite_dash_manifest_proxies_segment_urls(self):
        self.assertTrue(callable(getattr(app, "_rewrite_mpd_manifest", None)))

        mpd = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<MPD xmlns=\"urn:mpeg:dash:schema:mpd:2011\">
  <Period>
    <AdaptationSet>
      <Representation id=\"1\" bandwidth=\"1000\">
        <BaseURL>video/</BaseURL>
        <SegmentTemplate initialization=\"init-$RepresentationID$.mp4\" media=\"chunk-$Number$.m4s\" />
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""

        rewritten = app._rewrite_mpd_manifest(
            mpd,
            "https://cdn.example.com/root/manifest.mpd",
            "cricfy:test:id",
        )

        self.assertIn("/proxy/media?id=cricfy%3Atest%3Aid&amp;url=https://cdn.example.com/root/video/init-$RepresentationID$.mp4", rewritten)
        self.assertIn("/proxy/media?id=cricfy%3Atest%3Aid&amp;url=https://cdn.example.com/root/video/chunk-$Number$.m4s", rewritten)

    def test_forward_request_headers_does_not_forward_origin(self):
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/proxy/media",
            "headers": [
                (b"origin", b"http://127.0.0.1:8000"),
                (b"range", b"bytes=0-99"),
                (b"accept", b"*/*"),
                (b"accept-encoding", b"gzip"),
            ],
        }
        request = Request(scope)

        forwarded = app._forward_request_headers(request)

        self.assertEqual(forwarded.get("range"), "bytes=0-99")
        self.assertEqual(forwarded.get("accept"), "*/*")
        self.assertNotIn("origin", forwarded)
        self.assertNotIn("accept-encoding", forwarded)

    def test_segment_request_headers_are_minimal(self):
        """For media segments (is_segment=True), only forward Range and channel headers."""
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/proxy/media",
            "headers": [
                (b"origin", b"http://127.0.0.1:8000"),
                (b"range", b"bytes=0-99"),
                (b"accept", b"*/*"),
                (b"accept-language", b"en-US"),
                (b"accept-encoding", b"gzip"),
                (b"user-agent", b"Mozilla/5.0"),
            ],
        }
        request = Request(scope)
        channel_headers = {
            "Cookie": "__hdnea__=st=1234",
            "User-Agent": "Channel-UA",
        }

        # When is_segment=True, should NOT forward accept, accept-language, user-agent from player
        forwarded = app._forward_request_headers(request, channel_headers, is_segment=True)

        # Channel headers win (dict keys are case-sensitive)
        self.assertEqual(forwarded.get("Cookie"), "__hdnea__=st=1234")
        self.assertEqual(forwarded.get("User-Agent"), "Channel-UA")
        # Range should be forwarded (for seek operations)
        self.assertEqual(forwarded.get("range"), "bytes=0-99")
        # But not these
        self.assertNotIn("accept", forwarded)
        self.assertNotIn("accept-language", forwarded)
        self.assertNotIn("origin", forwarded)

    def test_manifest_request_headers_are_normal(self):
        """For manifest requests (is_segment=False), forward more headers like Accept."""
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/proxy/media",
            "headers": [
                (b"accept", b"application/dash+xml"),
                (b"accept-language", b"en-US"),
                (b"range", b"bytes=0-99"),
            ],
        }
        request = Request(scope)
        channel_headers = {
            "User-Agent": "Channel-UA",
        }

        # When is_segment=False (manifest), forward Accept and Accept-Language
        forwarded = app._forward_request_headers(request, channel_headers, is_segment=False)

        self.assertEqual(forwarded.get("accept"), "application/dash+xml")
        self.assertEqual(forwarded.get("accept-language"), "en-US")
        self.assertEqual(forwarded.get("range"), "bytes=0-99")
        self.assertEqual(forwarded.get("User-Agent"), "Channel-UA")


if __name__ == "__main__":
    unittest.main()
