# -*- coding: utf-8 -*-
"""入口：单容器、多路麦克风、按唤醒词组合共享 KWS 模型，同时连接多个 LVA。

每个 LVA 使用独立的音频源 / KWS stream / WebSocket / 队列 / 唤醒状态，
实现“按来源 + 唤醒词分开唤醒”。
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import threading

from .audio_source import AudioSource, AudioSourceError, create_audio_source
from .config import Config, load_config
from .json_lva_client import JsonLvaClient
from .kws import create_spotter
from .lva_client import LvaClient

logger = logging.getLogger("main")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def _source_loop(
    audio: AudioSource,
    client: LvaClient,
    cfg: Config,
    stop_event: threading.Event,
) -> None:
    """每个 LVA 一个独立麦克风音频线程：只喂给对应的 LvaClient。"""
    frames_per_chunk = max(1, cfg.audio_sample_rate * cfg.chunk_ms // 1000)
    logger.info("音频线程[%s] 启动: %s, %dHz, %dms/块",
                client._target.device_name, audio.description,
                cfg.audio_sample_rate, cfg.chunk_ms)
    audio.start()
    while not stop_event.is_set():
        try:
            chunk = audio.read(frames_per_chunk)
        except AudioSourceError:
            if stop_event.is_set():
                break
            logger.warning("音频[%s] 读取失败，1s 后重试", client._target.device_name)
            stop_event.wait(1.0)
            audio.start()
            continue
        except Exception:
            logger.exception("音频[%s] 读取异常", client._target.device_name)
            stop_event.wait(1.0)
            continue
        try:
            client.process_chunk(chunk)
        except Exception:
            logger.exception("音频[%s] 分发失败", client._target.device_name)
    logger.info("音频线程[%s] 退出", client._target.device_name)


async def async_main() -> None:
    cfg = load_config()
    setup_logging(cfg.log_level)

    logger.info("==========================================================")
    logger.info("sherpa-onnx-kws 启动（单容器多 LVA）")
    logger.info("  LVA 数量:   %d", len(cfg.lva_targets))
    for i, t in enumerate(cfg.lva_targets, 1):
        logger.info("  LVA[%d]: %s name=%s uuid=%s keywords=%s audio=%s proto=%s",
                    i, t.url, t.device_name, t.device_uuid[:8], " / ".join(t.keywords),
                    t.audio_source or cfg.audio_source, t.protocol)
    logger.info("  采样率:     %dHz, %dms/块", cfg.audio_sample_rate, cfg.chunk_ms)
    logger.info("  模型目录:   %s (fake=%s)", cfg.kws_model_dir, cfg.kws_fake)
    logger.info("==========================================================")

    # 相同唤醒词组合的 LVA 共用一个 KeywordSpotter（多 stream），
    # 不同唤醒词组合各自独立模型，避免“听到别人的唤醒词也重置本路 stream”。
    # 每路麦克风仍然只喂给对应的 LvaClient，因此重复唤醒词也不会串唤醒。
    def _keyword_display(kw: str) -> str:
        kw = kw.strip()
        if not kw:
            return ""
        if "@" in kw:
            return kw.split("@", 1)[1].strip()
        return kw.split()[0]

    groups: dict[tuple[str, ...], list] = {}
    for target in cfg.lva_targets:
        # 按“中文唤醒词本身”分组（兼容 @ 格式和旧 token 行）
        key = tuple(sorted({_keyword_display(kw) for kw in target.keywords if kw.strip()}))
        groups.setdefault(key, []).append(target)

    clients: list = []
    for key, targets in groups.items():
        kws = create_spotter(
            model_dir=cfg.kws_model_dir,
            keywords=list(key) or list(cfg.keywords),
            num_threads=cfg.kws_num_threads,
            fake=cfg.kws_fake,
            trigger_every=cfg.kws_fake_trigger_every,
        )
        for target in targets:
            stream_id = kws.create_stream()
            if target.protocol == "json":
                clients.append(JsonLvaClient(cfg, target, kws, stream_id))
            else:
                clients.append(LvaClient(cfg, target, kws, stream_id))

    tasks = [asyncio.create_task(client.run()) for client in clients]

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows 等平台不支持 add_signal_handler

    # 每个 LVA 一个独立麦克风：来源隔离，重复唤醒词也只唤醒对应 LVA
    audio_stop = threading.Event()
    audio_sources: list[AudioSource] = []
    audio_threads: list[threading.Thread] = []
    for client in clients:
        src_spec = client._target.audio_source or cfg.audio_source
        audio = create_audio_source(src_spec, cfg.audio_sample_rate)
        audio_sources.append(audio)
        thread = threading.Thread(
            target=_source_loop,
            args=(audio, client, cfg, audio_stop),
            name=f"audio-{client._target.device_name}",
            daemon=True,
        )
        thread.start()
        audio_threads.append(thread)

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        audio_stop.set()
        for audio in audio_sources:
            audio.stop()
        for client in clients:
            client.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for thread in audio_threads:
            thread.join(timeout=5)
    logger.info("sherpa-onnx-kws 已退出")


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
