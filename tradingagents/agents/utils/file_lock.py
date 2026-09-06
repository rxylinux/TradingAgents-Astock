"""跨线程 + 跨进程的文件锁（advisory），无第三方依赖，Windows 兼容。

用途（A06）：记忆日志的读-改-写事务边界。同一日志路径的所有实例
（同进程多对象 / 多线程 / 多进程）共用同一把锁：

- 进程内：按**规范化后**的锁文件路径共享 ``threading.Lock``（避免同进程
  自锁等待；符号链接/相对路径别名经 ``Path.resolve()`` 归一到同一把锁）；
- 跨进程：锁文件上的操作系统 advisory 锁——POSIX 用 ``fcntl.flock``，
  Windows 用 ``msvcrt.locking``。

失败语义（Codex 退回修正）：**绝不静默降级**。锁文件无法创建/打开、或
底层锁调用返回非竞争性错误时直接抛出——事务要么在完整保护下执行，要么
明确失败；不允许"没有跨进程锁也继续写日志"的路径。竞争性忙（POSIX
``EWOULDBLOCK/EAGAIN``；Windows 的 ``msvcrt`` 在 fd 已可写打开时以
``EACCES`` 表示忙）是唯一被当作"重试"的错误。
"""

from __future__ import annotations

import contextlib
import errno
import os
import threading
import time
from pathlib import Path

# 进程内按锁文件路径共享的互斥锁（跨实例生效）
_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()

try:  # POSIX
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

# POSIX 非阻塞锁「忙」的 errno；Windows msvcrt.locking(LK_NBLCK) 忙时同样报
# EACCES——但此时 fd 已成功以可写方式打开，权限错误已被 open 排除。
_BUSY_ERRNOS = {errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES}


def _process_lock(path_str: str) -> threading.Lock:
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(path_str)
        if lock is None:
            lock = threading.Lock()
            _PROCESS_LOCKS[path_str] = lock
        return lock


def _try_os_lock(fd) -> bool:
    """尝试对 fd 加非阻塞排它锁。返回 False 仅表示「正被别人持有」。"""
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as e:
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return False
            raise  # 真实底层错误：透传，不得当作竞争
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return True
    except PermissionError:
        # fd 已可写打开（open 已成功），此处 EACCES 即「忙」
        return False
    except OSError:
        raise


def _os_unlock(fd) -> None:
    if fcntl is not None:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
    else:
        with contextlib.suppress(OSError):
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def file_lock(target: str | Path, timeout: float = 30.0, poll: float = 0.02):
    """对 ``target``（被保护的数据文件）加跨进程排它锁。

    持锁期间「读者看不到部分写入、写者的读-改-写不被并发交错」由调用方
    在锁内完成完整事务保证；本模块只提供互斥。超时抛 ``TimeoutError``。
    锁文件创建/打开失败（权限、只读文件系统等）**直接抛出原异常**——调用
    方的事务在不受保护的状态下绝不继续。
    """
    # 规范化：解析符号链接与相对路径别名，确保同一物理文件对应同一把锁。
    # 锁文件名必须取 **resolved 路径**的 name——用别名的 name 会让 real.md
    # 与 alias.md 得到两把不同的锁（Codex 退回修正）。
    resolved = Path(target).expanduser().resolve()
    lock_path = resolved.with_name(resolved.name + ".lock")
    lock_path_str = str(lock_path)
    process_lock = _process_lock(lock_path_str)

    deadline = time.monotonic() + timeout
    acquired_process = process_lock.acquire(
        timeout=max(0.0, deadline - time.monotonic())
    )
    if not acquired_process:
        raise TimeoutError(f"file_lock: 进程内锁等待超时 ({lock_path})")

    fd = None
    try:
        Path(lock_path).touch(exist_ok=True)  # 创建失败（权限/只读）→ 直接抛
        fd = os.open(lock_path_str, os.O_RDWR)  # 打开失败 → 直接抛
        while not _try_os_lock(fd):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"file_lock: 跨进程锁等待超时 ({lock_path})")
            time.sleep(poll)
        yield
    finally:
        if fd is not None:
            _os_unlock(fd)
            os.close(fd)
        process_lock.release()
