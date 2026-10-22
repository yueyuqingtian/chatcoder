#!/bin/bash
# chatcoder 自建更新源同步（服务器端执行）：直接从 GitHub Releases 拉取最新正式版并部署。
#
# 用法:
#   sync-updates.sh [期望版本号]   立即同步（给了版本号则校验 GitHub 最新版与之一致）
#   sync-updates.sh --if-changed   定时任务用：只取 GitHub 上 1KB 的 latest.yml 比对版本，
#                                  与本地一致就静默退出；有新版本才下载部署
#
# 安装为定时任务（每 5 分钟）见 server-install-updater.sh；发版后无需本机做任何操作，
# GitHub Actions 公开 Release 后最多 5 分钟，自建源就会自己拉到新版本。
#
# 流程: latest.yml（GitHub 最新正式版）→ 下载安装包与 blockmap → sha512 校验 →
#       （旧版本缺更新说明时）补写 releaseNotes → 原子部署（latest.yml 最后替换）→
#       刷新 releases.json → 清理旧版本（安装包只留最新；blockmap 留最近 10 个，差分更新要用）
#
# 可选环境变量:
#   EXTRA_MIRRORS  空格分隔的额外下载源根地址（https，目录下放同名安装包），写进 latest.yml 的 mirrors，
#                  客户端下载时与自建源 / GitHub 一起择优，不用发新版客户端就能新增国内镜像
#   TARGET / STAGE / REPO / GH_WEB / GH_API / PY / LOCK  部署目录、暂存目录、仓库与地址（测试时可覆盖）
set -euo pipefail

REPO="${REPO:-yueyuqingtian/chatcoder}"
TARGET="${TARGET:-/var/www/chatcoder-updates}"
STAGE="${STAGE:-/tmp/chatcoder-updates-staging}"
GH_WEB="${GH_WEB:-https://github.com}"
GH_API="${GH_API:-https://api.github.com}"
PY="${PY:-python3}"
LOCK="${LOCK:-/tmp/chatcoder-updates.lock}"
KEEP_BLOCKMAPS="${KEEP_BLOCKMAPS:-10}"

IF_CHANGED=0
EXPECTED_VERSION=""
for arg in "$@"; do
  case "$arg" in
    --if-changed) IF_CHANGED=1 ;;
    *) EXPECTED_VERSION="${arg#v}" ;;
  esac
done

log() { echo "[$(date '+%F %T')] [server-sync] $*"; }

# 定时任务与手动触发可能撞在一起：同一时刻只跑一个同步
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK"
  if ! flock -n 9; then
    log "已有同步在进行，跳过本次"
    exit 0
  fi
fi

yml_field() { sed -n "s/^$1:[[:space:]]*//p" "$2" | head -1 | tr -d "\"' \r"; }

mkdir -p "$STAGE" "$TARGET"

# 1) GitHub 最新正式版的 latest.yml。releases/latest 只指向已公开的正式版：草稿与预发布都不会被拉到，
#    CI 先建草稿、传完三件套才公开，所以这里拿到的版本资产一定是齐的。
if ! curl -sfL --retry 3 --retry-delay 5 --max-time 60 \
    "$GH_WEB/$REPO/releases/latest/download/latest.yml" -o "$STAGE/latest.yml.new"; then
  log "无法从 GitHub 获取 latest.yml（网络或 GitHub 暂不可用），下次再试"
  exit 1
fi
VERSION="$(yml_field version "$STAGE/latest.yml.new")"
EXE_NAME="$(yml_field path "$STAGE/latest.yml.new")"
if [ -z "$VERSION" ] || [ -z "$EXE_NAME" ]; then
  log "latest.yml 缺少 version / path 字段"
  exit 1
fi
CURRENT=""
[ -f "$TARGET/latest.yml" ] && CURRENT="$(yml_field version "$TARGET/latest.yml")"

refresh_releases_json() {
  # 更新历史（关于页）：GitHub API 不可用 / 限流时保留旧文件，不影响安装包部署
  if ! curl -sfL --retry 2 --max-time 60 -H "Accept: application/vnd.github+json" \
      "$GH_API/repos/$REPO/releases?per_page=50" -o "$STAGE/releases.json"; then
    log "GitHub API 暂不可用，releases.json 保留旧版本（下次同步再刷新）"
    return 0
  fi
  "$PY" - "$STAGE/releases.json" "$TARGET/releases.json" <<'PY'
import json, os, sys
src, dst = sys.argv[1], sys.argv[2]
raw = json.load(open(src, encoding="utf-8"))
items = []
for r in raw if isinstance(raw, list) else []:
    tag = (r.get("tag_name") or "").strip()
    if not tag or r.get("draft"):
        continue
    items.append({
        "version": tag[1:] if tag.startswith("v") else tag,
        "name": r.get("name") or tag,
        "date": r.get("published_at") or "",
        "notes": r.get("body") or "",
        "prerelease": bool(r.get("prerelease")),
    })
if not items:
    print("[server-sync] releases.json：API 返回为空，保留旧文件")
    sys.exit(0)
tmp = dst + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(items, f, ensure_ascii=False, indent=1)
os.replace(tmp, dst)
print(f"[server-sync] releases.json：{len(items)} 个版本")
PY
}

