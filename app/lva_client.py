# -*- coding: utf-8 -*-
"""LVA peripheral 客户端：每个 LVA 一个独立连接/队列/唤醒状态。

与 v1 的区别：
- 不再自己开音频线程；由 main.py 的共享音频线程把同一块 PCM 分发给所有客户端。
- 每个 LvaClient 持有自己的 KWS stream_id、ring buffer、queue、streaming 状态。
- 因此多个 LVA 可以“分开监听、分开唤醒、分开推流”，互不阻塞。
"""

from __future__ import annotations

import asyncio
import collections
import logging
import random
import threading
import time
from urllib.parse import urlsplit

from . import lva_protocol as proto
from . import wsproto
from .audio_source import linear_resample
from .config import Config, LvaTarget
from .lva_protocol import AudioFormat, MsgType

logger = logging.getLogger("lva")


class LvaClient:
    def __init__(self, cfg: Config, target: LvaTarget, kws, stream_id: int) -> None:
        self._cfg = cfg
        self._target = target
        self._kws = kws
        self._stream_id = stream_id
        self._url = target.url

        self._frames_per_chunk = max(1, cfg.audio_sample_rate * cfg.chunk_ms // 1000)
        self._ring = collections.deque(
            maxlen=max(1, int(cfg.wake_buffer_s * 1000 / cfg.chunk_ms))
        )

        self._lock = threading.Lock()
        self._streaming: tuple[int, int, float] | None = None  # (stt_session_id, dst_rate, 开始时间)
        self._wake_pending: tuple[int, float] | None = None  # (sid, 发送时间)
        self._last_wake_time = 0.0
        self._stream_rate = cfg.audio_sample_rate

        self._loop: asyncio.AbstractEventLoop | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=64)
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
                    if self._connected_at is not None \
                            and time.monotonic() - self._connected_at < 3.0:
                        logger.warning(
                            "[%s] 连接建立后 3 秒内即被 LVA 关闭（code=%s）——通常是 LVA 侧拒绝："
                            "请检查 LVA 容器日志确认外设是否注册成功，以及 LVA 版本是否支持 "
                            "MicrophoneProfile/WakeWordEvent 协议（若字段号不匹配只需修改 "
                            "app/lva_protocol.py 常量）",
                            self._target.device_name, getattr(e, "code", "?"))
                except (OSError, asyncio.TimeoutError, wsproto.WsError,
                        proto.DecodeError, ValueError) as e:
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
        """单次连接会话：握手 -> 发 MicrophoneProfile -> 收消息循环。"""
        url = urlsplit(self._url)
        host = url.hostname or "127.0.0.1"
        port = url.port or 10700
        path = url.path or "/api/peripheral"

        reader, writer = await asyncio.open_connection(host, port)
        try:
            await wsproto.client_handshake(reader, writer, host, port, path)
            self._writer = writer

            profile = proto.build_microphone_profile(
                self._target.device_name,
                self._cfg.supported_languages,
                self._target.device_uuid,
            )
            await self._ws_send(writer, proto.encode_frame(MsgType.MICROPHONE_PROFILE, profile))
            self._connected_at = time.monotonic()
            logger.info(
                "[%s] 已连接 %s，MicrophoneProfile 已发送 (uuid=%s, langs=%s)",
                self._target.device_name, self._url,
                self._target.device_uuid[:8], ",".join(self._cfg.supported_languages),
            )

            # 丢弃上一会话残留的音频块
            self._drain_queue()

            sender = asyncio.create_task(self._sender(writer))
            try:
                await self._recv_loop(reader, writer)
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
        finally:
            self._writer = None
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            with self._lock:
                self._streaming = None
                self._wake_pending = None
            logger.info("[%s] 会话结束", self._target.device_name)

    # ------------------------------------------------------------------ 音频分发（由 main 的共享音频线程调用）

    def process_chunk(self, chunk: bytes) -> None:
        """把同一块 16k PCM 喂给当前 LVA 的 KWS stream，并按需推流。"""
        if self._stop_event.is_set():
            return

        # 唤醒词检测（每个 LVA 用自己的 stream，互不影响）
        try:
            keyword = self._kws.accept(self._stream_id, chunk, self._cfg.audio_sample_rate)
        except Exception:
            logger.exception("[%s] KWS 处理失败", self._target.device_name)
            keyword = None
        # 单模型使用所有 LVA 唤醒词的并集，这里只响应当前 LVA 自己的唤醒词
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

        # 环形缓冲 + 流式转发（每个 LVA 独立状态）
        with self._lock:
            self._ring.append(bytes(chunk))
            streaming = self._streaming
            if streaming is not None:
                started = streaming[2]
                if time.monotonic() - started > self._cfg.max_stream_seconds:
                    logger.warning("[%s] Start 后 %.0fs 未收到 Stop，自动停止流式传输",
                                   self._target.device_name, self._cfg.max_stream_seconds)
                    self._streaming = None
                    streaming = None
        if streaming is not None:
            sid, dst_rate = streaming[0], streaming[1]
            try:
                data = linear_resample(chunk, self._cfg.audio_sample_rate, dst_rate)
            except Exception:
                logger.exception("[%s] 重采样失败", self._target.device_name)
                return
            self._queue_put(sid, data)

    def _handle_keyword(self, keyword: str) -> None:
        now = time.monotonic()
        with self._lock:
            last = self._last_wake_time
            streaming = self._streaming
        if streaming is not None:
            logger.debug("[%s] 正在流式传输中，忽略唤醒词 %s", self._target.device_name, keyword)
            return
        if now - last < self._cfg.wake_cooldown_s:
            logger.debug("[%s] 唤醒冷却中，忽略 %s", self._target.device_name, keyword)
            return
        with self._lock:
            self._last_wake_time = now
        logger.info("[%s] 🎯 检测到唤醒词: %s", self._target.device_name, keyword)
        loop = self._loop
        if loop is not None and not self._stop_event.is_set():
            loop.call_soon_threadsafe(self._on_wake_word, keyword)

    def _queue_put(self, sid: int, data: bytes) -> None:
        try:
            self._queue.put_nowait((sid, data))
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()  # 丢弃最旧
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait((sid, data))
            except asyncio.QueueFull:
                pass

    def _drain_queue(self) -> None:
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    # ------------------------------------------------------------------ 事件循环侧

    def _on_wake_word(self, keyword: str) -> None:
        with self._lock:
            pending = self._wake_pending
        if pending is not None:
            sid, sent_at = pending
            if time.monotonic() - sent_at > self._cfg.wake_start_timeout_s:
                logger.warning("[%s] 唤醒事件 %d 未收到 Start（LVA 版本可能不支持 WakeWordEvent）",
                               self._target.device_name, sid)
                with self._lock:
                    self._wake_pending = None
            else:
                logger.debug("[%s] 已有未决唤醒事件，忽略重复触发", self._target.device_name)
                return
        if self._writer is None or self._writer.is_closing():
            logger.debug("[%s] 未连接，忽略本次唤醒触发", self._target.device_name)
            return
        sid = random.getrandbits(63) + 1
        with self._lock:
            self._wake_pending = (sid, time.monotonic())
        payload = proto.build_wake_word_event(
            sid, self._cfg.audio_sample_rate, 1, AudioFormat.PCM16
        )
        if self._send_nowait(proto.encode_frame(MsgType.WAKE_WORD_EVENT, payload)):
            logger.info("[%s] 已发送 WakeWordEvent (stt_session_id=%d)，等待 LVA 回复 Start ...",
                        self._target.device_name, sid)
        else:
            logger.warning("[%s] WakeWordEvent (stt_session_id=%d) 发送失败",
                           self._target.device_name, sid)
            with self._lock:
                if self._wake_pending is not None and self._wake_pending[0] == sid:
                    self._wake_pending = None

    def _send_nowait(self, data: bytes) -> bool:
        """把 LVA 协议帧包裹为 WebSocket 二进制帧写入连接。返回是否已写入。"""
        writer = self._writer
        if writer is None or writer.is_closing():
            logger.debug("[%s] 未连接，丢弃待发送消息", self._target.device_name)
            return False
        writer.write(wsproto.encode_frame(wsproto.OP_BINARY, data))
        asyncio.create_task(self._safe_drain(writer))
        return True

    async def _safe_drain(self, writer: asyncio.StreamWriter) -> None:
        try:
            await writer.drain()
        except Exception:
            pass

    async def _ws_send(self, writer: asyncio.StreamWriter, data: bytes) -> None:
        writer.write(wsproto.encode_frame(wsproto.OP_BINARY, data))
        await writer.drain()

    async def _sender(self, writer: asyncio.StreamWriter) -> None:
        """把队列中的音频块编码为 RecordedAudioChunk 发送。"""
        try:
            while True:
                sid, data = await self._queue.get()
                chunk = proto.build_recorded_audio_chunk(
                    sid, data, self._stream_rate, 1, AudioFormat.PCM16
                )
                await self._ws_send(writer, proto.encode_frame(MsgType.RECORDED_AUDIO_CHUNK, chunk))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("[%s] 发送任务结束", self._target.device_name, exc_info=True)

    async def _recv_loop(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        while True:
            opcode, payload = await asyncio.wait_for(
                wsproto.recv_message(reader, writer), timeout=self._cfg.read_timeout_s
            )
            if opcode != wsproto.OP_BINARY:
                continue
            try:
                msg_type, body = proto.decode_frame(payload)
            except ValueError:
                logger.warning("[%s] 无法解析帧，忽略 %d 字节", self._target.device_name, len(payload))
                continue
            if self._cfg.log_protocol_hex:
                logger.debug("[%s] 收到 %s (type=%d, %d 字节): %s",
                             self._target.device_name, MsgType.name(msg_type),
                             msg_type, len(body), body[:64].hex())
            self._handle_message(msg_type, body)

    def _handle_message(self, msg_type: int, body: bytes) -> None:
        tag = f"[{self._target.device_name}]"
        if msg_type == MsgType.ACK:
            ack = proto.parse_ack(body)
            logger.info("%s LVA Ack: ok=%s message=%r", tag, ack.ok, ack.message)
            if not ack.ok:
                raise wsproto.WsError(f"LVA 拒绝了外设: {ack.message}")
        elif msg_type == MsgType.STREAM_SETTINGS:
            settings = proto.parse_stream_settings(body)
            if settings.sample_rate_hz:
                self._stream_rate = settings.sample_rate_hz
            logger.info("%s LVA StreamSettings: sample_rate=%dHz, chunk=%dms",
                        tag, settings.sample_rate_hz or 16000, settings.chunk_duration_ms)
        elif msg_type == MsgType.START:
            start = proto.parse_start(body)
            sid = start.stt_session_id
            with self._lock:
                self._wake_pending = None
                self._streaming = (sid, self._stream_rate, time.monotonic())
                buffered = list(self._ring)
            logger.info("%s ▶ LVA Start: stt_session_id=%d，开始流式传输 (%dHz，回放缓冲 %d 块)",
                        tag, sid, self._stream_rate, len(buffered))
            for chunk in buffered:
                try:
                    data = linear_resample(chunk, self._cfg.audio_sample_rate, self._stream_rate)
                except Exception:
                    continue
                self._queue_put(sid, data)
        elif msg_type == MsgType.STOP:
            stop = proto.parse_stop(body)
            with self._lock:
                streaming = self._streaming
                if streaming is not None and streaming[0] == stop.stt_session_id:
                    self._streaming = None
                    stopped_here = True
                else:
                    stopped_here = False
            if stopped_here:
                logger.info("%s ■ LVA Stop: stt_session_id=%d，停止流式传输，继续监听唤醒词",
                            tag, stop.stt_session_id)
            else:
                logger.debug("%s LVA Stop (stt_session_id=%d) 与当前会话不符，忽略",
                             tag, stop.stt_session_id)
        elif msg_type == MsgType.ERROR:
            err = proto.parse_error(body)
            logger.warning("%s LVA Error: %s", tag, err.message)
        elif msg_type == MsgType.TRANSCRIPTION_EVENT:
            evt = proto.parse_transcription_event(body)
            logger.info("%s 💬 LVA 转写: %r (session=%d, is_command=%s)",
                        tag, evt.text, evt.stt_session_id, evt.is_command)
        elif msg_type == MsgType.COMMAND:
            cmd = proto.parse_command(body)
            logger.info("%s ⚡ LVA 命令: %r (session=%d)", tag, cmd.text, cmd.stt_session_id)
        elif msg_type == MsgType.SESSION_CONTEXT:
            ctx = proto.parse_session_context(body)
            logger.debug("%s LVA SessionContext: language=%s session=%d",
                         tag, ctx.language_code, ctx.stt_session_id)
        else:
            logger.warning("%s 忽略未知消息类型 %s (%d 字节): %s",
                           tag, MsgType.name(msg_type), len(body), body[:64].hex())
