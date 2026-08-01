"""v0.3: 安全路径工具(防穿越)。所有 fs 工具共享。

v2.5: 修复绝对路径在 workspace 内被误拒的问题。
agent 常传绝对路径(如 'F:\\project\\yipinCode\\clinic\\docs\\report.md'),
只要该路径在 workspace_root 内就应放行。

v1.0: 修复符号链接穿越漏洞——删除 v5.3 的“放行”旁路逻辑，
增加 symlink 检测，使用 os.path.realpath 解析符号链接后再校验。
"""
import os
from pathlib import Path


def safe_resolve(workspace_root: str, rel_path: str) -> Path | None:
    """把相对路径或绝对路径解析到 workspace 内,防穿越。

    返回 None 表示越界或非法。

    v2.5: 绝对路径如果在 workspace_root 内,也接受(不再一律拒绝)。
    v1.0: 删除 v5.3 旁路逻辑，增加符号链接检测。
    """
    if not rel_path:
        return None

    try:
        root = Path(os.path.realpath(workspace_root))
    except (OSError, ValueError):
        return None

    try:
        p = Path(rel_path)
    except (OSError, ValueError):
        return None

    # v2.5: 绝对路径 — 检查是否在 workspace_root 内
    if p.is_absolute():
        try:
            # v1.0: 用 realpath 解析符号链接
            target = Path(os.path.realpath(str(p)))
            target.relative_to(root)
            return target
        except (ValueError, OSError):
            return None

    # 相对路径 — 拼接到 root 后解析
    try:
        # v1.0: 用 realpath 解析符号链接，防止 symlink 穿越
        joined = root / rel_path
        target = Path(os.path.realpath(str(joined)))
    except (OSError, ValueError):
        return None

    # 检查解析后的路径是否仍在 root 内(防 ../../ 穿越 + symlink 穿越)
    try:
        target.relative_to(root)
    except ValueError:
        # v1.0: 删除 v5.3 的旁路逻辑——任何解析后在 root 外的路径一律拒绝
        return None

    # v1.0: 额外检查——如果原始路径含符号链接且指向 root 外，拒绝
    # （防止 root 内的 symlink 指向外部敏感文件）
    try:
        if joined.is_symlink():
            link_target = Path(os.path.realpath(str(joined)))
            link_target.relative_to(root)
    except (ValueError, OSError):
        return None

    return target


def safe_resolve_parent(workspace_root: str, rel_path: str) -> Path | None:
    """v2.5: 对 fs.write 这类需要创建新文件的场景,
    检查父目录是否在 workspace 内(文件本身可能尚不存在)。

    与 safe_resolve 的区别:不要求 target 存在,只检查路径合法性。
    """
    if not rel_path:
        return None

    try:
        root = Path(workspace_root).resolve()
    except (OSError, ValueError):
        return None

    try:
        p = Path(rel_path)
    except (OSError, ValueError):
        return None

    if p.is_absolute():
        target = p
    else:
        target = root / rel_path

    # 检查最终路径是否在 root 内
    # 用 os.path.realpath 避免 Path.resolve() 对不存在路径的符号链接展开异常
    try:
        target_real = Path(os.path.realpath(str(target)))
        root_real = Path(os.path.realpath(str(root)))
        target_real.relative_to(root_real)
    except (ValueError, OSError):
        return None

    return target_real
