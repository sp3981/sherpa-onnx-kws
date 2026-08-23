# -*- coding: utf-8 -*-
"""多 stream KWS 接口测试（FakeSpotter，不依赖 sherpa-onnx）。"""

from __future__ import annotations

import unittest

from app.kws import FakeSpotter


class TestFakeSpotterMultiStream(unittest.TestCase):
    def test_streams_are_independent(self):
        spotter = FakeSpotter(["你好小智"], trigger_every=1, min_interval_s=0)
        s1 = spotter.create_stream()
        s2 = spotter.create_stream()

        # 两个 stream 各自都能命中
        self.assertEqual(spotter.accept(s1, b"\x00" * 3200), "你好小智")
        self.assertEqual(spotter.accept(s2, b"\x00" * 3200), "你好小智")

        # 命中 s1 不影响 s2 的下一次计数
        self.assertEqual(spotter.accept(s1, b"\x00" * 3200), "你好小智")
        self.assertEqual(spotter.accept(s2, b"\x00" * 3200), "你好小智")

    def test_unknown_stream_returns_none(self):
        spotter = FakeSpotter(["你好小智"], trigger_every=1, min_interval_s=0)
        self.assertIsNone(spotter.accept(999, b"\x00" * 3200))


if __name__ == "__main__":
    unittest.main()
