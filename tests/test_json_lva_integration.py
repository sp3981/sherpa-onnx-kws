# -*- coding: utf-8 -*-
"""JSON 模式集成测试：本地 KWS 命中后只向对应 LVA 发 start_listening。"""

from __future__ import annotations

import asyncio
import json
import unittest

from app import wsproto
from app.config import Config, LvaTarget
from app.json_lva_client import JsonLvaClient
from app.kws import FakeSpotter

CHUNK = b"\x00\x00" * 1600  # 100ms @16k s16le


async def wait_until(pred, timeout: float = 10.0, interval: float = 0.02) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(interval)
    return False


class JsonMockLva:
    def __init__(self) -> None:
        self.commands: list[dict] = []
        self.connected = False

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await wsproto.server_handshake(reader, writer)
        self.connected = True
        snapshot = json.dumps({"event": "snapshot", "data": {}}, ensure_ascii=False).encode("utf-8")
        await wsproto.send_frame(writer, wsproto.OP_TEXT, snapshot, mask=False)
        try:
            while True:
                opcode, payload = await wsproto.recv_message(reader, writer)
                if opcode == wsproto.OP_TEXT:
                    msg = json.loads(payload.decode("utf-8"))
                    if msg.get("command") == "start_listening":
                        self.commands.append(msg)
        except Exception:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


def _make_cfg(url1: str, url2: str) -> Config:
    cfg = Config()
    cfg.audio_sample_rate = 16000
    cfg.chunk_ms = 100
    cfg.wake_cooldown_s = 0.0
    cfg.wake_start_timeout_s = 5.0
    cfg.reconnect_min_s = 0.1
    cfg.reconnect_max_s = 0.2
    cfg.read_timeout_s = 30.0
    cfg.supported_languages = ["zh_CN"]
    cfg.lva_targets = [
        LvaTarget(url=url1, device_name="json-1", device_uuid_file="/tmp/j1",
                  device_uuid="j1", keywords=["你好小智"], protocol="json"),
        LvaTarget(url=url2, device_name="json-2", device_uuid_file="/tmp/j2",
                  device_uuid="j2", keywords=["你好小智"], protocol="json"),
    ]
    return cfg


class TestJsonLvaIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mock1 = JsonMockLva()
        self.mock2 = JsonMockLva()
        self.server1 = await asyncio.start_server(self.mock1.handle, "127.0.0.1", 0)
        self.server2 = await asyncio.start_server(self.mock2.handle, "127.0.0.1", 0)
        port1 = self.server1.sockets[0].getsockname()[1]
        port2 = self.server2.sockets[0].getsockname()[1]
        self.cfg = _make_cfg(f"ws://127.0.0.1:{port1}", f"ws://127.0.0.1:{port2}")

    async def asyncTearDown(self):
        self.server1.close()
        self.server2.close()
        await self.server1.wait_closed()
        await self.server2.wait_closed()

    async def test_start_listening_only_for_its_own_source(self):
        spotter = FakeSpotter(["你好小智"], trigger_every=1, min_interval_s=0)
        sid1 = spotter.create_stream()
        sid2 = spotter.create_stream()
        c1 = JsonLvaClient(self.cfg, self.cfg.lva_targets[0], spotter, sid1)
        c2 = JsonLvaClient(self.cfg, self.cfg.lva_targets[1], spotter, sid2)
        t1 = asyncio.create_task(c1.run())
        t2 = asyncio.create_task(c2.run())

        try:
            ok = await wait_until(lambda: self.mock1.connected and self.mock2.connected)
            self.assertTrue(ok, "两个 JSON LVA 都未连接")

            c1.process_chunk(CHUNK)
            await asyncio.sleep(0.05)
            self.assertEqual(len(self.mock1.commands), 1, "LVA1 应收到 start_listening")
            self.assertEqual(len(self.mock2.commands), 0, "LVA2 不应收到")

            c2.process_chunk(CHUNK)
            await asyncio.sleep(0.05)
            self.assertEqual(len(self.mock2.commands), 1, "LVA2 应收到 start_listening")
        finally:
            t1.cancel()
            t2.cancel()
            await asyncio.gather(t1, t2, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
