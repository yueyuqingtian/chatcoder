#!/usr/bin/env python3
"""chatcoder 发布后同步自建更新源（plan-90-390）。

在 gh release create 之后调用：把服务器端同步脚本上传到更新服务器执行
（由服务器从 GitHub 拉取三件套，规避本地跨境上行瓶颈），完成后回连验证。

跨境 SSH 偶发抖动：连接/上传/轮询均带重试；服务器端同步以 nohup 后台执行，
本地断线重连后继续看结果，不会中断服务器上的同步过程。

用法:
    python tools/sync-updates.py [--version X.Y.Z] [--dry-run]

依赖: paramiko；SSH 私钥默认 ~/.ssh/chatcoder_deploy（可用 --key 指定）。
"""
import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

import paramiko

HOST = "47.77.233.39"
USER = "root"
DEFAULT_KEY = Path.home() / ".ssh" / "chatcoder_deploy"
BASE_URL = "https://service.guyueyu.asia/updates/"
REMOTE_SCRIPT = "/tmp/chatcoder-sync-updates.sh"
REMOTE_LOG = "/tmp/chatcoder-sync.log"
REMOTE_EXIT = "/tmp/chatcoder-sync.exit"
WAIT_TIMEOUT_S = 900


def fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "chatcoder-sync"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def ssh_connect(key_path: str, attempts: int = 3) -> paramiko.SSHClient:
    last_err = None
    for i in range(1, attempts + 1):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                HOST,
                username=USER,
                pkey=paramiko.Ed25519Key.from_private_key_file(key_path),
                timeout=20,
                banner_timeout=30,
            )
            client.get_transport().set_keepalive(30)
            return client
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[sync] SSH 连接失败（第 {i}/{attempts} 次）: {e}", file=sys.stderr)
            if i < attempts:
                time.sleep(3)
    raise last_err  # type: ignore[misc]


def run(client: paramiko.SSHClient, cmd: str, timeout: int = 60) -> str:
    _stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    return (out + ("\n[stderr] " + err if err.strip() else "")).strip()


def upload_script(client: paramiko.SSHClient, script_bytes: bytes, key_path: str):
    """上传服务器端脚本（统一 LF），失败自动重连重试。"""
    last_err = None
    for i in range(1, 4):
        try:
            sftp = client.open_sftp()
            with sftp.open(REMOTE_SCRIPT, "wb") as f:
                f.write(script_bytes)
            sftp.close()
            return client
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[sync] 上传失败（第 {i}/3 次）: {e}", file=sys.stderr)
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            client = ssh_connect(key_path)
    raise last_err  # type: ignore[misc]


def wait_remote_sync(client: paramiko.SSHClient, key_path: str) -> tuple[int, str]:
    """等待后台同步完成（轮询 exit 文件），断线自动重连后续看。"""
    deadline = time.time() + WAIT_TIMEOUT_S
    status = "RUNNING"
    while True:
        time.sleep(5)
        if time.time() > deadline:
            print("[sync] 等待服务器同步超时", file=sys.stderr)
            break
        try:
            status = run(client, f"cat {REMOTE_EXIT} 2>/dev/null || echo RUNNING")
        except Exception as e:  # noqa: BLE001
            print(f"[sync] 轮询连接中断，重连: {e}", file=sys.stderr)
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            client = ssh_connect(key_path)
            continue
        if status != "RUNNING":
            break
    log = ""
    try:
        log = run(client, f"tail -c 4000 {REMOTE_LOG} 2>/dev/null")
    except Exception as e:  # noqa: BLE001
        print(f"[sync] 读取远端日志失败: {e}", file=sys.stderr)
    rc = int(status) if status.isdigit() else 1
    return rc, log


def main() -> int:
    parser = argparse.ArgumentParser(description="同步自建更新源（服务器从 GitHub 拉取）")
    parser.add_argument("--version", default="", help="期望版本号（默认读 package.json）")
    parser.add_argument("--key", default=str(DEFAULT_KEY), help="SSH 私钥路径")
    parser.add_argument("--dry-run", action="store_true", help="只上传脚本，不执行同步")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    version = args.version
    if not version:
        version = json.loads((root / "package.json").read_text(encoding="utf-8"))["version"]
    print(f"[sync] 目标版本: v{version}")

    server_script = Path(__file__).resolve().parent / "server-sync-updates.sh"
    if not server_script.exists():
        print(f"[sync] 缺少服务器端脚本: {server_script}", file=sys.stderr)
        return 1

    client = ssh_connect(args.key)
    try:
        # 统一换行，避免 CRLF 在 Linux 上执行报错
        script_bytes = server_script.read_bytes().replace(b"\r\n", b"\n")
        client = upload_script(client, script_bytes, args.key)
        print(f"[sync] 已上传服务器脚本 -> {REMOTE_SCRIPT}")

        if args.dry_run:
            print("[sync] dry-run: 跳过执行")
            return 0

        # nohup 后台执行：连接抖动不中断服务器上的同步
        print("[sync] 服务器同步中（拉取/校验/注入/部署/清理）...")
        inner = f"bash {REMOTE_SCRIPT} {version} > {REMOTE_LOG} 2>&1; echo $? > {REMOTE_EXIT}"
        run(client, f"rm -f {REMOTE_LOG} {REMOTE_EXIT}; nohup bash -c '{inner}' >/dev/null 2>&1 & echo STARTED")

        rc, log = wait_remote_sync(client, args.key)
        if log:
            print(log)
        if rc != 0:
            print(f"[sync] 服务器同步失败 (exit={rc})", file=sys.stderr)
            return 1
    finally:
        client.close()

    # 回连验证：latest.yml 版本号 + releases.json 可用（带重试，跨境线路偶发抖动）
    print("[sync] 回连验证...")
    last_err = None
    for i in range(1, 4):
        try:
            yml = fetch(BASE_URL + "latest.yml").decode("utf-8", "replace")
            if f"version: {version}" not in yml:
                print("[sync] 验证失败: latest.yml 中未找到目标版本", file=sys.stderr)
                return 1
            releases = json.loads(fetch(BASE_URL + "releases.json").decode("utf-8"))
            if not isinstance(releases, list) or not releases:
                print("[sync] 验证失败: releases.json 为空", file=sys.stderr)
                return 1
            print(f"[sync] 验证通过: latest.yml=v{version}, releases.json {len(releases)} 个版本")
            last_err = None
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[sync] 验证请求失败（第 {i}/3 次）: {e}", file=sys.stderr)
            if i < 3:
                time.sleep(3)
    if last_err is not None:
        print(f"[sync] 验证失败: {last_err}", file=sys.stderr)
        return 1
    print("[sync] 自建更新源就绪 " + BASE_URL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
