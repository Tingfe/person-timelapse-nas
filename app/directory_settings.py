"""Directory choices stay inside explicitly exposed container mount points."""

import os
import tempfile
from pathlib import Path


def checked_directory(value, roots, *, writable=False, create=False):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("请选择容器内的目录")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or any(part.startswith(".") for part in path.parts[1:]):
        raise ValueError("请使用已挂载目录内的绝对路径，不支持隐藏目录或上级跳转")
    roots = [Path(root).absolute() for root in roots]
    root = next((root for root in roots if path == root or root in path.parents), None)
    if root is None or not root.is_dir():
        raise ValueError("该目录未开放，请先通过 Compose 挂载录像或成片目录")
    # Do not let a symlink inside a permitted mount expose another host path.
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise ValueError("不能选择符号链接目录，请直接挂载实际目录")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("目录超出已挂载范围")
    if create and not path.exists():
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir() or not os.access(path, os.R_OK | os.X_OK):
        raise ValueError("目录不存在或没有读取权限，请检查挂载路径与 NAS 权限")
    if writable:
        try:
            with tempfile.TemporaryFile(dir=path):
                pass
        except OSError as error:
            raise ValueError("成片目录不可写，请检查是否只读挂载及 NAS 目录权限") from error
    return path


def list_directories(value, roots):
    path = checked_directory(value, roots)
    children = []
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                continue
            children.append({"name": entry.name, "path": str(path / entry.name)})
            if len(children) >= 201:
                break
    return {"path": str(path), "children": sorted(children, key=lambda item: item["name"])[:200],
            "truncated": len(children) > 200}
