#!/usr/bin/env bash
# 下载 sherpa-onnx 中文关键词检测模型（构建镜像时执行，也可在宿主机手动运行）。
# 用法: scripts/download-model.sh [目标目录] [模型版本]
# 国内直连 GitHub 不稳时，可设置 GH_PROXY 加速前缀，例如:
#   GH_PROXY=https://ghfast.top/ bash scripts/download-model.sh
set -euo pipefail

DEST="${1:-/opt/kws-model}"
VERSION="${2:-sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01}"
BASE_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models"
ARCHIVE="${VERSION}.tar.bz2"
GH_PROXY="${GH_PROXY:-}"

echo "==> 下载模型 ${ARCHIVE}"
echo "    ${BASE_URL}/${ARCHIVE}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

download_ok=0
if curl -fL --retry 3 -o "${TMP}/${ARCHIVE}" "${BASE_URL}/${ARCHIVE}"; then
    download_ok=1
elif [ -n "${GH_PROXY}" ]; then
    echo "==> GitHub 直连失败，尝试加速代理: ${GH_PROXY}"
    curl -fL --retry 3 -o "${TMP}/${ARCHIVE}" "${GH_PROXY}${BASE_URL}/${ARCHIVE}" && download_ok=1
fi

if [ "${download_ok}" = "1" ]; then
    echo "==> 解压到 ${DEST}"
    mkdir -p "${DEST}"
    tar -xjf "${TMP}/${ARCHIVE}" -C "${TMP}"
    SUB="$(find "${TMP}" -mindepth 1 -maxdepth 1 -type d | head -n1)"
    cp -r "${SUB}/." "${DEST}/"
else
    echo "==> GitHub 下载失败，改用 HuggingFace 镜像仓库逐文件下载"
    HF_BASE="https://huggingface.co/csukuangfj/${VERSION}/resolve/main"
    mkdir -p "${DEST}"
    for f in tokens.txt \
             keywords.txt \
             encoder-epoch-99-avg-1-chunk-16-left-64.onnx \
             decoder-epoch-99-avg-1-chunk-16-left-64.onnx \
             joiner-epoch-99-avg-1-chunk-16-left-64.onnx; do
        curl -fL --retry 3 -o "${DEST}/${f}" "${HF_BASE}/${f}"
    done
fi

echo "==> 完成，模型文件："
ls -l "${DEST}" | head -n 30
echo "==> 校验关键文件（通配符匹配，兼容 -chunk-16-left-64 等命名变体）："
ok=1
for pat in "tokens.txt" "encoder-*.onnx" "decoder-*.onnx" "joiner-*.onnx"; do
    if ls "${DEST}"/${pat} >/dev/null 2>&1; then
        echo "  OK  ${pat} -> $(ls "${DEST}"/${pat} | head -n1 | xargs basename)"
    else
        echo "  缺少 ${pat}"
        ok=0
    fi
done
[ "$ok" = "1" ] || { echo "==> 模型不完整，构建失败"; exit 1; }
