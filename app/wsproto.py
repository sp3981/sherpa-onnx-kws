# -*- coding: utf-8 -*-
"""极简 RFC 6455 WebSocket 实现（纯标准库，无第三方依赖）。

仅实现本项目所需的最小集合：
  - 客户端握手（Sec-WebSocket-Key / Accept）
  - 二进制/文本帧收发（客户端帧带掩码，服务端帧不带掩码）
  - 分片消息重组、ping/pong、close
不协商任何扩展（无 permessage-deflate）。

参考：RFC 6455 https://datatracker.ietf.org/doc/html/rfc6455
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

_OP_NAMES = {
    OP_CONT: "continuation",
    OP_TEXT: "text",
    OP_BINARY: "binary",
    OP_CLOSE: "close",
    OP_PING: "ping",
    OP_PONG: "pong",
}


class WsError(Exception):
    """WebSocket 协议/连接错误。"""


class WsClosed(Exception):
    """对端发送了 close 帧，连接已关闭。"""

    def __init__(self, code: int = 1000, reason: str = ""):
        super().__init__(f"websocket closed: code={code} reason={reason!r}")
        self.code = code
        self.reason = reason


def _build_handshake_request(host: str, port: int, path: str) -> tuple[bytes, str]:
    """构造客户端握手请求，返回 (请求字节, Sec-WebSocket-Key)。"""
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
        "",
        "",
    ]
    return ("\r\n".join(lines)).encode("ascii"), key


async def client_handshake(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, host: str, port: int, path: str
) -> None:
    """执行客户端 WebSocket 握手，失败抛 WsError。"""
    request, key = _build_handshake_request(host, port, path)
    writer.write(request)
    await writer.drain()

    status_line = await asyncio.wait_for(reader.readline(), timeout=30)
    if not status_line or not status_line.startswith(b"HTTP/1.1 101"):
        raise WsError(f"websocket 握手被拒绝: {status_line.decode('utf-8', 'replace').strip()!r}")

    headers: dict[str, str] = {}
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout=30)
        if line in (b"\r\n", b"\n", b""):
            break
        try:
            k, _, v = line.decode("utf-8", "replace").partition(":")
            headers[k.strip().lower()] = v.strip()
        except Exception:
            continue

    expected = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
    if headers.get("sec-websocket-accept") != expected:
        raise WsError("websocket 握手校验失败: Sec-WebSocket-Accept 不匹配")


def compute_accept(key: str) -> str:
    """服务端握手用：根据 Sec-WebSocket-Key 计算 Sec-WebSocket-Accept。"""
    return base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")


async def server_handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> str:
    """服务端握手（供 mock/测试使用），返回请求 path。"""
    request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
    lines = request.decode("utf-8", "replace").split("\r\n")
    method, path, _ = lines[0].split(" ", 2)
    if method != "GET":
        raise WsError(f"非 GET 请求: {method}")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    key = headers.get("sec-websocket-key", "")
    if not key:
        raise WsError("缺少 Sec-WebSocket-Key")
    accept = compute_accept(key)
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    ).encode("ascii")
    writer.write(response)
    await writer.drain()
    return path


def encode_frame(
    opcode: int, payload: bytes, mask: bool = True, mask_key: bytes | None = None
) -> bytes:
    """编码一个 WebSocket 帧。客户端帧必须掩码（mask=True）。"""
    if mask_key is None:
        mask_key = os.urandom(4)
    n = len(payload)
    b0 = 0x80 | (opcode & 0x0F)
    if n < 126:
        header = bytes([b0, (0x80 if mask else 0x00) | n])
    elif n < 65536:
        header = bytes([b0, (0x80 if mask else 0x00) | 126]) + struct.pack(">H", n)
    else:
        header = bytes([b0, (0x80 if mask else 0x00) | 127]) + struct.pack(">Q", n)
    if not mask:
        return header + payload
    masked = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))
    return header + mask_key + masked


async def _read_exact(reader: asyncio.StreamReader, n: int) -> bytes:
    if n == 0:
        return b""
    try:
        return await reader.readexactly(n)
    except asyncio.IncompleteReadError as e:
        raise WsError(f"连接中断: 期望 {n} 字节，仅读到 {len(e.partial)}") from e


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    """读取一帧，返回 (opcode, payload)。"""
    header = await _read_exact(reader, 2)
    b0, b1 = header[0], header[1]
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", await _read_exact(reader, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", await _read_exact(reader, 8))[0]
    mask_key = await _read_exact(reader, 4) if masked else None
    payload = await _read_exact(reader, n)
    if masked:
        payload = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))  # type: ignore[index]
    if not fin and opcode == OP_CONT:
        pass
    return opcode, payload


async def send_frame(
    writer: asyncio.StreamWriter, opcode: int, payload: bytes, mask: bool = True
) -> None:
    writer.write(encode_frame(opcode, payload, mask=mask))
    await writer.drain()


async def recv_message(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> tuple[int, bytes]:
    """读取一条完整消息（自动重组分片、响应 ping、识别 close）。

    返回 (opcode, payload)。对端 close 时抛 WsClosed。
    """
    fragments = bytearray()
    message_opcode: int | None = None
    while True:
        opcode, payload = await read_frame(reader)
        if opcode == OP_PING:
            await send_frame(writer, OP_PONG, payload)
            continue
        if opcode == OP_PONG:
            continue
        if opcode == OP_CLOSE:
            code, reason = 1000, ""
            if len(payload) >= 2:
                code = struct.unpack(">H", payload[:2])[0]
                reason = payload[2:].decode("utf-8", "replace")
            await send_frame(writer, OP_CLOSE, payload[:2] if len(payload) >= 2 else b"")
            raise WsClosed(code, reason)
        if opcode == OP_CONT:
            if message_opcode is None:
                raise WsError("收到无起点的 continuation 帧")
            fragments.extend(payload)
            continue
        if opcode in (OP_TEXT, OP_BINARY):
            if message_opcode is not None:
                fragments.clear()
            message_opcode = opcode
            fragments.extend(payload)
            return opcode, bytes(fragments)
        raise WsError(f"未知帧 opcode: 0x{opcode:x} ({_OP_NAMES.get(opcode, '?')})")
