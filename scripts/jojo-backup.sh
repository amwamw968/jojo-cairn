#!/usr/bin/env bash
# Copy the private SQLite database and page images. This directory is gitignored.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -d var/private ]]; then
  echo "没有 var/private，先导入或启动一次。" >&2
  exit 1
fi
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
dest="var/private/backups/${stamp}"
mkdir -p "$dest"
# The backup folder lives inside var/private; do not copy backups into themselves.
find var/private -mindepth 1 -maxdepth 1 ! -name backups -exec cp -a {} "$dest/" \;
echo "备份目录：${dest}"
echo "恢复：停掉服务后，用该目录里的 jojo.db、originals 和 scans 覆盖 var/private 里的同名项。"
