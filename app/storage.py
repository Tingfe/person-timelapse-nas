"""Atomic snapshots and renewable locks shared by the NAS and SMB workers."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time
import uuid


def atomic_json(path, value, durable=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            if durable:
                os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def shared_lock(path, timeout=5, stale_seconds=900):
    """mkdir is exclusive across SMB clients; a heartbeat prevents live lease expiry."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    token = uuid.uuid4().hex
    owner_info = {"token": token, "host": socket.gethostname(), "pid": os.getpid()}
    while True:
        try:
            path.mkdir()
            break
        except FileExistsError:
            try:
                abandoned_owner = False
                try:
                    previous = json.loads((path / "owner").read_text(encoding="ascii"))
                    if previous.get("host") == owner_info["host"]:
                        try:
                            os.kill(previous["pid"], 0)
                        except ProcessLookupError:
                            abandoned_owner = True
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                if abandoned_owner or time.time() - path.stat().st_mtime > stale_seconds:
                    abandoned = path.with_name(f"{path.name}.stale-{token}")
                    path.rename(abandoned)
                    if abandoned.is_dir():
                        shutil.rmtree(abandoned)
                    else:
                        abandoned.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"另一个计算节点正在处理：{path.name}")
            time.sleep(0.1)
    owner = path / "owner"
    stop = threading.Event()
    thread = None
    try:
        owner.write_text(json.dumps(owner_info), encoding="ascii")

        def renew():
            while not stop.wait(min(30, stale_seconds / 3)):
                try:
                    if json.loads(owner.read_text(encoding="ascii")).get("token") != token:
                        return
                    os.utime(path, None)
                except (OSError, ValueError, AttributeError):
                    return

        thread = threading.Thread(target=renew, daemon=True)
        thread.start()
        yield
    finally:
        stop.set()
        if thread:
            thread.join(timeout=1)
        try:
            if json.loads(owner.read_text(encoding="ascii")).get("token") == token:
                owner.unlink()
                path.rmdir()
        except (OSError, ValueError, AttributeError):
            pass
