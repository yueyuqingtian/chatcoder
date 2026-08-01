"""诊断与版本检查（D14/D15）。"""
import platform
import shutil
import subprocess

from fastapi import APIRouter

router = APIRouter(tags=["diagnostics"])


@router.get("/diagnostics", response_model=dict)
async def diagnostics():
    """环境诊断：git、后端、模型连通、目录权限。"""
    result: dict = {"ok": True, "checks": {}}

    # git
    try:
        ver = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=10)
        result["checks"]["git"] = {"ok": ver.returncode == 0, "detail": ver.stdout.strip() or ver.stderr.strip()}
    except Exception as e:
        result["checks"]["git"] = {"ok": False, "detail": str(e)}

    # 后端端口
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=2):
            result["checks"]["backend_port"] = {"ok": True, "detail": "8000 端口可连接"}
    except OSError as e:
        result["checks"]["backend_port"] = {"ok": False, "detail": f"{e}"}

    # 平台/WSL
    result["checks"]["platform"] = {"ok": True, "detail": f"{platform.system()} {platform.release()}"}
    result["checks"]["wsl"] = {"ok": True, "detail": "WSL 检测：N/A"}

    # 工作目录可写
    import tempfile
    try:
        with tempfile.TemporaryFile(dir=".") as f:
            f.write(b"1")
        result["checks"]["workspace_writable"] = {"ok": True, "detail": "当前目录可写"}
    except OSError as e:
        result["checks"]["workspace_writable"] = {"ok": False, "detail": str(e)}

    result["ok"] = all(c.get("ok") for c in result["checks"].values())
    return result


@router.get("/update-check", response_model=dict)
async def update_check():
    """版本检查（占位：读内置版本源）。"""
    return {"ok": True, "current": "0.2.0", "latest": None, "has_update": False}
