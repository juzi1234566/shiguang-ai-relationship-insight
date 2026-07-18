#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
python3 scripts/create_demo.py --root .demo-data
export SHIGUANG_APP_HOME="$ROOT/.demo-data"
export SHIGUANG_EDITION=general
echo "拾光合成演示：http://127.0.0.1:18999"
python3 scripts/server.py --port 18999 --open
