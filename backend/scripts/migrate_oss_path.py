#!/usr/bin/env python3
"""已退役的旧 OSS URL 字段改写入口。

该文件仅保留为安全兼容桩，防止旧运维命令静默执行错误的数据改写。
"""

from __future__ import annotations

import sys
from typing import Optional, Sequence, TextIO

RETIREMENT_MESSAGE = (
    "migrate_oss_path.py 已退役，未执行任何写入。"
    "请改用 `python -m scripts.migrate_kodo_to_oss --dry-run`，"
    "确认报告后再显式选择 `--pilot N` 或 `--full`。"
)


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    stderr: TextIO = sys.stderr,
) -> int:
    # 接受并忽略旧参数，只为给旧自动化返回稳定、不可误判为成功的退出码。
    del argv
    stderr.write(f"{RETIREMENT_MESSAGE}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
