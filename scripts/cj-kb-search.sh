#!/bin/zsh
# Search the archived CJ handbook knowledge base (read-only).
#
#   scripts/cj-kb-search.sh 资金费率
#   scripts/cj-kb-search.sh --tag 衍生品套利 --min-likes 100
#   scripts/cj-kb-search.sh --book 平仓深度
#
# See docs/knowledge/cj-defi-handbook/README.md for the full index.
set -eu

script_dir="${0:A:h}"
exec python3 "${script_dir}/cj_kb_search.py" "$@"
