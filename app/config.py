# -*- coding: utf-8 -*-
"""配置：支持单容器内连接多个 LVA，每个 LVA 独立监听/唤醒/推流。

环境变量约定：
  LVA_URLS=ws://lva1:10700/api/peripheral,ws://lva2:10700/api/peripheral
  LVA_NAMES=kws-1,kws-2                 # 可选，默认 sherpa-onnx-kws-1...
  LVA_UUID_FILES=/data/uuid1,/data/uuid2  # 可选，默认 /data/uuid_1...
  LVA_KEYWORDS=你好小智|小智小智,你好同学   # 可选，| 分隔每个 LVA，组内逗号分隔多唤醒词
  LVA_AUDIO_SOURCES=pulse:dev1|alsa:plughw:1,0  # 可选，| 分隔每个 LVA 的麦克风
  LVA_PROTOCOL=json                          # 可选：protobuf（旧协议）或 json（当前 LVA 官方协议）
  LVA_PROTOCOLS=json|protobuf                # 可选，| 分隔每个 LVA 的协议

兼容旧变量：LVA_URL、DEVICE_NAME、DEVICE_UUID_FILE、KEYWORDS、AUDIO_SOURCE。
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


@dataclass
class LvaTarget:
    url: str
    device_name: str
    device_uuid_file: str
    device_uuid: str = ""
    keywords: list[str] = field(default_factory=list)
    audio_source: str = ""
    protocol: str = "protobuf"


@dataclass
class Config:
    # ---- 多 LVA 连接 ----
    lva_targets: list[LvaTarget] = field(default_factory=list)
    reconnect_min_s: float = 1.0
    reconnect_max_s: float = 30.0
    read_timeout_s: float = 300.0

    # ---- 外设身份（所有 LVA 共用的语言；设备名/UUID 见 LvaTarget）----
    supported_languages: list[str] = field(default_factory=lambda: ["zh_CN", "en_US"])

    # ---- 唤醒 ----
    keywords: list[str] = field(default_factory=lambda: ["你好小智"])
    kws_model_dir: str = "/opt/kws-model"
    kws_num_threads: int = 2
    kws_fake: bool = False
    kws_fake_trigger_every: int = 5
    wake_cooldown_s: float = 3.0
    wake_buffer_s: float = 2.0
    wake_start_timeout_s: float = 8.0

    # ---- 音频 ----
    audio_source: str = "pulse"
    audio_sample_rate: int = 16000
    chunk_ms: int = 100
    max_stream_seconds: float = 120.0

    # ---- 其他 ----
    log_level: str = "INFO"
    log_protocol_hex: bool = False


def _load_device_uuid(uuid_file: str) -> str:
    """持久化 device_uuid（LVA 按 UUID 记住外设设置）。"""
    if os.path.exists(uuid_file):
        try:
            with open(uuid_file, "r", encoding="utf-8") as f:
                v = f.read().strip()
            if v:
                return v
        except OSError:
            pass
    v = str(uuid.uuid4())
    try:
        os.makedirs(os.path.dirname(uuid_file) or ".", exist_ok=True)
        with open(uuid_file, "w", encoding="utf-8") as f:
            f.write(v)
    except OSError:
        pass
    return v


def _split_list(value: str) -> list[str]:
    return [s.strip() for s in value.replace("，", ",").split(",") if s.strip()]


def _load_lva_targets(cfg: Config) -> list[LvaTarget]:
    raw_urls = os.environ.get("LVA_URLS", "").strip()
    if not raw_urls:
        raw_urls = os.environ.get("LVA_URL", "ws://127.0.0.1:10700/api/peripheral")
    urls = _split_list(raw_urls)
    if not urls:
        urls = ["ws://127.0.0.1:10700/api/peripheral"]

    names_raw = os.environ.get("LVA_NAMES", "").strip()
    names = _split_list(names_raw) if names_raw else []
    if not names:
        default_name = os.environ.get("DEVICE_NAME", "sherpa-onnx-kws")
        names = [default_name if i == 0 else f"{default_name}-{i + 1}" for i in range(len(urls))]

    uuid_files_raw = os.environ.get("LVA_UUID_FILES", "").strip()
    uuid_files = _split_list(uuid_files_raw) if uuid_files_raw else []
    if not uuid_files:
        default_uuid_file = os.environ.get("DEVICE_UUID_FILE", "/data/device_uuid")
        uuid_files = [
            default_uuid_file if i == 0 else f"/data/uuid_{i + 1}"
            for i in range(len(urls))
        ]

    # LVA_KEYWORDS：用 | 分隔每个 LVA，组内用逗号分隔多个唤醒词
    raw_groups = os.environ.get("LVA_KEYWORDS", "").strip()
    groups: list[list[str]] = []
    if raw_groups:
        for group in raw_groups.split("|"):
            kws = [s.strip() for s in group.replace("，", ",").split(",") if s.strip()]
            groups.append(kws or list(cfg.keywords))
    while len(groups) < len(urls):
        groups.append(list(cfg.keywords))

    # LVA_AUDIO_SOURCES：用 | 分隔，每个 LVA 一个麦克风；不填则全部用全局 AUDIO_SOURCE。
    # 用 | 而不是逗号，是因为 ALSA PCM 名可能带逗号（如 alsa:plughw:1,0）。
    audio_sources_raw = os.environ.get("LVA_AUDIO_SOURCES", "").strip()
    audio_sources = [s.strip() for s in audio_sources_raw.split("|") if s.strip()] if audio_sources_raw else []

    # LVA_PROTOCOL：全局默认；LVA_PROTOCOLS：按 LVA 用 | 分隔覆盖
    default_protocol = os.environ.get("LVA_PROTOCOL", "protobuf").strip().lower() or "protobuf"
    protocols_raw = os.environ.get("LVA_PROTOCOLS", "").strip()
    protocols = [p.strip().lower() for p in protocols_raw.split("|") if p.strip()] if protocols_raw else []

    targets: list[LvaTarget] = []
    for i, url in enumerate(urls):
        targets.append(LvaTarget(
            url=url,
            device_name=names[i] if i < len(names) else f"sherpa-onnx-kws-{i + 1}",
            device_uuid_file=uuid_files[i] if i < len(uuid_files) else f"/data/uuid_{i + 1}",
            keywords=groups[i] if i < len(groups) else list(cfg.keywords),
            audio_source=audio_sources[i] if i < len(audio_sources) else cfg.audio_source,
            protocol=protocols[i] if i < len(protocols) else default_protocol,
        ))
    for t in targets:
        t.device_uuid = _load_device_uuid(t.device_uuid_file)
    return targets


def load_config() -> Config:
    cfg = Config()
    cfg.reconnect_min_s = _env_float("RECONNECT_MIN_S", cfg.reconnect_min_s)
    cfg.reconnect_max_s = _env_float("RECONNECT_MAX_S", cfg.reconnect_max_s)
    cfg.read_timeout_s = _env_float("READ_TIMEOUT_S", cfg.read_timeout_s)

    if os.environ.get("SUPPORTED_LANGUAGES"):
        cfg.supported_languages = _split_list(os.environ["SUPPORTED_LANGUAGES"])

    if os.environ.get("KEYWORDS"):
        cfg.keywords = _split_list(os.environ["KEYWORDS"])
    cfg.kws_model_dir = os.environ.get("KWS_MODEL_DIR", cfg.kws_model_dir)
    cfg.kws_num_threads = _env_int("KWS_NUM_THREADS", cfg.kws_num_threads)
    cfg.kws_fake = _env_bool("KWS_FAKE", False)
    cfg.kws_fake_trigger_every = _env_int("KWS_FAKE_TRIGGER_EVERY", cfg.kws_fake_trigger_every)
    cfg.wake_cooldown_s = _env_float("WAKE_COOLDOWN_S", cfg.wake_cooldown_s)
    cfg.wake_buffer_s = _env_float("WAKE_BUFFER_S", cfg.wake_buffer_s)
    cfg.wake_start_timeout_s = _env_float("WAKE_START_TIMEOUT_S", cfg.wake_start_timeout_s)

    cfg.audio_source = os.environ.get("AUDIO_SOURCE", cfg.audio_source)
    cfg.audio_sample_rate = _env_int("AUDIO_SAMPLE_RATE", cfg.audio_sample_rate)
    cfg.chunk_ms = _env_int("CHUNK_MS", cfg.chunk_ms)
    cfg.max_stream_seconds = _env_float("MAX_STREAM_SECONDS", cfg.max_stream_seconds)

    cfg.log_level = os.environ.get("LOG_LEVEL", cfg.log_level).upper()
    cfg.log_protocol_hex = _env_bool("LOG_PROTOCOL_HEX", False)

    cfg.lva_targets = _load_lva_targets(cfg)
    return cfg
