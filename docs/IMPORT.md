# 本地材料导入

导入器读取一份你自己保管的 JSON 清单，按 SHA256 找到同目录里的 PDF 或图片，再把每一页放进本机 Cairn 库。清单和原件都不要提交到公开仓库。

## 清单

只允许这些字段：`purpose`、`files`、`boundaries`、`synthetic`、`notice`。每个文件只允许 `filename`、`label`、`role`、`bytes`、`sha256`、`pages`、`group`、`synthetic`。`filename` 只能是文件名，不能是路径。出现 `answer`、`content` 或其他字段会整份拒绝，不会写入题目。

`role` 可以是 `questions`、`source`、`source_photo`、`source_with_answers`、`answer_companion`。同一 `group`（或清单用语里能对应上的题目版/答案版、第 N 题照片和讲义）只生成一套题，答案页仍单独记账。

合成夹具必须自己把 `synthetic` 设为 true。页面会标明它不是学习者的真实材料。

## 命令

```bash
export CAIRN_FAMILY_PROFILE=jojo
export CAIRN_DB_URL=sqlite:///$(pwd)/var/private/jojo.db
.venv/bin/python -m cairn.material_ingest \
  --manifest /path/outside/the/repo/manifest.json \
  --root /path/outside/the/repo
```

PDF 用本机 PDFium 渲染，文字用本机 RapidOCR。算式只有在重新算出的结果和页上印出的答案一致时才写入标准答案，并进入「已核对练习」的发布版本。其余页面保留原图，标成待复核，不进入今天的练习。看不清、对不上或没有答案的内容不会被补成答案。

页面种类只记为题目、答案、封面、空白、读不清。来源名称只在页面文字里真正读到时写入；不会把一个名字改成另一个机构。

私有库在 `var/private/`：`jojo.db`、`originals/`、`scans/`、`ingest-report.json`。备份用 `scripts/jojo-backup.sh`。这些路径已在 `.gitignore`。
