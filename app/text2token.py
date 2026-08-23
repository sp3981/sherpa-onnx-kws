# -*- coding: utf-8 -*-
"""把中文唤醒词转换为 sherpa-onnx KWS 所需的 token 序列。

参考 sherpa-onnx 官方 scripts/text2token.py 和原模型 tokens.txt：
该中文 KWS 模型的建模单元是 **拼音声母 + 韵母（带声调）**，
例如：你好小智 -> n ǐ h ǎo x iǎo zh ì

原模型 keywords.txt 每行格式为::

    token1 token2 ... @中文唤醒词

例如::

    n ǐ h ǎo x iǎo zh ì @你好小智

本项目在启动时用本模块自动把 ``KEYWORDS=你好小智`` 转换成上面的格式，
因此 docker-compose / .env 里只需要填中文，不需要手工填 token。
"""

from __future__ import annotations

from typing import Iterable, Sequence

# sentencepiece 风格的空格符号；部分模型会用到。
SPACE_SYMBOL = "▁"

# 拼音声母（按长度降序，先匹配 zh/ch/sh）
PINYIN_INITIALS = [
    "zh", "ch", "sh",
    "b", "p", "m", "f", "d", "t", "n", "l",
    "g", "k", "h", "j", "q", "x",
    "r", "z", "c", "s", "y", "w",
]


class Text2TokenError(Exception):
    """中文文本无法转换为模型 token 时抛出。"""


def _is_cjk(ch: str) -> bool:
    """判断是否为 CJK 统一汉字。"""
    return "\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf"


def read_tokens(tokens_path: str) -> list[str]:
    """读取 sherpa-onnx 的 tokens.txt，返回 token 列表。

    tokens.txt 每行格式为 ``token id``，这里只取第一列 token。
    空行和注释行会被忽略。
    """
    tokens: list[str] = []
    # utf-8-sig 兼容 Windows 下可能带 BOM 的 tokens.txt
    with open(tokens_path, "r", encoding="utf-8-sig") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            tokens.append(line.split()[0])
    if not tokens:
        raise Text2TokenError(f"tokens.txt 为空或无法读取: {tokens_path}")
    return tokens


class Text2Token:
    """基于 tokens.txt 的中文拼音（声母 + 韵母）tokenizer。

    对汉字先用 pypinyin 转成带声调的拼音（如 nǐ），
    再按声母表拆成 tokens.txt 中的 token（如 n + ǐ）。
    若用户误传了已经是 token 序列的字符串，也会识别并兼容。
    """

    def __init__(self, tokens: Sequence[str]) -> None:
        self.tokens = list(tokens)
        self._token_set = set(self.tokens)
        if not self._token_set:
            raise Text2TokenError("tokens 列表为空")
        self._space_symbol = SPACE_SYMBOL if SPACE_SYMBOL in self._token_set else None
        self._max_token_len = max((len(t) for t in self.tokens), default=1)

    @classmethod
    def from_file(cls, tokens_path: str) -> "Text2Token":
        return cls(read_tokens(tokens_path))

    def _is_already_tokenized(self, text: str) -> bool:
        """判断用户是否已经传入了纯 token 序列。"""
        pieces = text.split()
        return bool(pieces) and all(p in self._token_set for p in pieces)

    def _parse_keyword_line(self, text: str) -> tuple[str, list[str]] | None:
        """识别既有 keywords 行，返回 ``(中文唤醒词, tokens)``。

        支持两种格式：
        1. 原模型格式：``n ǐ h ǎo @你好小智``
        2. 旧项目格式：``你好小智 你 好 小 智``
        """
        text = text.strip()
        if not text:
            return None

        # 原模型格式：tokens... @中文
        if "@" in text:
            left, _, right = text.partition("@")
            tokens = left.split()
            display = right.strip()
            if tokens and all(t in self._token_set for t in tokens):
                return display, tokens

        pieces = text.split()
        # 旧格式：中文唤醒词 token1 token2 ...
        if (
            len(pieces) >= 2
            and pieces[0] not in self._token_set
            and all(p in self._token_set for p in pieces[1:])
        ):
            return pieces[0], pieces[1:]
        # 纯 token 序列
        if self._is_already_tokenized(text):
            tokens = pieces
            display = "".join(tokens)
            if self._space_symbol:
                display = display.replace(self._space_symbol, " ")
            return display, tokens
        return None

    def _normalize(self, text: str) -> str:
        """把普通空格换成 sentencepiece 的 ▁，便于英文等含空格文本。"""
        if self._space_symbol and " " in text and " " not in self._token_set:
            return text.replace(" ", self._space_symbol)
        return text

    def _pinyin_syllable_tokens(self, ch: str) -> list[str]:
        """把一个汉字转成 ``[声母, 韵母]`` 形式的 token（带声调）。"""
        try:
            from pypinyin import Style, pinyin
        except ImportError:
            return []
        try:
            syl = pinyin(ch, style=Style.TONE)[0][0]
        except Exception:
            return []
        if not syl:
            return []
        for init in PINYIN_INITIALS:
            if syl.startswith(init):
                final = syl[len(init):]
                return [init, final] if final else [init]
        return [syl]

    def tokenize(self, text: str) -> list[str]:
        """把一段中文（或其它文本）切成 tokens.txt 中存在的 token 序列。"""
        text = text.strip()
        if not text:
            raise Text2TokenError("唤醒词为空")

        # 兼容已经手工 token 化的输入
        parsed = self._parse_keyword_line(text)
        if parsed is not None:
            return parsed[1]

        normalized = self._normalize(text)
        result: list[str] = []
        i = 0
        n = len(normalized)
        while i < n:
            # 先尝试最长匹配（英文、已有 token 等情况）
            matched = False
            end = min(n, i + self._max_token_len)
            while end > i:
                piece = normalized[i:end]
                if piece in self._token_set:
                    result.append(piece)
                    i = end
                    matched = True
                    break
                end -= 1
            if matched:
                continue

            ch = normalized[i]
            if _is_cjk(ch):
                toks = self._pinyin_syllable_tokens(ch)
                if toks and all(t in self._token_set for t in toks):
                    result.extend(toks)
                    i += 1
                    continue
                raise Text2TokenError(
                    f"字符 {ch!r} 无法转换为模型拼音 token（声母/韵母）: {toks}；"
                    f"请换一个模型支持的唤醒词"
                )
            raise Text2TokenError(
                f"字符 {ch!r} 不在 tokens.txt 中，也无法转换为拼音 token；"
                f"请换一个模型支持的唤醒词"
            )
        return result

    def keyword_line(self, keyword: str) -> str:
        """生成 KWS keywords 文件的一行：``token1 token2 ... @中文唤醒词``。"""
        keyword = keyword.strip()
        if not keyword:
            raise Text2TokenError("唤醒词为空")

        parsed = self._parse_keyword_line(keyword)
        if parsed is not None:
            display, tokens = parsed
        else:
            tokens = self.tokenize(keyword)
            display = keyword.replace(" ", "")

        return " ".join(tokens) + " @" + display

    def format_lines(self, keywords: Iterable[str]) -> list[str]:
        """批量转换，供写入 keywords.generated.txt。"""
        return [self.keyword_line(kw) for kw in keywords]
