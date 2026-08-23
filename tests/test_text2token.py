# -*- coding: utf-8 -*-
"""text2token 适配测试：中文唤醒词 -> 原模型拼音声母/韵母 token 行。"""

from __future__ import annotations

import unittest

from app.text2token import Text2Token

PINYIN_TOKENS = ["n", "ǐ", "h", "ǎo", "x", "iǎo", "zh", "ì"]


class TestPinyinText2Token(unittest.TestCase):
    def test_original_model_format(self):
        converter = Text2Token(PINYIN_TOKENS)
        self.assertEqual(
            converter.keyword_line("你好小智"),
            "n ǐ h ǎo x iǎo zh ì @你好小智",
        )

    def test_accepts_original_keywords_line(self):
        converter = Text2Token(PINYIN_TOKENS)
        line = "n ǐ h ǎo x iǎo zh ì @你好小智"
        self.assertEqual(converter.keyword_line(line), line)


if __name__ == "__main__":
    unittest.main()
