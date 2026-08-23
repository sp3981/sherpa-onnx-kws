# sherpa-onnx-kws：多麦克风多 LVA 中文唤醒词外设

本项目把带麦克风的 Linux 设备变成 **多个 Linux Voice Assistant (LVA) 的唤醒外设**。
每个 LVA 对应一路独立麦克风，即使多个 LVA 使用相同的唤醒词，也只会唤醒真正听到声音的那一路。

支持两种对接协议：

- `json`：对接当前官方 LVA 的 JSON peripheral API（推荐）
- `protobuf`：对接旧版 LVA protobuf 音频外设协议（推流模式）

## 特性

- **单容器多 LVA**：一个进程同时连接多个 LVA
- **多路麦克风来源隔离**：每路麦克风只喂给对应的 LVA，重复唤醒词不会串唤醒
- **按来源 + 唤醒词唤醒**：A 房间听到“你好小智”只唤醒 A 房间的 LVA
- **JSON 模式**：本地 KWS 命中后向对应 LVA 发送 `start_listening`
- **protobuf 模式**：本地 KWS 命中后向 LVA 推流预唤醒缓冲 + 后续音频
- **中文唤醒词自动转拼音 token**：`KEYWORDS=你好小智` 即可，无需手工填 token
- **纯 Python 标准库 WebSocket**：无 websockets / websocket-client 依赖

## 架构

### JSON 模式（当前官方 LVA）

```
麦克风 1 ──> 本地 KWS ──> {"command":"start_listening"} ──> LVA 1（音响 1）
麦克风 2 ──> 本地 KWS ──> {"command":"start_listening"} ──> LVA 2（音响 2）
麦克风 N ──> 本地 KWS ──> {"command":"start_listening"} ──> LVA N（音响 N）
```

LVA 自己从 PulseAudio 采集后续语音并播放音响；本项目只负责“本地 KWS 检测 + 按来源触发对应 LVA”。

### protobuf 模式（旧版 LVA）

```
麦克风 1 ──> 音频线程 1 ──> LvaClient 1 (KWS stream 1, WS -> LVA 1)
麦克风 2 ──> 音频线程 2 ──> LvaClient 2 (KWS stream 2, WS -> LVA 2)
```

## 快速开始

```bash
cp .env.example .env
# 编辑 .env
docker compose up -d --build
docker compose logs -f kws
```

### 多 LVA 配置示例

```env
LVA_PROTOCOL=json

LVA_URLS=ws://192.168.10.5:7001,ws://192.168.10.6:7001
LVA_NAMES=bedroom1,bedroom2
LVA_UUID_FILES=/data/uuid1,/data/uuid2
LVA_KEYWORDS=你好小智|你好小智,小智小智
LVA_AUDIO_SOURCES=pulse:alsa_input.usb-mic-1|pulse:alsa_input.usb-mic-2
```

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `LVA_PROTOCOL` | `protobuf` | `json`（当前官方 LVA）或 `protobuf`（旧协议） |
| `LVA_PROTOCOLS` | 空 | 按 LVA 用 `\|` 分隔覆盖协议 |
| `LVA_URLS` | 空 | 逗号分隔多个 LVA WebSocket 地址 |
| `LVA_URL` | `ws://127.0.0.1:10700/api/peripheral` | 单 LVA 兼容 |
| `LVA_NAMES` | 自动生成 | 逗号分隔设备名 |
| `LVA_UUID_FILES` | 自动生成 | 逗号分隔 UUID 持久化文件 |
| `LVA_KEYWORDS` | 空 | 按 LVA 用 `\|` 分隔唤醒词，组内逗号分隔 |
| `LVA_AUDIO_SOURCES` | 空 | 按 LVA 用 `\|` 分隔麦克风 |
| `KEYWORDS` | `你好小智` | 所有 LVA 共用的唤醒词 |
| `KWS_MODEL_DIR` | `/opt/kws-model` | 模型目录 |
| `KWS_NUM_THREADS` | `2` | 推理线程数 |
| `KWS_FAKE` | `0` | `1`=不加载模型，定时假命中 |
| `AUDIO_SOURCE` | `pulse` | 全局音频源 |
| `AUDIO_SAMPLE_RATE` | `16000` | 采集采样率 |
| `CHUNK_MS` | `100` | 分块时长 |
| `WAKE_COOLDOWN_S` | `3` | 两次唤醒最小间隔 |
| `WAKE_BUFFER_S` | `2` | protobuf 模式预唤醒缓冲时长 |
| `WAKE_START_TIMEOUT_S` | `8` | protobuf 模式等待 Start 超时 |
| `MAX_STREAM_SECONDS` | `120` | protobuf 模式推流上限 |
| `DEVICE_NAME` | `sherpa-onnx-kws` | 单 LVA 兼容设备名 |
| `DEVICE_UUID_FILE` | `/data/device_uuid` | 单 LVA 兼容 UUID 文件 |
| `SUPPORTED_LANGUAGES` | `zh_CN,en_US` | 上报语言 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `LOG_PROTOCOL_HEX` | `0` | 打印协议帧十六进制 |
| `PULSE_SOCKET` | `/run/user/1000/pulse` | PulseAudio socket 目录 |
| `PULSE_SERVER` | 自动生成 | 可手动覆盖 |
| `AUDIO_GID` | `audio` | 容器内 audio 组 |
| `DOWNLOAD_MODEL` | `1` | 构建时是否下载模型 |
| `APT_MIRROR` | `mirrors.aliyun.com` | apt 源 |
| `PIP_INDEX_URL` | 清华源 | pip 源 |
| `GH_PROXY` | 空 | GitHub 加速前缀 |

## 查询音频源

### PulseAudio / PipeWire

```bash
pactl list short sources
pactl get-default-source
```

配置：

```env
AUDIO_SOURCE=pulse:alsa_input.usb-0d8c_C-Media_USB_Audio_Device-00.analog-stereo
```

### ALSA

```bash
arecord -l
arecord -L
```

配置：

```env
AUDIO_SOURCE=alsa:plughw:1,0
```

## 中文唤醒词自动转拼音

本项目使用原模型 `tokens.txt` 的 **拼音声母 + 韵母（带声调）** 格式。

例如：

```text
你好小智 -> n ǐ h ǎo x iǎo zh ì @你好小智
```

`KEYWORDS` 只需填中文，启动时自动转换。

## 开发与测试

```bash
python -m unittest discover -s tests -v
```

## 参考资料

- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)
- [Linux Voice Assistant (LVA)](https://github.com/OHF-Voice/linux-voice-assistant)
- [sherpa-onnx KWS 中文模型](https://github.com/k2-fsa/sherpa-onnx/releases/tag/kws-models)
- [wunuo1/sherpa-onnx-kws](https://github.com/wunuo1/sherpa-onnx-kws)
- [smartdeng 的 WebSocket ASR 服务器](https://www.smartdeng.com/zh/posts/algorithm/websocket-asr-server.html)

## License

[MIT](./LICENSE)
