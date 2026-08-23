# -*- coding: utf-8 -*-
"""多 LVA 集成冒烟测试：两个 mock LVA + FakeSpotter，验证来源隔离唤醒。

不依赖 sherpa-onnx / 真实麦克风。
"""

from __future__ import annotations

import asyncio
import logging
import unittest

from app.config import Config, LvaTarget
from app.kws import FakeSpotter
from app.lva_client import LvaClient

from scripts.mock_lva_server import MockLva

CHUNK = b"\x00\x00" * 1600  # 100ms @16k s16le


async def wait_until(pred, timeout: float = 10.0, interval: float = 0.02) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(interval)
    return False


def _make_cfg(url1: str, url2: str) -> Config:
    cfg = Config()
    cfg.audio_sample_rate = 16000
    cfg.chunk_ms = 100
    cfg.wake_cooldown_s = 0.0
    cfg.wake_buffer_s = 0.2
    cfg.wake_start_timeout_s = 5.0
    cfg.reconnect_min_s = 0.1
    cfg.reconnect_max_s = 0.2
    cfg.max_stream_seconds = 30.0
    cfg.read_timeout_s = 30.0
    cfg.supported_languages = ["zh_CN"]
    cfg.lva_targets = [
        LvaTarget(url=url1, device_name="lva-1", device_uuid_file="/tmp/uuid1",
                  device_uuid="uuid-1", keywords=["你好小智"]),
        LvaTarget(url=url2, device_name="lva-2", device_uuid_file="/tmp/uuid2",
                  device_uuid="uuid-2", keywords=["你好小智"]),
    ]
    return cfg


class TestMultiLvaIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mock1 = MockLva("结果1", chunks_before_stop=0)
        self.mock2 = MockLva("结果2", chunks_before_stop=0)
        self.server1 = await asyncio.start_server(self.mock1.handle, "127.0.0.1", 0)
        self.server2 = await asyncio.start_server(self.mock2.handle, "127.0.0.1", 0)
        port1 = self.server1.sockets[0].getsockname()[1]
        port2 = self.server2.sockets[0].getsockname()[1]
        self.cfg = _make_cfg(
            f"ws://127.0.0.1:{port1}/api/peripheral",
            f"ws://127.0.0.1:{port2}/api/peripheral",
        )

    async def asyncTearDown(self):
        self.server1.close()
        self.server2.close()
        await self.server1.wait_closed()
        await self.server2.wait_closed()

    async def test_wake_only_its_own_lva(self):
        spotter = FakeSpotter(["你好小智"], trigger_every=1, min_interval_s=0)
        sid1 = spotter.create_stream()
        sid2 = spotter.create_stream()
        c1 = LvaClient(self.cfg, self.cfg.lva_targets[0], spotter, sid1)
        c2 = LvaClient(self.cfg, self.cfg.lva_targets[1], spotter, sid2)
        t1 = asyncio.create_task(c1.run())
        t2 = asyncio.create_task(c2.run())

        try:
            ok = await wait_until(lambda: any(
                e["kind"] == "profile" for e in self.mock1.events
            ) and any(e["kind"] == "profile" for e in self.mock2.events))
            self.assertTrue(ok, "两个 LVA 都未完成 MicrophoneProfile")

            # 只喂 LVA1 的音频：只有 mock1 应收到唤醒
            c1.process_chunk(CHUNK)
            await asyncio.sleep(0.05)
            self.assertTrue(any(e["kind"] == "wake_event" for e in self.mock1.events),
                            "LVA1 应收到唤醒事件")
            self.assertFalse(any(e["kind"] == "wake_event" for e in self.mock2.events),
                             "LVA2 不应收到 LVA1 来源的唤醒事件")

            # 只喂 LVA2 的音频：只有 mock2 应收到唤醒
            c2.process_chunk(CHUNK)
            await asyncio.sleep(0.05)
            self.assertTrue(any(e["kind"] == "wake_event" for e in self.mock2.events),
                            "LVA2 应收到唤醒事件")
        finally:
            t1.cancel()
            t2.cancel()
            await asyncio.gather(t1, t2, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    unittest.main()
