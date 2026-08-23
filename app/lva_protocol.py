# -*- coding: utf-8 -*-
"""Linux Voice Assistant (OHF-Voice/linux-voice-assistant) peripheral WebSocket 协议。

对应 LVA 仓库中的 `linux_voice_assistant/schema/protocol/websocket.proto`。
为减少依赖，这里手工实现 protobuf wire format 编解码（仅覆盖本项目用到的消息）。

帧格式（二进制 WebSocket 消息）：
    ┌──────────────┬──────────────────────┬─────────────────────────┐
    │ 消息类型 1B  │ 载荷长度 4B (大端序) │ protobuf 载荷            │
    └──────────────┴──────────────────────┴─────────────────────────┘

消息方向（相对 addon 而言）：
    发送: MicrophoneProfile(7)  WakeWordEvent(10)  RecordedAudioChunk(1)
    接收: StreamSettings(4)  Ack(5)  Start(8)  Stop(9)  Error(6)
           TranscriptionEvent(2)  Command(3)  SessionContext(11)

注意：若你使用的 LVA 版本协议字段有出入，只需修改本文件中的字段号常量。
调试时设置 LOG_LEVEL=DEBUG 可打印原始帧十六进制。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 消息类型
# ---------------------------------------------------------------------------


class MsgType:
    UNKNOWN = 0
    RECORDED_AUDIO_CHUNK = 1
    TRANSCRIPTION_EVENT = 2
    COMMAND = 3
    STREAM_SETTINGS = 4
    ACK = 5
    ERROR = 6
    MICROPHONE_PROFILE = 7
    START = 8
    STOP = 9
    WAKE_WORD_EVENT = 10
    SESSION_CONTEXT = 11

    _NAMES = {
        0: "UNKNOWN",
        1: "RECORDED_AUDIO_CHUNK",
        2: "TRANSCRIPTION_EVENT",
        3: "COMMAND",
        4: "STREAM_SETTINGS",
        5: "ACK",
        6: "ERROR",
        7: "MICROPHONE_PROFILE",
        8: "START",
        9: "STOP",
        10: "WAKE_WORD_EVENT",
        11: "SESSION_CONTEXT",
    }

    @classmethod
    def name(cls, value: int) -> str:
        return cls._NAMES.get(value, f"UNKNOWN({value})")


class AudioFormat:
    UNKNOWN = 0
    WAVE = 1
    FLAC = 2
    MP3 = 3
    OGG_OPUS = 4
    OGG_VORBIS = 5
    PCM16 = 6


# ---------------------------------------------------------------------------
# 帧编解码
# ---------------------------------------------------------------------------


def encode_frame(msg_type: int, payload: bytes) -> bytes:
    return bytes([msg_type & 0xFF]) + struct.pack(">I", len(payload)) + payload


def decode_frame(data: bytes) -> tuple[int, bytes]:
    if len(data) < 5:
        raise ValueError(f"帧太短: {len(data)} 字节")
    msg_type = data[0]
    length = struct.unpack(">I", data[1:5])[0]
    if len(data) < 5 + length:
        raise ValueError(f"帧不完整: 声明 {length} 字节，实际 {len(data) - 5}")
    return msg_type, bytes(data[5: 5 + length])


# ---------------------------------------------------------------------------
# 极简 protobuf wire format 编解码
# ---------------------------------------------------------------------------

WIRE_VARINT = 0
WIRE_64BIT = 1
WIRE_LEN = 2
WIRE_32BIT = 5


class DecodeError(Exception):
    pass


class _Writer:
    __slots__ = ("buf",)

    def __init__(self) -> None:
        self.buf = bytearray()

    def varint(self, n: int) -> None:
        n &= (1 << 64) - 1
        while True:
            b = n & 0x7F
            n >>= 7
            if n:
                self.buf.append(b | 0x80)
            else:
                self.buf.append(b)
                break

    def tag(self, field_no: int, wire: int) -> None:
        self.varint((field_no << 3) | wire)

    def uint(self, field_no: int, n: int) -> None:
        if n:
            self.tag(field_no, WIRE_VARINT)
            self.varint(n)

    def boolean(self, field_no: int, value: bool) -> None:
        if value:
            self.tag(field_no, WIRE_VARINT)
            self.varint(1)

    def string(self, field_no: int, s: str) -> None:
        if s:
            self.bytes_(field_no, s.encode("utf-8"))

    def bytes_(self, field_no: int, data: bytes) -> None:
        if data:
            self.tag(field_no, WIRE_LEN)
            self.varint(len(data))
            self.buf.extend(data)

    def done(self) -> bytes:
        return bytes(self.buf)


@dataclass
class _Field:
    number: int
    wire: int
    value: int | bytes


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.d = data
        self.pos = 0

    def varint(self) -> int:
        result = 0
        shift = 0
        while True:
            if self.pos >= len(self.d):
                raise DecodeError("截断的 varint")
            b = self.d[self.pos]
            self.pos += 1
            result |= (b & 0x7F) << shift
            if not (b & 0x80):
                return result
            shift += 7
            if shift > 70:
                raise DecodeError("varint 过长")

    def fields(self):
        while self.pos < len(self.d):
            key = self.varint()
            field_no = key >> 3
            wire = key & 0x07
            if field_no == 0:
                raise DecodeError("非法字段号 0")
            if wire == WIRE_VARINT:
                value = self.varint()
            elif wire == WIRE_LEN:
                n = self.varint()
                if self.pos + n > len(self.d):
                    raise DecodeError("截断的 length-delimited 字段")
                value = bytes(self.d[self.pos: self.pos + n])
                self.pos += n
            elif wire == WIRE_64BIT:
                if self.pos + 8 > len(self.d):
                    raise DecodeError("截断的 64-bit 字段")
                value = struct.unpack("<Q", self.d[self.pos: self.pos + 8])[0]
                self.pos += 8
            elif wire == WIRE_32BIT:
                if self.pos + 4 > len(self.d):
                    raise DecodeError("截断的 32-bit 字段")
                value = struct.unpack("<I", self.d[self.pos: self.pos + 4])[0]
                self.pos += 4
            else:
                raise DecodeError(f"不支持的 wire type: {wire}")
            yield _Field(field_no, wire, value)


def parse_fields(payload: bytes) -> list[_Field]:
    """解析 payload 为字段列表（保留重复字段与顺序）。"""
    return list(_Reader(payload).fields())


def _first_varint(fields: list[_Field], number: int) -> int:
    for f in fields:
        if f.number == number and f.wire == WIRE_VARINT and isinstance(f.value, int):
            return f.value
    return 0


def _first_bytes(fields: list[_Field], number: int) -> bytes:
    for f in fields:
        if f.number == number and f.wire == WIRE_LEN and isinstance(f.value, bytes):
            return f.value
    return b""


def _first_string(fields: list[_Field], number: int) -> str:
    return _first_bytes(fields, number).decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# 消息构造（addon -> LVA）
# ---------------------------------------------------------------------------


def build_microphone_profile(
    device_name: str, supported_languages: list[str], device_uuid: str
) -> bytes:
    """MicrophoneProfile: 1=device_name, 2=supported_languages(repeated), 3=device_uuid"""
    w = _Writer()
    w.string(1, device_name)
    for lang in supported_languages:
        w.string(2, lang)
    w.string(3, device_uuid)
    return w.done()


def build_recorded_audio_chunk(
    stt_session_id: int,
    samples: bytes,
    sample_rate_hz: int,
    num_channels: int,
    audio_format: int = AudioFormat.PCM16,
    command_message_id: int = 0,
) -> bytes:
    """RecordedAudioChunk:
    1=stt_session_id, 2=samples(bytes), 3=sample_rate_hz, 4=num_channels,
    5=audio_format, 6=command_message_id"""
    w = _Writer()
    w.uint(1, stt_session_id)
    w.bytes_(2, samples)
    w.uint(3, sample_rate_hz)
    w.uint(4, num_channels)
    w.uint(5, audio_format)
    w.uint(6, command_message_id)
    return w.done()


def build_wake_word_event(
    stt_session_id: int,
    sample_rate_hz: int,
    num_channels: int,
    audio_format: int = AudioFormat.PCM16,
) -> bytes:
    """WakeWordEvent: 1=stt_session_id, 2=sample_rate_hz, 3=num_channels, 4=audio_format"""
    w = _Writer()
    w.uint(1, stt_session_id)
    w.uint(2, sample_rate_hz)
    w.uint(3, num_channels)
    w.uint(4, audio_format)
    return w.done()


# ---------------------------------------------------------------------------
# 消息构造（LVA -> addon 方向，供 mock/调试工具使用）
# ---------------------------------------------------------------------------


def build_stream_settings(sample_rate_hz: int, chunk_duration_ms: int) -> bytes:
    w = _Writer()
    w.uint(1, sample_rate_hz)
    w.uint(2, chunk_duration_ms)
    return w.done()


def build_ack(ok: bool, message: str = "") -> bytes:
    w = _Writer()
    w.boolean(1, ok)
    w.string(2, message)
    return w.done()


def build_start(stt_session_id: int, send_partial_transcription_events: bool = False) -> bytes:
    w = _Writer()
    w.uint(1, stt_session_id)
    w.boolean(2, send_partial_transcription_events)
    return w.done()


def build_stop(stt_session_id: int) -> bytes:
    w = _Writer()
    w.uint(1, stt_session_id)
    return w.done()


def build_error(message: str) -> bytes:
    w = _Writer()
    w.string(1, message)
    return w.done()


def build_transcription_event(
    stt_session_id: int,
    text: str,
    is_command: bool = False,
    language_code: str = "",
    stability: float = 0.0,
) -> bytes:
    w = _Writer()
    w.uint(1, stt_session_id)
    w.string(2, text)
    w.boolean(3, is_command)
    w.string(4, language_code)
    if stability:
        w.tag(5, WIRE_32BIT)
        w.buf.extend(struct.pack("<f", stability))
    return w.done()


# ---------------------------------------------------------------------------
# 消息解析（LVA -> addon）
# ---------------------------------------------------------------------------


@dataclass
class Ack:
    ok: bool = False
    message: str = ""


@dataclass
class StreamSettings:
    sample_rate_hz: int = 0
    chunk_duration_ms: int = 0


@dataclass
class Start:
    stt_session_id: int = 0
    send_partial_transcription_events: bool = False


@dataclass
class Stop:
    stt_session_id: int = 0


@dataclass
class ErrorMsg:
    message: str = ""


@dataclass
class TranscriptionEvent:
    stt_session_id: int = 0
    text: str = ""
    is_command: bool = False
    language_code: str = ""
    stability: float = 0.0


@dataclass
class Command:
    stt_session_id: int = 0
    text: str = ""
    command_message_id: int = 0
    wake_word: str = ""


@dataclass
class SessionContext:
    language_code: str = ""
    stt_session_id: int = 0
    raw: list[_Field] = field(default_factory=list)


def parse_ack(payload: bytes) -> Ack:
    fields = parse_fields(payload)
    return Ack(
        ok=bool(_first_varint(fields, 1)),
        message=_first_string(fields, 2),
    )


def parse_stream_settings(payload: bytes) -> StreamSettings:
    """解析 StreamSettings。对不同 LVA 版本的字段号差异做容错：
    在字段 1-3 中挑出取值范围合理的 sample rate，字段 2-4 中挑出 chunk 时长。"""
    fields = parse_fields(payload)
    varints = {
        f.number: f.value
        for f in fields
        if f.wire == WIRE_VARINT and isinstance(f.value, int)
    }
    rate = 0
    for fno in (1, 2, 3):
        v = varints.get(fno)
        if v is not None and 8000 <= v <= 96000:
            rate = v
            break
    chunk_ms = 0
    for fno in (2, 3, 4):
        v = varints.get(fno)
        if v is not None and 20 <= v <= 5000:
            chunk_ms = v
            break
    return StreamSettings(sample_rate_hz=rate, chunk_duration_ms=chunk_ms)


def parse_start(payload: bytes) -> Start:
    fields = parse_fields(payload)
    sid = _first_varint(fields, 1)
    partial = bool(_first_varint(fields, 2))
    return Start(stt_session_id=sid, send_partial_transcription_events=partial)


def parse_stop(payload: bytes) -> Stop:
    fields = parse_fields(payload)
    return Stop(stt_session_id=_first_varint(fields, 1))


def parse_error(payload: bytes) -> ErrorMsg:
    fields = parse_fields(payload)
    return ErrorMsg(message=_first_string(fields, 1))


def parse_transcription_event(payload: bytes) -> TranscriptionEvent:
    fields = parse_fields(payload)
    stab = 0.0
    for f in fields:
        if f.number == 5 and f.wire == WIRE_32BIT and isinstance(f.value, int):
            stab = struct.unpack("<f", struct.pack("<I", f.value))[0]
    return TranscriptionEvent(
        stt_session_id=_first_varint(fields, 1),
        text=_first_string(fields, 2),
        is_command=bool(_first_varint(fields, 3)),
        language_code=_first_string(fields, 4),
        stability=stab,
    )


def parse_command(payload: bytes) -> Command:
    fields = parse_fields(payload)
    return Command(
        stt_session_id=_first_varint(fields, 1),
        text=_first_string(fields, 2),
        command_message_id=_first_varint(fields, 3),
        wake_word=_first_string(fields, 6),
    )


def parse_session_context(payload: bytes) -> SessionContext:
    fields = parse_fields(payload)
    return SessionContext(
        language_code=_first_string(fields, 1),
        stt_session_id=_first_varint(fields, 2),
        raw=fields,
    )


def parse_message(msg_type: int, payload: bytes):
    """按类型分发解析，未知类型返回 None。"""
    dispatch = {
        MsgType.ACK: parse_ack,
        MsgType.STREAM_SETTINGS: parse_stream_settings,
        MsgType.START: parse_start,
        MsgType.STOP: parse_stop,
        MsgType.ERROR: parse_error,
        MsgType.TRANSCRIPTION_EVENT: parse_transcription_event,
        MsgType.COMMAND: parse_command,
        MsgType.SESSION_CONTEXT: parse_session_context,
    }
    parser = dispatch.get(msg_type)
    if parser is None:
        return None
    try:
        return parser(payload)
    except DecodeError:
        return None
