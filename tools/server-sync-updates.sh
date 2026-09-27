#!/bin/bash
# chatcoder 自建更新源同步（服务器端执行，plan-90-390）
# 用法: bash server-sync-updates.sh [期望版本号]
# 流程: 拉取 GitHub 最新 release -> 下载三件套到 staging -> sha512 校验 ->
#       注入 releaseNotes -> 原子部署 -> 生成 releases.json -> 清理旧版本（只保留最新）
set -euo pipefail

REPO="yueyuqingtian/chatcoder"
TARGET="/var/www/chatcoder-updates"
STAGE="/tmp/chatcoder-updates-staging"
EXPECTED_VERSION="${1:-}"

rm -rf "$STAGE"
mkdir -p "$STAGE" "$TARGET"

echo "[server-sync] 拉取 GitHub release 元数据..."
curl -sfL --max-time 60 "https://api.github.com/repos/$REPO/releases/latest" -o "$STAGE/release.json"

# 解析版本号 / 资产 URL / release notes
python3 - "$STAGE" <<'PY'
import json, os, sys
stage = sys.argv[1]
release = json.load(open(os.path.join(stage, "release.json"), encoding="utf-8"))
tag = release.get("tag_name") or ""
version = tag[1:] if tag.startswith("v") else tag
assets = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}
exe = next((n for n in assets if n.endswith(".exe")), None)
blockmap = f"{exe}.blockmap" if exe else None
for n in (exe, blockmap, "latest.yml"):
    if not n or n not in assets:
        print(f"ERROR: 资产缺失 {n}", file=sys.stderr)
        sys.exit(1)
with open(os.path.join(stage, "meta.tsv"), "w", encoding="utf-8") as f:
    f.write(f"{version}\n")
    f.write(f"{exe}\t{assets[exe]}\n")
    f.write(f"{blockmap}\t{assets[blockmap]}\n")
    f.write(f"latest.yml\t{assets['latest.yml']}\n")
with open(os.path.join(stage, "notes.md"), "w", encoding="utf-8") as f:
    f.write(release.get("body") or "")
print(f"[server-sync] release: v{version}")
PY

VERSION="$(head -1 "$STAGE/meta.tsv")"
EXE_NAME="$(sed -n '2p' "$STAGE/meta.tsv" | cut -f1)"
BM_NAME="$(sed -n '3p' "$STAGE/meta.tsv" | cut -f1)"
if [ -z "$VERSION" ]; then echo "[server-sync] 版本解析失败"; exit 1; fi
if [ -n "$EXPECTED_VERSION" ] && [ "$VERSION" != "$EXPECTED_VERSION" ]; then
  echo "[server-sync] 版本不一致: GitHub latest=v$VERSION 期望=v$EXPECTED_VERSION"; exit 1
fi

echo "[server-sync] 下载三件套 (v$VERSION)..."
mkdir -p "$STAGE/dl"
tail -n +2 "$STAGE/meta.tsv" | while IFS=$'\t' read -r name url; do
  echo "  下载 $name"
  curl -sfL --max-time 900 -o "$STAGE/dl/$name" "$url"
done

# sha512 校验（latest.yml 中为 base64 编码）
python3 - "$STAGE" "$EXE_NAME" <<'PY'
import base64, hashlib, os, re, sys
stage, exe = sys.argv[1], sys.argv[2]
yml = open(os.path.join(stage, "dl", "latest.yml"), encoding="utf-8").read()
m = re.search(r"^sha512:\s*(\S+)\s*$", yml, re.M)
if not m:
    print("ERROR: latest.yml 缺少 sha512", file=sys.stderr); sys.exit(1)
expected = m.group(1)
h = hashlib.sha512()
with open(os.path.join(stage, "dl", exe), "rb") as f:
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
actual = base64.b64encode(h.digest()).decode()
if actual != expected:
    print(f"ERROR: sha512 校验失败 actual={actual[:16]}... expected={expected[:16]}...", file=sys.stderr)
    sys.exit(1)
print("[server-sync] sha512 校验通过")
PY

# 注入 releaseNotes 到 latest.yml（追加块标量；electron-updater 原生支持该字段，
# 保证主源升级时 available/downloaded 状态能展示"更新了什么"）
python3 - "$STAGE" <<'PY'
import os, sys
stage = sys.argv[1]
yml_path = os.path.join(stage, "dl", "latest.yml")
notes = open(os.path.join(stage, "notes.md"), encoding="utf-8").read().replace("\r\n", "\n").replace("\r", "\n").strip("\n")
with open(yml_path, "a", encoding="utf-8") as f:
    if not notes:
        f.write("releaseNotes: ''\n")
    else:
        f.write("releaseNotes: |\n")
        for line in notes.split("\n"):
            f.write(("  " + line).rstrip() + "\n")
print("[server-sync] releaseNotes 已注入")
PY

echo "[server-sync] 原子部署（latest.yml 最后替换，保证元数据与文件一致）..."
mv -f "$STAGE/dl/$EXE_NAME" "$TARGET/"
mv -f "$STAGE/dl/$BM_NAME" "$TARGET/"
mv -f "$STAGE/dl/latest.yml" "$TARGET/latest.yml"

echo "[server-sync] 生成 releases.json..."
curl -sfL --max-time 60 "https://api.github.com/repos/$REPO/releases?per_page=50" -o "$STAGE/releases.json"
python3 - "$STAGE" "$TARGET" <<'PY'
import json, os, sys
stage, target = sys.argv[1], sys.argv[2]
raw = json.load(open(os.path.join(stage, "releases.json"), encoding="utf-8"))
items = []
for r in raw:
    tag = (r.get("tag_name") or "").strip()
    if not tag:
        continue
    items.append({
        "version": tag[1:] if tag.startswith("v") else tag,
        "name": r.get("name") or tag,
        "date": r.get("published_at") or "",
        "notes": r.get("body") or "",
        "prerelease": bool(r.get("prerelease")),
    })
with open(os.path.join(target, "releases.json"), "w", encoding="utf-8") as f:
    json.dump(items, f, ensure_ascii=False, indent=1)
print(f"[server-sync] releases.json: {len(items)} 个版本")
PY

echo "[server-sync] 清理旧版本文件（只保留最新）..."
cd "$TARGET"
shopt -s nullglob
for f in chatcoder-Setup-*.exe chatcoder-Setup-*.exe.blockmap; do
  case "$f" in
    *"$VERSION"*) : ;;
    *) echo "  删除 $f"; rm -f "$f" ;;
  esac
done

echo "[server-sync] 当前目录:"
ls -lh "$TARGET"
rm -rf "$STAGE"
echo "[server-sync] 完成 version=v$VERSION"
