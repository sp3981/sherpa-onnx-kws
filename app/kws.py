# -*- coding: utf-8 -*-
"""sherpa-onnx 关键词检测（KWS）封装（单模型多 stream，供多 LVA 独立唤醒）。

使用一个 sherpa_onnx.KeywordSpotter 实例 + 多个 stream：
每个 LVA 客户端持有自己的 stream_id，互不抢占、互不重置，
从而实现“同一个容器、同一个模型、多个 LVA 分开监听和唤醒”。

模型目录需包含: encoder-*.onnx, decoder-*.onnx, joiner-*.onnx, tokens.txt
"""

from __future__ import annotations

import glob
import logging
import os
import re
import threading

from .text2token import Text2Token, Text2TokenError

logger = logging.getLogger("kws")


class SpotterError(Exception):
    pass


def _find_model_file(model_dir: str, pattern: str) -> str:
    """在模型目录中找匹配文件；同名多版本时优先 fp32、再取 epoch 最大者。"""
    matches = glob.glob(os.path.join(model_dir, pattern))
    if not matches:
        raise SpotterError(f"模型目录 {model_dir} 中找不到 {pattern}")

    def rank(path: str) -> tuple[bool, int]:
        base = os.path.basename(path)
        m = re.search(r"epoch-(\d+)", base)
        return (".int8." not in base, int(m.group(1)) if m else 0)

    return sorted(matches, key=rank, reverse=True)[0]


class KeywordSpotter:
    """sherpa-onnx KeywordSpotter 封装：单模型多 stream。

    每个 LVA 客户端通过 ``create_stream()`` 拿到一个 stream_id，
    之后一直用该 id 调用 ``accept()``，命中后内部自动重置该 stream，
    不影响其它 LVA 的 stream。
    """

    def __init__(self, model_dir: str, keywords: list[str], num_threads: int = 2,
                 max_active_paths: int = 4, provider: str = "cpu",
                 keywords_score: float | None = None,
                 keywords_threshold: float | None = None) -> None:
        if not keywords:
            raise SpotterError("KEYWORDS 为空，请至少配置一个中文唤醒词")

        import numpy as np  # noqa: F401  确认 numpy 可用
        import sherpa_onnx

        tokens = _find_model_file(model_dir, "tokens.txt")
        encoder = _find_model_file(model_dir, "encoder-*.onnx")
        decoder = _find_model_file(model_dir, "decoder-*.onnx")
        joiner = _find_model_file(model_dir, "joiner-*.onnx")

        # 用户只需在 KEYWORDS 里写中文；自动转成 KWS token 行
        try:
            converter = Text2Token.from_file(tokens)
            keyword_lines = converter.format_lines(keywords)
        except (OSError, Text2TokenError) as e:
            raise SpotterError(f"关键词转 token 失败: {e}") from e

        self._keywords_file = os.path.join(model_dir, "keywords.generated.txt")
        with open(self._keywords_file, "w", encoding="utf-8") as f:
            f.write("\n".join(keyword_lines) + "\n")
        logger.info("关键词已自动转 token: %s", keyword_lines)

        kwargs = dict(
            tokens=tokens,
            encoder=encoder,
            decoder=decoder,
            joiner=joiner,
            num_threads=num_threads,
            max_active_paths=max_active_paths,
            keywords_file=self._keywords_file,
            provider=provider,
        )
        if keywords_score is not None:
            kwargs["keywords_score"] = keywords_score
        if keywords_threshold is not None:
            kwargs["keywords_threshold"] = keywords_threshold

        self._kws = sherpa_onnx.KeywordSpotter(**kwargs)
        self._streams: dict[int, object] = {}
        self._next_id = 1
        self._lock = threading.Lock()
        logger.info(
            "KeywordSpotter 已初始化: model=%s keywords=%s threads=%d (多 stream 模式)",
            os.path.basename(model_dir), keywords, num_threads,
        )

    def create_stream(self) -> int:
        """创建一个独立 KWS stream，返回 stream_id。"""
        with self._lock:
            stream = self._kws.create_stream()
            sid = self._next_id
            self._next_id += 1
            self._streams[sid] = stream
            return sid

    def accept(self, stream_id: int, pcm16: bytes, sample_rate: int = 16000) -> str | None:
        """喂入一段 s16le PCM 到指定 stream，返回命中的关键词或 None。"""
        import numpy as np

        samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None:
                return None
            stream.accept_waveform(sample_rate, samples)
            while self._kws.is_ready(stream):
                self._kws.decode_stream(stream)
            result = self._kws.get_result(stream)
            # 兼容不同 sherpa-onnx 版本：
            # 旧版返回 KeywordSpotterResult（带 .keyword），新版直接返回 str
            if result is None:
                keyword = None
            elif isinstance(result, str):
                keyword = result
            else:
                keyword = getattr(result, "keyword", None)
            if keyword:
                # 命中后只重置当前 LVA 的 stream，不影响其它 LVA
                self._streams[stream_id] = self._kws.create_stream()
            return keyword

    def reset(self, stream_id: int) -> None:
        with self._lock:
            if stream_id in self._streams:
                self._streams[stream_id] = self._kws.create_stream()

    def remove_stream(self, stream_id: int) -> None:
        with self._lock:
            self._streams.pop(stream_id, None)


class FakeSpotter:
    """测试/联调用：每个 stream 独立周期性“命中”关键词。"""

    def __init__(self, keywords: list[str], trigger_every: int = 5,
                 min_interval_s: float = 0.5) -> None:
        self._keyword = keywords[0] if keywords else "你好小智"
        self._trigger_every = max(1, trigger_every)
        self._min_interval_s = min_interval_s
        self._lock = threading.Lock()
        self._next_id = 1
        self._state: dict[int, dict] = {}
        logger.info("FakeSpotter 已启用: 每 %d 块(且间隔>=%.1fs)命中一次 '%s' (多 stream 模式)",
                    self._trigger_every, self._min_interval_s, self._keyword)

    def create_stream(self) -> int:
        with self._lock:
            sid = self._next_id
            self._next_id += 1
            self._state[sid] = {"count": 0, "last_hit": 0.0}
            return sid

    def accept(self, stream_id: int, pcm16: bytes, sample_rate: int = 16000) -> str | None:
        import time

        with self._lock:
            st = self._state.get(stream_id)
            if st is None:
                return None
            st["count"] += 1
            now = time.monotonic()
            if st["count"] >= self._trigger_every and now - st["last_hit"] >= self._min_interval_s:
                st["count"] = 0
                st["last_hit"] = now
                return self._keyword
            return None

    def reset(self, stream_id: int) -> None:
        with self._lock:
            if stream_id in self._state:
                self._state[stream_id] = {"count": 0, "last_hit": 0.0}

    def remove_stream(self, stream_id: int) -> None:
        with self._lock:
            self._state.pop(stream_id, None)


def create_spotter(model_dir: str, keywords: list[str], num_threads: int,
                   fake: bool = False, trigger_every: int = 5) -> KeywordSpotter | FakeSpotter:
    if fake:
        return FakeSpotter(keywords, trigger_every=trigger_every)
    return KeywordSpotter(model_dir=model_dir, keywords=keywords, num_threads=num_threads)
