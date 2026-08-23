# syntax=docker/dockerfile:1
# 单容器多 LVA 中文唤醒词外设。
# 构建: docker build -t sherpa-onnx-kws .
FROM python:3.11-slim

ARG DOWNLOAD_MODEL=1
# 模型版本（kws-models 发布资产名，例如 sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01）
# 手动指定: docker build --build-arg MODEL_VERSION=xxx .
ARG MODEL_VERSION=sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01
ARG APT_MIRROR=deb.debian.org
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG GH_PROXY=

RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|http://deb.debian.org|http://${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i "s|http://deb.debian.org|http://${APT_MIRROR}|g" /etc/apt/sources.list; \
    fi; \
    ok=0; \
    for i in 1 2 3 4 5; do \
        if apt-get update; then ok=1; break; fi; \
        echo "apt-get update 失败，10 秒后重试 ($i/5)"; sleep 10; \
    done; \
    if [ "$ok" != "1" ]; then \
        echo "== 容器内 resolv.conf 内容（排查用）: =="; cat /etc/resolv.conf; \
        echo "apt-get update 连续 5 次失败，退出"; exit 1; \
    fi; \
    apt-get install -y --no-install-recommends \
        pulseaudio-utils \
        alsa-utils \
        curl \
        bzip2 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/kws

COPY requirements.txt ./
RUN pip install --no-cache-dir --retries 5 -i ${PIP_INDEX_URL} -r requirements.txt

COPY app ./app
COPY scripts ./scripts

RUN if [ "${DOWNLOAD_MODEL}" = "1" ]; then \
        export GH_PROXY="${GH_PROXY}"; \
        bash scripts/download-model.sh /opt/kws-model "${MODEL_VERSION}"; \
    else \
        echo "DOWNLOAD_MODEL=0，跳过模型下载，请通过挂载提供 KWS_MODEL_DIR"; \
    fi

VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os; os.kill(1, 0)"

ENTRYPOINT ["python", "-m", "app.main"]