if [ "$IF_CHANGED" = 1 ] && [ "$VERSION" = "$CURRENT" ]; then
  # 已是最新：静默退出（不刷日志）；只有更新历史缺了当前版本时才补拉一次
  if [ ! -f "$TARGET/releases.json" ] || ! grep -q "\"version\": \"$VERSION\"" "$TARGET/releases.json"; then
    refresh_releases_json
  fi
  rm -f "$STAGE/latest.yml.new"
  exit 0
fi
if [ -n "$EXPECTED_VERSION" ] && [ "$VERSION" != "$EXPECTED_VERSION" ]; then
  log "版本不一致：GitHub 最新正式版 v$VERSION，期望 v$EXPECTED_VERSION（Release 可能尚未公开）"
  exit 1
fi
log "同步 v$VERSION（自建源当前 ${CURRENT:+v}${CURRENT:-无}）"

# 2) 下载安装包与 blockmap（文件名取 latest.yml 的 path，与 Release 资产同名）
BM_NAME="$EXE_NAME.blockmap"
DL="$STAGE/dl"
rm -rf "$DL"
mkdir -p "$DL"
mv -f "$STAGE/latest.yml.new" "$DL/latest.yml"
for name in "$EXE_NAME" "$BM_NAME"; do
  log "下载 $name"
  curl -sfL --retry 3 --retry-delay 5 --max-time 1800 \
    -o "$DL/$name" "$GH_WEB/$REPO/releases/download/v$VERSION/$name"
done

# 3) sha512 校验（latest.yml 中为 base64 编码）
"$PY" - "$DL" "$EXE_NAME" <<'PY'
import base64, hashlib, os, re, sys
dl, exe = sys.argv[1], sys.argv[2]
yml = open(os.path.join(dl, "latest.yml"), encoding="utf-8").read()
m = re.search(r"^sha512:\s*(\S+)\s*$", yml, re.M)
if not m:
    print("ERROR: latest.yml 缺少 sha512", file=sys.stderr); sys.exit(1)
h = hashlib.sha512()
with open(os.path.join(dl, exe), "rb") as f:
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
actual = base64.b64encode(h.digest()).decode()
if actual != m.group(1):
    print(f"ERROR: sha512 校验失败 actual={actual[:16]}... expected={m.group(1)[:16]}...", file=sys.stderr)
    sys.exit(1)
print("[server-sync] sha512 校验通过")
PY

# 4) latest.yml 补充字段：CI 打包的 latest.yml 已内置 releaseNotes，原样保留；
#    更早的版本（本地打包）缺更新说明时，从 Release 正文补写。重复写同名键会让客户端解析失败，所以先判断。
"$PY" - "$DL/latest.yml" "$GH_API/repos/$REPO/releases/tags/v$VERSION" <<'PY'
import json, os, re, sys, urllib.request
path, api = sys.argv[1], sys.argv[2]
yml = open(path, encoding="utf-8").read().replace("\r\n", "\n")
add = []
mirrors = [m for m in os.environ.get("EXTRA_MIRRORS", "").split() if m.startswith("https://")]
if mirrors and not re.search(r"^mirrors:", yml, re.M):
    add.append("mirrors:")
    add.extend("  - " + (m if m.endswith("/") else m + "/") for m in mirrors)
if not re.search(r"^releaseNotes:", yml, re.M):
    notes = ""
    try:
        req = urllib.request.Request(api, headers={"Accept": "application/vnd.github+json", "User-Agent": "chatcoder-sync"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            notes = (json.load(resp).get("body") or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    except Exception as e:  # noqa: BLE001 — 取不到说明不阻塞部署
        print(f"[server-sync] 取 Release 正文失败，releaseNotes 留空：{e}")
    if notes:
        add.append("releaseNotes: |")
        add.extend(("  " + line).rstrip() for line in notes.split("\n"))
    else:
        add.append("releaseNotes: ''")
if add:
    with open(path, "w", encoding="utf-8") as f:
        f.write(yml.rstrip("\n") + "\n" + "\n".join(add) + "\n")
    print("[server-sync] latest.yml 已补写：" + "、".join(k for k in ("mirrors", "releaseNotes") if any(a.startswith(k) for a in add)))
PY

# 5) 原子部署：先放安装包与 blockmap，latest.yml 最后替换——客户端读到新版本号时文件一定已就位
log "部署到 $TARGET"
mv -f "$DL/$EXE_NAME" "$TARGET/"
mv -f "$DL/$BM_NAME" "$TARGET/"
mv -f "$DL/latest.yml" "$TARGET/latest.yml"

# 6) 更新历史
refresh_releases_json

# 7) 清理旧版本：安装包只留最新；blockmap 按版本号从新到旧保留 KEEP_BLOCKMAPS 个
#    （客户端差分更新需要「旧版本」的 blockmap，约 350KB / 个）
cd "$TARGET"
shopt -s nullglob
for f in chatcoder-Setup-*.exe; do
  if [ "$f" != "$EXE_NAME" ]; then
    log "删除旧安装包 $f"
    rm -f "$f"
  fi
done
ls -1 chatcoder-Setup-*.exe.blockmap 2>/dev/null | sort -V -r | tail -n +"$((KEEP_BLOCKMAPS + 1))" | while read -r f; do
  log "删除旧 blockmap $f"
  rm -f "$f"
done

rm -rf "$DL"
log "完成 v$VERSION"
ls -lh "$TARGET"
