# -*- coding: utf-8 -*-
"""多 LVA 配置解析测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from app.config import load_config


class TestMultiLvaConfig(unittest.TestCase):
    def test_parses_multiple_targets(self):
        with tempfile.TemporaryDirectory() as d:
            env = {
                "LVA_URLS": "ws://lva1:10700/api/peripheral,ws://lva2:10700/api/peripheral",
                "LVA_NAMES": "kws-1,kws-2",
                "LVA_UUID_FILES": f"{d}/uuid1,{d}/uuid2",
                "LVA_KEYWORDS": "你好小智|小智小智,你好同学",
                "LVA_AUDIO_SOURCES": "pulse:mic1|alsa:plughw:2,0",
                "LVA_PROTOCOL": "protobuf",
                "LVA_PROTOCOLS": "json|protobuf",
                "KEYWORDS": "默认词",
                "KWS_FAKE": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                cfg = load_config()
            self.assertEqual(len(cfg.lva_targets), 2)
            self.assertEqual(cfg.lva_targets[0].url, "ws://lva1:10700/api/peripheral")
            self.assertEqual(cfg.lva_targets[0].device_name, "kws-1")
            self.assertEqual(cfg.lva_targets[0].keywords, ["你好小智"])
            self.assertEqual(cfg.lva_targets[1].keywords, ["小智小智", "你好同学"])
            self.assertEqual(cfg.lva_targets[0].audio_source, "pulse:mic1")
            self.assertEqual(cfg.lva_targets[1].audio_source, "alsa:plughw:2,0")
            self.assertEqual(cfg.lva_targets[0].protocol, "json")
            self.assertEqual(cfg.lva_targets[1].protocol, "protobuf")
            self.assertTrue(cfg.lva_targets[0].device_uuid)
            self.assertNotEqual(cfg.lva_targets[0].device_uuid, cfg.lva_targets[1].device_uuid)

    def test_falls_back_to_single_lva_variables(self):
        with tempfile.TemporaryDirectory() as d:
            env = {
                "LVA_URL": "ws://single:10700/api/peripheral",
                "DEVICE_NAME": "single-kws",
                "DEVICE_UUID_FILE": f"{d}/uuid",
                "KEYWORDS": "你好小智",
            }
            with patch.dict(os.environ, env, clear=False):
                cfg = load_config()
            self.assertEqual(len(cfg.lva_targets), 1)
            self.assertEqual(cfg.lva_targets[0].url, "ws://single:10700/api/peripheral")
            self.assertEqual(cfg.lva_targets[0].device_name, "single-kws")
            self.assertEqual(cfg.lva_targets[0].keywords, ["你好小智"])
            self.assertEqual(cfg.lva_targets[0].audio_source, cfg.audio_source)


if __name__ == "__main__":
    unittest.main()
