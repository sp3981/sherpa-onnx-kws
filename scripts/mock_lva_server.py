# -*- coding: utf-8 -*-
"""Mock LVA 服务器：模拟 Linux Voice Assistant 的 peripheral WebSocket 行为。

用途：
  1. 集成测试
  2. 无真实 LVA 时联调本项目的 Docker 镜像：
       python scripts/mock_lva_server.py --port 10700
     然后把容器的 LVA_URLS 指向 ws://<主机IP>:10700/api/peripheral

行为：
  - 收到 MicrophoneProfile -> 回复 StreamSettings(16000Hz, 100ms) + Ack(ok)
  - 收到 WakeWordEvent    -> 回复 Start(新的 stt_session_id)
  - 收到 RecordedAudioChunk -> 计数，达到 --chunks-before-stop 后发送
    Stop + TranscriptionEvent("模拟转写结果")
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import lva_protocol as proto  # noqa: E402
from app import wsproto  # noqa: E402
from app.lva_protocol import AudioFormat, MsgType  # noqa: E402

logger = logging.getLogger("mock-lva")


class MockLva:
    def __init__(self, text: str, chunks_before_stop: int, close_after_stop: bool = False) -> None:
        self.text = text
        self.chunks_before_stop = chunks_before_stop
        self.close_after_stop = close_after_stop
        self.events: list[dict] = []
        self.chunk_byte_counts: list[int] = []

    def _record(self, kind: str, **kw) -> None:
        self.events.append({"kind": kind, **kw})
        logger.info("事件: %s %s", kind, kw if len(str(kw)) < 200 else str(kw)[:200])

    async def _send(self, writer: asyncio.StreamWriter, msg_type: int, payload: bytes) -> None:
        frame = proto.encode_frame(msg_type, payload)
        await wsproto.send_frame(writer, wsproto.OP_BINARY, frame, mask=False)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        path = await wsproto.server_handshake(reader, writer)
        logger.info("外设已连接: path=%s", path)
        self._record("connect", path=path)
        chunks_received = 0
        active_sid = 0
        try:
            while True:
                opcode, payload = await wsproto.recv_message(reader, writer)  # type: ignore[arg-type]
                if opcode != wsproto.OP_BINARY:
                    continue
                msg_type, body = proto.decode_frame(payload)
                logger.debug("收到 %s (%d 字节)", MsgType.name(msg_type), len(body))

                if msg_type == MsgType.MICROPHONE_PROFILE:
                    fields = proto.parse_fields(body)
                    name = next(
                        (f.value.decode() for f in fields if f.number == 1), "?"
                    )
                    self._record("profile", device_name=name)
                    await self._send(
                        writer, MsgType.STREAM_SETTINGS, proto.build_stream_settings(16000, 100)
                    )
                    await self._send(writer, MsgType.ACK, proto.build_ack(True, "mock ok"))

                elif msg_type == MsgType.WAKE_WORD_EVENT:
                    fields = proto.parse_fields(body)
                    wake_sid = next((f.value for f in fields if f.number == 1), 0)
                    self._record("wake_event", stt_session_id=wake_sid)
                    active_sid = wake_sid if wake_sid else active_sid
                    await self._send(writer, MsgType.START, proto.build_start(active_sid))
                    logger.info("已回复 Start (stt_session_id=%d)", active_sid)

                elif msg_type == MsgType.RECORDED_AUDIO_CHUNK:
                    fields = proto.parse_fields(body)
                    sid = next((f.value for f in fields if f.number == 1), 0)
                    samples = next(
                        (f.value for f in fields if f.number == 2), b""
                    )
                    if isinstance(samples, bytes):
                        chunks_received += 1
                        self.chunk_byte_counts.append(len(samples))
                        if chunks_received % 10 == 1:
                            logger.info("已收到 %d 个音频块 (session=%d)", chunks_received, sid)
                        if self.chunks_before_stop and chunks_received >= self.chunks_before_stop:
                            await self._send(writer, MsgType.STOP, proto.build_stop(sid))
                            await self._send(
                                writer,
                                MsgType.TRANSCRIPTION_EVENT,
                                proto.build_transcription_event(sid, self.text),
                            )
                            self._record(
                                "stopped_and_transcribed",
                                chunks=chunks_received, sid=sid, text=self.text,
                            )
                            chunks_received = 0
                            active_sid = 0
                            if self.close_after_stop:
                                logger.info("按配置关闭连接")
                                return
                else:
                    logger.debug("忽略消息类型 %s", MsgType.name(msg_type))
        except Exception as e:  # WsClosed/WsError 正常断开；连接重置等异常同样结束会话
            logger.info("外设断开 (%s)", type(e).__name__)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception as e:  # noqa: BLE001 清理路径：忽略一切关闭期异常
                logger.debug("关闭连接时忽略异常: %s", e)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Mock LVA peripheral 服务器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=10700)
    parser.add_argument("--text", default="打开客厅的灯（模拟转写）")
    parser.add_argument("--chunks-before-stop", type=int, default=30,
                        help="收到多少个音频块后回 Stop，0 表示永不停止")
    parser.add_argument("--close-after-stop", action="store_true",
                        help="发完转写后主动断开连接（测试客户端重连）")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    mock = MockLva(args.text, args.chunks_before_stop, args.close_after_stop)
    server = await asyncio.start_server(mock.handle, args.host, args.port)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets or [])
    logger.info("Mock LVA 监听: %s，请把 LVA_URLS 指向 ws://%s:%d/api/peripheral",
                addrs, args.host, args.port)
    try:
        await server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
