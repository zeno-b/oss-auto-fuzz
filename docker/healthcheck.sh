#!/usr/bin/env bash
set -euo pipefail

python3 --version >/dev/null
git --version >/dev/null
if command -v docker >/dev/null 2>&1; then
  docker --version >/dev/null
fi

exit 0
