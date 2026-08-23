# -*- coding: utf-8 -*-
"""JSON 模式 LVA 客户端：适配当前官方 LVA 的 JSON peripheral API。

当前 LVA（OHF-Voice/linux-voice-assistant）的外设 WebSocket 协议是 JSON 文本：
  LVA -> 外设: {"event": "snapshot", "data": {...}}
  外设 -> LVA: {"command": "start_listening"}

JSON 模式不做音频推流：本地 KWS 检测到唤醒词后，只向对应 LVA 发送
``{"command": "start_listening"}``，LVA 自己会从 PulseAudio 采集后续语音并播放音响。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from urllib.parse import urlsplit

from . import wsproto
from .config import Config, LvaTarget

logger = logging.getLogger("lva-json")


class JsonLvaClient:
    def __init__(self, cfg: Config, target: LvaTarget, kws, stream_id: int) -> None:
        self._cfg = cfg
        self._target = target
        self._kws = kws
        self._stream_id = stream_id
        self._url = target.url

        self._lock = threading.Lock()
        self._last_wake_time = 0.0

        self._loop: asyncio.AbstractEventLoop | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._stop_event = threading.Event()
        self._connected_at: float | None = None

    # ------------------------------------------------------------------ 生命周期

    def stop(self) -> None:
        self._stop_event.set()
        self._kws.remove_stream(self._stream_id)

    async def run(self) -> None:
        """常驻运行：连接失败/断开自动重连（指数退避）。"""
        self._loop = asyncio.get_running_loop()
        backoff = 0.0
        try:
            while not self._stop_event.is_set():
                try:
                    await self.session()
                    backoff = 0.0
                except wsproto.WsClosed as e:
                    logger.info("[%s] LVA 连接已关闭: %s", self._target.device_name, e)
                except (OSError, asyncio.TimeoutError, wsproto.WsError, ValueError) as e:
                    logger.warning("[%s] LVA 会话异常: %s", self._target.device_name, e)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("[%s] LVA 会话异常", self._target.device_name)
                backoff = min(backoff + self._cfg.reconnect_min_s, self._cfg.reconnect_max_s)
                logger.info("[%s] %.1fs 后重连 %s", self._target.device_name, backoff, self._url)
                await asyncio.sleep(backoff)
        finally:
            self.stop()

    async def session(self) -> None:
        """单次 JSON WebSocket 会话：连接后等待事件，唤醒时发命令。"""
        url = urlsplit(self._url)
        host = url.hostname or "127.0.0.1"
        port = url.port or 6055
        path = url.path or "/"

        reader, writer = await asyncio.open_connection(host, port)
        try:
            await wsproto.client_handshake(reader, writer, host, port, path)
            self._writer = writer
            self._connected_at = time.monotonic()
            logger.info("[%s] JSON 已连接 %s", self._target.device_name, self._url)
            await self._recv_loop(reader, writer)
        finally:
            self._writer = None
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            logger.info("[%s] JSON 会话结束", self._target.device_name)

    # ------------------------------------------------------------------ 音频分发

    def process_chunk(self, chunk: bytes) -> None:
        """喂入 PCM 做本地 KWS；命中后只向本 LVA 发 start_listening。"""
        if self._stop_event.is_set():
            return

        try:
            keyword = self._kws.accept(self._stream_id, chunk, self._cfg.audio_sample_rate)
        except Exception:
            logger.exception("[%s] KWS 处理失败", self._target.device_name)
            keyword = None

        allowed = set()
        for kw in self._target.keywords:
            kw = kw.strip()
            if not kw:
                continue
            if "@" in kw:
                allowed.add(kw.split("@", 1)[1].strip())
            else:
                allowed.add(kw.split()[0])
        if keyword and keyword not in allowed:
            keyword = None
        if keyword:
            self._handle_keyword(keyword)

    def _handle_keyword(self, keyword: str) -> None:
        now = time.monotonic()
        with self._lock:
            last = self._last_wake_time
        if now - last < self._cfg.wake_cooldown_s:
            logger.debug("[%s] 唤醒冷却中，忽略 %s", self._target.device_name, keyword)
            return
        with self._lock:
            self._last_wake_time = now
        logger.info("[%s] 🎯 检测到唤醒词: %s -> 发送 start_listening", self._target.device_name, keyword)
        loop = self._loop
        if loop is not None and not self._stop_event.is_set():
            loop.call_soon_threadsafe(self._on_wake_word)

    def _on_wake_word(self) -> None:
        if self._writer is None or self._writer.is_closing():
            logger.debug("[%s] 未连接，忽略唤醒触发", self._target.device_name)
            return
        payload = json.dumps({"command": "start_listening"}, ensure_ascii=False).encode("utf-8")
        if self._send_nowait(payload):
            logger.info("[%s] 已发送 start_listening", self._target.device_name)
        else:
            logger.warning("[%s] start_listening 发送失败", self._target.device_name)

    def _send_nowait(self, payload: bytes) -> bool:
        writer = self._writer
        if writer is None or writer.is_closing():
            return False
        writer.write(wsproto.encode_frame(wsproto.OP_TEXT, payload))
        asyncio.create_task(self._safe_drain(writer))
        return True

    async def _safe_drain(self, writer: asyncio.StreamWriter) -> None:
        try:
            await writer.drain()
        except Exception:
            pass

    # ------------------------------------------------------------------ 接收

    async def _recv_loop(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        while True:
            opcode, payload = await asyncio.wait_for(
                wsproto.recv_message(reader, writer), timeout=self._cfg.read_timeout_s
            )
            if opcode == wsproto.OP_TEXT:
                try:
                    text = payload.decode("utf-8", "replace")
                    msg = json.loads(text)
                    event = msg.get("event")
                    if event:
                        logger.debug("[%s] LVA event: %s data=%s",
                                     self._target.device_name, event, msg.get("data"))
                except Exception:
                    logger.debug("[%s] 忽略无法解析的 JSON: %.200s",
                                 self._target.device_name, payload[:200])
            elif opcode == wsproto.OP_BINARY:
                logger.debug("[%s] 忽略二进制帧（JSON 模式）", self._target.device_name)
