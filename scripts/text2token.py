# -*- coding: utf-8 -*-
"""适配版 sherpa-onnx scripts/text2token.py。

把中文唤醒词转换为 KWS keywords.txt 需要的 token 行，例如::

    python scripts/text2token.py \
        --tokens /opt/kws-model/tokens.txt \
        --text "你好小智,小智小智"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.text2token import Text2Token, Text2TokenError  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="把中文/文本唤醒词转换为 sherpa-onnx KWS token 行"
    )
    parser.add_argument("--tokens", required=True, help="sherpa-onnx 模型的 tokens.txt 路径")
    parser.add_argument(
        "--text",
        action="append",
        default=[],
        help="要转换的唤醒词，多个用英文逗号分隔；可多次指定",
    )
    parser.add_argument(
        "--output",
        default="",
        help="可选输出文件；不指定时打印到 stdout",
    )
    args = parser.parse_args()

    keywords: list[str] = []
    for text in args.text:
        for kw in text.replace("\n", ",").replace("，", ",").split(","):
            kw = kw.strip()
            if kw:
                keywords.append(kw)

    if not keywords:
        parser.error("至少需要一个 --text 唤醒词")

    try:
        converter = Text2Token.from_file(args.tokens)
        lines = converter.format_lines(keywords)
    except (OSError, Text2TokenError) as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"已写入 {args.output}")
    else:
        print("\n".join(lines))


if __name__ == "__main__":
    main()
