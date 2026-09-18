"""仓库根 conftest: 让"单测污染生产文件"变成硬错误.

2026-09-18 事故: 从仓库根跑 pytest 时, 生产落盘入口(相对 cwd)直接指向真实
`trade_records/`、`records/`; 用例改写 state 与订单账本, 重启后 guard/pending
记录蒸发 —— BTC 裸仓 6 分 41 秒、ETH 挂单被遗弃 3 小时。

`tests/conftest.py` 的 autouse fixture 能在**用例执行期间**把它们重定向到
tmp_path, 但管不住用例结束后仍在跑的泄漏线程: 探针实测 `_stop_watchdog_loop`
在 fixture 回滚之后调 `_drop_guard` → 真实 state 被写成 guards=[]。

所以这里再加一道进程级闸门: 测试进程内任何指向 `PROJECT_ROOT/trade_records`
或 `PROJECT_ROOT/records` 的写入都在 fs 层直接抛错, 不再依赖调用方是否
记得打补丁。真实业务代码路径不受影响(runtime_state 用绝对路径 os.replace)。
"""
from __future__ import annotations

import builtins
import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
_GUARDED = tuple(
    root for root in (_REPO_ROOT / "trade_records", _REPO_ROOT / "records") if root.is_dir()
) or ()


def _guarded(path: object) -> Path | None:
    """路径落在生产落盘目录时返回该目录, 否则 None."""
    try:
        candidate = Path(os.fspath(path))  # type: ignore[arg-type]
    except TypeError:
        return None
    if not candidate.is_absolute():
        candidate = _REPO_ROOT / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    for root in _GUARDED:
        if resolved == root or root in resolved.parents:
            return root
    return None


def _deny(path: object, action: str) -> None:
    raise RuntimeError(
        f"测试进程试图{action}生产落盘路径 {path!r}; "
        "这是 2026-09-18 事故的根因(单测覆盖 state/账本导致裸仓). "
        "请检查是否有未清理的后台线程或漏打的路径补丁."
    )


_original_open = builtins.open
_original_replace = os.replace
_original_remove = os.remove


def _guarded_open(file, mode="r", *args, **kwargs):
    if (
        isinstance(file, (str, bytes, os.PathLike))
        and set(mode) & set("wax+")
        and _guarded(file) is not None
    ):
        _deny(file, "写入")
    return _original_open(file, mode, *args, **kwargs)


def _guarded_replace(src, dst, *args, **kwargs):
    if _guarded(dst) is not None:
        _deny(dst, "替换")
    return _original_replace(src, dst, *args, **kwargs)


def _guarded_remove(path, *args, **kwargs):
    if _guarded(path) is not None:
        _deny(path, "删除")
    return _original_remove(path, *args, **kwargs)


builtins.open = _guarded_open
os.replace = _guarded_replace
os.remove = _guarded_remove


def pytest_report_header(config) -> str:
    return "pa-agent: 生产落盘目录已加写保护 " + ", ".join(str(p) for p in _GUARDED)
