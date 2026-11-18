#!/usr/bin/env bash
# Start the family reader and, with --studio, the separate authoring app.
# Binds to loopback only. The operator token is created in var/private and is not printed.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -x .venv/bin/uvicorn ]]; then
  echo "先安装：python3.12 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

HOST="${HOST:-127.0.0.1}"
if [[ "$HOST" != "127.0.0.1" && "$HOST" != "localhost" ]]; then
  echo "拒绝绑定 ${HOST}。这个启动脚本只监听本机回环地址。" >&2
  exit 1
fi

PORT="${PORT:-18421}"
STUDIO_PORT="${STUDIO_PORT:-18423}"
mkdir -p var/private
TOKEN_FILE="var/private/operator-token"
if [[ ! -s "$TOKEN_FILE" ]]; then
  umask 077
  .venv/bin/python -c 'import secrets; open("var/private/operator-token","w").write(secrets.token_hex(32))'
  chmod 600 "$TOKEN_FILE"
fi

export CAIRN_FAMILY_PROFILE=jojo
export CAIRN_DB_URL="${CAIRN_DB_URL:-sqlite:///$(pwd)/var/private/jojo.db}"
export CAIRN_WORKFLOW_ADMIN_TOKEN
CAIRN_WORKFLOW_ADMIN_TOKEN="$(cat "$TOKEN_FILE")"
export CAIRN_WORKFLOW_CANVAS_ORIGINS="${CAIRN_WORKFLOW_CANVAS_ORIGINS:-http://127.0.0.1:${STUDIO_PORT}}"
unset CAIRN_FAMILY_ALLOW_MODEL || true

echo "读者端：http://${HOST}:${PORT}/"
echo "学习者页：http://${HOST}:${PORT}/learn"
echo "材料页：http://${HOST}:${PORT}/materials"
echo "operator 凭据文件：${TOKEN_FILE}（内容不在这里打印）"
echo "模型讲解、复述和云端视觉识别保持关闭。"

if [[ "${1:-}" == "--studio" ]]; then
  echo "作者端：http://${HOST}:${STUDIO_PORT}/  （与读者端分开，需自行填入凭据文件里的 token）"
  (cd workflow-canvas && CAIRN_API_TARGET="http://${HOST}:${PORT}" npm run dev -- --host "$HOST" --port "$STUDIO_PORT") &
fi

exec env \
  -u CAIRN_TUTOR_LLM_URL \
  -u CAIRN_TUTOR_LLM_API_KEY \
  -u CAIRN_TUTOR_LLM_KEY_FILE \
  -u CAIRN_VLM_PROVIDER \
  -u CAIRN_VLM_CLOUD_URL \
  -u CAIRN_VLM_CLOUD_KEY_FILE \
  -u CAIRN_ASR_URL \
  -u CAIRN_ASR_KEY_FILE \
  .venv/bin/uvicorn cairn.web:app --host "$HOST" --port "$PORT"
