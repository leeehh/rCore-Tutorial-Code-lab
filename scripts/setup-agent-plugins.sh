#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

setup_error() {
    local setup_red="" setup_reset=""
    if [[ -t 2 && "${TERM:-}" != "dumb" && -z "${NO_COLOR:-}" ]]; then
        setup_red=$'\033[31m'
        setup_reset=$'\033[0m'
    fi
    printf '%s[失败]%s %s\n' "${setup_red}" "${setup_reset}" "$1" >&2
}

if ! command -v python3 >/dev/null 2>&1; then
    setup_error '未找到 Python 3，需要 Python 3.9 或更高版本。'
    exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 9))'; then
    setup_error 'Python 版本过低，需要 Python 3.9 或更高版本。'
    exit 1
fi
exec python3 "${SCRIPT_DIR}/../plugins/rcore-session-archive/scripts/setup_agents.py" "$@"
