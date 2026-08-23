# -*- coding: utf-8 -*-
"""音频采集源：PulseAudio(parecord) / ALSA(arecord) / 文件(测试用)。

所有来源统一输出：16 kHz、单声道、s16le 原始 PCM 字节流。
read(n_frames) 阻塞读取，返回 n_frames * 2 字节。
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
import wave

logger = logging.getLogger("audio")


class AudioSourceError(Exception):
    pass


def _read_exact(stream, n: int) -> bytes:
    data = bytearray()
    while len(data) < n:
        chunk = stream.read(n - len(data))
        if not chunk:
            raise AudioSourceError(f"音频流提前结束（已读 {len(data)}/{n} 字节）")
        data.extend(chunk)
    return bytes(data)


def linear_resample(pcm16: bytes, src_rate: int, dst_rate: int) -> bytes:
    """线性插值重采样（s16le, mono）。src_rate == dst_rate 时原样返回。"""
    if src_rate == dst_rate:
        return pcm16
    try:
        import numpy as np
    except ImportError:
        raise AudioSourceError("重采样需要 numpy，请安装 numpy 或将采样率设为一致")

    src = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32)
    n_out = max(1, int(round(len(src) * dst_rate / src_rate)))
    idx = np.arange(n_out, dtype=np.float32) * (len(src) - 1) / max(n_out - 1, 1)
    i0 = np.floor(idx).astype(np.int64)
    i1 = np.minimum(i0 + 1, len(src) - 1)
    frac = (idx - i0).astype(np.float32)
    out = src[i0] * (1.0 - frac) + src[i1] * frac
    return np.clip(out, -32768, 32767).astype(np.int16).tobytes()


class AudioSource:
    """基类：16k mono s16le 采集源。"""

    sample_rate: int = 16000
    num_channels: int = 1

    def start(self) -> None:  # pragma: no cover
        raise NotImplementedError

    def read(self, n_frames: int) -> bytes:  # pragma: no cover
        raise NotImplementedError

    def stop(self) -> None:  # pragma: no cover
        raise NotImplementedError

    @property
    def description(self) -> str:
        return type(self).__name__


class _SubprocessSource(AudioSource):
    """通过外部命令 stdout 读取原始 PCM。命令失败时自动重启。"""

    _CMD: list[str] = []
    _STDERR_TAIL = 20

    def __init__(self, restart_delay: float = 2.0) -> None:
        self._proc: subprocess.Popen | None = None
        self._restart_delay = restart_delay
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._stderr_tail: list[str] = []
        self._tail_lock = threading.Lock()

    def _spawn(self) -> subprocess.Popen:
        logger.info("启动音频采集: %s", " ".join(self._CMD))
        proc = subprocess.Popen(
            self._CMD,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
        with self._tail_lock:
            self._stderr_tail = []

        def _drain() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                line = line.decode(errors="replace").rstrip("\n")
                if not line:
                    continue
                with self._tail_lock:
                    self._stderr_tail.append(line)
                    if len(self._stderr_tail) > self._STDERR_TAIL:
                        del self._stderr_tail[:-self._STDERR_TAIL]

        threading.Thread(target=_drain, daemon=True, name="audio-stderr").start()
        return proc

    def _dump_stderr(self, proc: subprocess.Popen) -> None:
        with self._tail_lock:
            tail = list(self._stderr_tail)
        if tail:
            logger.warning("音频采集命令 stderr（exit=%s）:\n%s",
                           proc.poll(), "\n".join(tail))
            logger.warning(
                "若为 PulseAudio 连接类错误（Connection refused / No such file），"
                "请检查容器内 PULSE_SERVER 环境变量与 docker-compose.yml 的 "
                "PULSE_SOCKET 挂载路径（须与 LVA 连宿主 Pulse 的 socket 一致）")

    def start(self) -> None:
        self._stopped.clear()
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._proc = self._spawn()

    def read(self, n_frames: int) -> bytes:
        n_bytes = n_frames * 2
        while True:
            with self._lock:
                proc = self._proc
            if proc is None:
                self.start()
                continue
            try:
                return _read_exact(proc.stdout, n_bytes)
            except AudioSourceError:
                logger.warning("音频采集进程退出（exit=%s），%.1fs 后重启",
                               proc.poll(), self._restart_delay)
                self._dump_stderr(proc)
                with self._lock:
                    if self._proc is proc:
                        self._proc = None
                if self._stopped.is_set():
                    raise
                time.sleep(self._restart_delay)

    def stop(self) -> None:
        self._stopped.set()
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            logger.info("音频采集已停止")


class PulseSource(_SubprocessSource):
    """PulseAudio / PipeWire-pulse 采集。"""

    def __init__(self, device: str = "@DEFAULT_SOURCE@", sample_rate: int = 16000,
                 restart_delay: float = 2.0) -> None:
        super().__init__(restart_delay)
        self.sample_rate = sample_rate
        self._CMD = [
            "parecord",
            "--raw",
            "--format=s16le",
            f"--rate={sample_rate}",
            "--channels=1",
            "--device",
            device,
        ]
        self._device = device

    @property
    def description(self) -> str:
        return f"PulseAudio({self._device})"


class AlsaSource(_SubprocessSource):
    """ALSA 采集。需要 --device /dev/snd 与 audio 组权限。"""

    def __init__(self, pcm: str = "default", sample_rate: int = 16000,
                 restart_delay: float = 2.0) -> None:
        super().__init__(restart_delay)
        self.sample_rate = sample_rate
        self._CMD = [
            "arecord",
            "-q",
            "-D",
            pcm,
            "-f",
            "S16_LE",
            "-r",
            str(sample_rate),
            "-c",
            "1",
            "-t",
            "raw",
        ]
        self._pcm = pcm

    @property
    def description(self) -> str:
        return f"ALSA({self._pcm})"


class FileSource(AudioSource):
    """从 WAV 文件循环读取（16k mono s16le；其他格式会做重采样/降混）。"""

    def __init__(self, path: str, restart_delay: float = 0.5) -> None:
        self._path = path
        self._restart_delay = restart_delay
        self._frames = b""
        self._pos = 0
        self._src_rate = 16000
        self._lock = threading.Lock()
        self._stopped = threading.Event()

    def start(self) -> None:
        self._stopped.clear()
        with self._lock:
            self._load()

    def _load(self) -> None:
        with wave.open(self._path, "rb") as wf:
            rate = wf.getframerate()
            channels = wf.getnchannels()
            width = wf.getsampwidth()
            raw = wf.readframes(wf.getnframes())
        self._src_rate = rate
        if channels == 1 and width == 2 and rate == 16000:
            self._frames = raw
        else:
            try:
                import numpy as np
            except ImportError:
                raise AudioSourceError("处理多声道/非 16k WAV 需要 numpy")
            audio = np.frombuffer(raw, dtype=np.int16)
            audio = audio.reshape(-1, channels)
            mono = audio.mean(axis=1).astype(np.int16)
            self._frames = linear_resample(mono.tobytes(), rate, 16000)
        self._pos = 0
        logger.info("测试音频文件已加载: %s (%dHz, %d声道, %.2fs)",
                    self._path, rate, channels, len(self._frames) / 2 / rate)

    def read(self, n_frames: int) -> bytes:
        n_bytes = n_frames * 2
        while True:
            with self._lock:
                frames = self._frames
                pos = self._pos
            if not frames:
                if self._stopped.is_set():
                    raise AudioSourceError("音频源已停止")
                time.sleep(self._restart_delay)
                with self._lock:
                    self._load()
                continue
            out = bytearray()
            remaining = n_bytes
            while remaining > 0:
                take = min(remaining, len(frames) - pos)
                out.extend(frames[pos: pos + take])
                pos += take
                remaining -= take
                if pos >= len(frames):
                    pos = 0
            with self._lock:
                self._pos = pos
            return bytes(out)

    def stop(self) -> None:
        self._stopped.set()

    @property
    def description(self) -> str:
        return f"File({self._path})"


class SynthSource(AudioSource):
    """合成正弦波音频源（纯内存，无需文件/麦克风）。"""

    def __init__(self, freq: float = 440.0, sample_rate: int = 16000,
                 amplitude: float = 0.2) -> None:
        self.sample_rate = sample_rate
        self._freq = freq
        self._amp = amplitude
        self._phase = 0.0

    def start(self) -> None:
        pass

    def read(self, n_frames: int) -> bytes:
        import math
        import struct

        out = bytearray()
        step = 2.0 * math.pi * self._freq / self.sample_rate
        for _ in range(n_frames):
            v = int(32767 * self._amp * math.sin(self._phase))
            out.extend(struct.pack("<h", v))
            self._phase = (self._phase + step) % (2.0 * math.pi)
        return bytes(out)

    def stop(self) -> None:
        pass

    @property
    def description(self) -> str:
        return f"Synth({self._freq}Hz)"


def create_audio_source(spec: str, sample_rate: int = 16000, restart_delay: float = 2.0) -> AudioSource:
    """根据配置字符串创建音频源。

    spec 取值:
      pulse            -> PulseAudio 默认源
      pulse:<device>   -> PulseAudio 指定源
      alsa[:pcm]       -> ALSA，默认 pcm=default
      file:<wav路径>   -> 循环播放 WAV（测试）
      synth[:频率Hz]   -> 合成正弦波（无麦克风联调）
    """
    kind, _, arg = spec.partition(":")
    if kind == "pulse":
        return PulseSource(device=arg or "@DEFAULT_SOURCE@", sample_rate=sample_rate,
                           restart_delay=restart_delay)
    if kind == "alsa":
        return AlsaSource(pcm=arg or "default", sample_rate=sample_rate,
                          restart_delay=restart_delay)
    if kind == "file":
        return FileSource(path=arg, restart_delay=restart_delay)
    if kind == "synth":
        try:
            freq = float(arg) if arg else 440.0
        except ValueError:
            freq = 440.0
        return SynthSource(freq=freq, sample_rate=sample_rate)
    raise ValueError(f"未知音频源: {spec!r}（支持 pulse / alsa / file:<wav> / synth）")
