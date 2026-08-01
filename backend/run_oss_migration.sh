#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir"

# 安全默认值是完整只读盘点；真实写入必须由操作者显式改用
# `--pilot N` 或 `--full`，并在执行前检查结构化报告。
if [[ "$#" -eq 0 ]]; then
  set -- --dry-run
fi

exec python -m scripts.migrate_kodo_to_oss "$@"
