"""因子/特征代码静态体检器(移植自 alphaevo 的 alpha_factory/sandbox.py, 改造为时序版).

来源与改造说明
--------------
上游项目 alphaevo(https://github.com/ZhuLinsen/alphaevo, Apache-2.0) 的
``src/alphaevo/alpha_factory/sandbox.py`` 用 AST 白名单把 LLM 生成的因子代码
挡在沙箱外。它的未来数据检查是为**日线截面选股**写的, 只认 ``idx + N`` 与
``shift(-N)``。PA_Agent 是**单品种逐 bar 时序**场景, 前视泄漏的形态更多, 所以:

  * 保留其危险调用 / 反射逃逸黑名单(逐条对齐, 见下方常量);
  * 未来数据检查重写为时序语义: 下标越界、开放上界切片、负 shift、居中滚动窗口、
    后向填充、取整段序列尾值、在整段序列上做统计;
  * 新增结构检查: ``compute(df, idx)`` 签名、函数体长度、调试输出、无界循环。

本模块**纯静态分析**: 不导入、不执行被测代码, 无网络、无落盘、无副作用。

用法::

    python tools/feature_guard.py path/to/factor.py
    python tools/feature_guard.py pa_agent/ai --json
    python tools/feature_guard.py --demo

退出码: 0 = 无 error, 1 = 存在 error, 2 = 用法/路径错误.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Severity = Literal["error", "warn"]
#: 违规上报回调: (rule, severity, node, message)
AddFn = Callable[[str, Severity, "ast.AST", str], None]

# --------------------------------------------------------------------------
# 规则常量(与 alphaevo alpha_factory/sandbox.py 对齐, 并做时序扩展)
# --------------------------------------------------------------------------

#: 允许导入的模块(因子代码只该做纯计算)
ALLOWED_IMPORTS = frozenset({"numpy", "pandas", "math"})
#: 允许出现的模块别名
ALLOWED_IMPORT_ALIASES = frozenset({"np", "pd", "numpy", "pandas", "math"})

#: 绝不能出现在因子代码里的名字
FORBIDDEN_NAMES = frozenset(
    {
        "exec",
        "eval",
        "compile",
        "__import__",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
        "dir",
        "open",
        "input",
        "os",
        "sys",
        "subprocess",
        "shutil",
        "pathlib",
        "socket",
        "http",
        "urllib",
        "requests",
        "pickle",
        "shelve",
        "marshal",
        "importlib",
        "ctypes",
    }
)

#: 反射逃逸用的 dunder 属性
FORBIDDEN_DUNDERS = frozenset(
    {
        "__class__",
        "__subclasses__",
        "__bases__",
        "__mro__",
        "__globals__",
        "__builtins__",
        "__code__",
        "__func__",
        "__self__",
        "__module__",
        "__dict__",
        "__init_subclass__",
        "__reduce__",
        "__reduce_ex__",
        "__getattr__",
        "__setattr__",
        "__delattr__",
    }
)

#: 白名单模块上也不许调的 IO / 进程方法
FORBIDDEN_MODULE_ATTRS = frozenset(
    {
        "read_csv",
        "read_json",
        "read_pickle",
        "read_parquet",
        "read_table",
        "read_excel",
        "load",
        "loads",
        "save",
        "to_csv",
        "to_pickle",
        "to_parquet",
        "urlopen",
        "system",
        "popen",
        "run",
        "check_output",
        "connect",
    }
)

#: 看起来是"整段行情"的变量名; 对它取负索引 / 全序列统计视为前视
FULL_FRAME_NAMES = frozenset({"df", "data", "frame", "bars", "ohlcv", "kline", "prices", "dataset"})

#: 会吃下整段序列的统计方法
STAT_METHODS = frozenset(
    {
        "max",
        "min",
        "mean",
        "std",
        "median",
        "sum",
        "var",
        "idxmax",
        "idxmin",
        "argmax",
        "argmin",
        "quantile",
        "rank",
    }
)

#: 单函数体行数上限(超出只告警, 不拦)
MAX_FUNCTION_LINES = 60

#: 行级豁免标记; 行尾写它可跳过该行所有违规(与 ruff 的 noqa 习惯一致)
NOQA_MARK = "noqa: feature-guard"

#: 允许的规则 id 全集(便于外部断言与文档)
RULE_IDS = (
    "forbidden_import",
    "dangerous_call",
    "dunder_escape",
    "future_idx_offset",
    "future_shift",
    "future_centered_rolling",
    "future_backfill",
    "future_tail_index",
    "future_full_series_stat",
    "missing_compute",
    "bad_compute_signature",
    "function_too_long",
    "debug_print",
    "unbounded_loop",
    "syntax_error",
)


# --------------------------------------------------------------------------
# 结果模型
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    """一条违规记录。"""

    rule: str
    severity: Severity
    line: int
    col: int
    message: str
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "line": self.line,
            "col": self.col,
            "message": self.message,
            "snippet": self.snippet,
        }


@dataclass
class ScanResult:
    """一次体检的结果。"""

    name: str = "<string>"
    violations: list[Violation] = field(default_factory=list)
    checked_lines: int = 0
    parse_error: str | None = None

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "error"]

    @property
    def warns(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "warn"]

    @property
    def ok(self) -> bool:
        """没有 error 且能正常解析才算通过(warn 不拦)。"""
        return self.parse_error is None and not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "checked_lines": self.checked_lines,
            "parse_error": self.parse_error,
            "error_count": len(self.errors),
            "warn_count": len(self.warns),
            "violations": [v.to_dict() for v in self.violations],
        }

    def format_text(self) -> str:
        head = "PASS" if self.ok else "FAIL"
        rows = [f"{head} {self.name} ({len(self.errors)} error, {len(self.warns)} warn)"]
        if self.parse_error:
            rows.append(f"  error  L-:  syntax_error  {self.parse_error}")
        for v in self.violations:
            rows.append(f"  {v.severity:<5}  L{v.line}:{v.col}  {v.rule}  {v.message}")
            if v.snippet:
                rows.append(f"        {v.snippet}")
        return "\n".join(rows)


# --------------------------------------------------------------------------
# AST 小工具
# --------------------------------------------------------------------------


def _const_number(node: ast.AST) -> float | None:
    """把常量节点读成 float(支持前置负号), 否则 None。"""
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
        and not isinstance(node.operand.value, bool)
    ):
        return -float(node.operand.value)
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ):
        return float(node.value)
    return None


def _is_idx(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "idx"


def _idx_offset(node: ast.AST) -> float | None:
    """识别 ``idx + k`` / ``idx - k`` / ``k + idx``, 返回 k; 否则 None。"""
    if not isinstance(node, ast.BinOp) or not isinstance(node.op, (ast.Add, ast.Sub)):
        return None
    left, right = node.left, node.right
    if _is_idx(left):
        k = _const_number(right)
        if k is None:
            return None
        return k if isinstance(node.op, ast.Add) else -k
    if _is_idx(right) and isinstance(node.op, ast.Add):
        return _const_number(left)
    return None


def _root_name(node: ast.AST) -> str | None:
    """沿属性/下标/调用链回溯到最底层的变量名, 例如 ``df["close"].iloc`` 回溯出 "df"。"""
    cur: ast.AST = node
    for _ in range(64):
        if isinstance(cur, ast.Name):
            return cur.id
        if isinstance(cur, (ast.Attribute, ast.Subscript)):
            cur = cur.value
            continue
        if isinstance(cur, ast.Call):
            cur = cur.func
            continue
        return None
    return None


# --------------------------------------------------------------------------
# 单条规则判定
# --------------------------------------------------------------------------


def _subscript_future_reason(node: ast.Subscript) -> tuple[Severity, str] | None:
    """下标/切片里的前视泄漏。

    切片语义: 下界**包含**、上界**不包含**。所以 ``[idx - 19 : idx + 1]`` 是取到
    当前 bar 为止的正确写法; 而上界缺省(``[x:]`` / ``[:]``)意味着一路切到序列
    末尾, 必然把当前 bar 之后的行情算进来 —— 这是最容易漏的一种写法。

    根变量是整段行情(df / bars / ohlcv ...)时按 error 拦; 无法确认根变量时降级为
    warn, 避免对"已被截断的局部窗口"误报。
    """
    sl = node.slice

    # 裸索引: df.iloc[idx + 1]
    offset = _idx_offset(sl)
    if offset is not None and offset > 0:
        return "error", f"索引越过当前 bar: idx + {offset:g}"

    if not isinstance(sl, ast.Slice):
        return None

    lower, upper = sl.lower, sl.upper

    if lower is not None:
        low_offset = _idx_offset(lower)
        if low_offset is not None and low_offset > 0:
            return "error", f"切片下界越过当前 bar: idx + {low_offset:g}"

    if upper is not None:
        up_offset = _idx_offset(upper)
        # 上界不含: idx + 1 正好切到当前 bar, idx + 2 才越界
        if up_offset is not None and up_offset > 1:
            return "error", f"切片上界越过当前 bar: idx + {up_offset:g}"
        return None

    severity: Severity = "error" if _root_name(node.value) in FULL_FRAME_NAMES else "warn"
    if lower is None:
        return severity, "整段切片 [:], 把当前 bar 之后的行情也算进来了"
    return severity, "开放上界切片 [x:], 把当前 bar 之后的行情也算进来了"


def _tail_index_reason(node: ast.Subscript) -> str | None:
    """整段行情上取负索引 = 直接读末根 bar(未来)。"""
    value = _const_number(node.slice)
    if value is None or value >= 0:
        return None
    root = _root_name(node.value)
    if root in FULL_FRAME_NAMES:
        return f"{root}[{value:g}] 取整段序列尾值, 等价于读未来 bar"
    return None


def _full_series_stat_reason(node: ast.Call) -> str | None:
    """在整段序列上做统计(没有 .iloc[:idx+1] 截断) = 用到未来 bar。

    两种情况不算前视, 直接放行:
      * 统计前已经用 .iloc / .loc 截断到当前 bar;
      * 统计挂在 rolling / ewm / expanding 这类**回溯窗口**算子后面
        (窗口本身只看过去, 是否越界由窗口参数与 center 标志决定)。
    """
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in STAT_METHODS:
        return None
    target = func.value
    for sub in ast.walk(target):
        if isinstance(sub, ast.Attribute) and sub.attr in {
            "iloc",
            "loc",
            "rolling",
            "ewm",
            "expanding",
        }:
            return None
    root = _root_name(target)
    if root in FULL_FRAME_NAMES:
        return f"{root} 上直接 .{func.attr}() 统计整段序列, 含未来 bar"
    return None


def _shift_reason(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "shift" and node.args:
        value = _const_number(node.args[0])
        if value is not None and value < 0:
            return f"shift({value:g}) 负位移 = 前视未来数据"
    return None


def _centered_rolling_reason(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "rolling":
        for kw in node.keywords:
            if kw.arg == "center" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                return "rolling(center=True) 窗口跨越当前 bar, 用到未来数据"
    return None


def _backfill_reason(node: ast.Call) -> str | None:
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr in {"bfill", "backfill"}:
        return f"{func.attr}() 用未来值回填缺失"
    if func.attr == "fillna":
        for kw in node.keywords:
            if (
                kw.arg == "method"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value in {"bfill", "backfill"}
            ):
                return "fillna(method='bfill') 用未来值回填缺失"
    return None


def _dangerous_call_reason(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name) and func.id in FORBIDDEN_NAMES:
        return f"调用被禁名称 {func.id}()"
    if isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_MODULE_ATTRS:
        root = _root_name(func.value)
        if root in ALLOWED_IMPORT_ALIASES:
            return f"模块 IO/进程调用 {root}.{func.attr}()"
    return None


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------


def _scan_imports(tree: ast.AST, add: AddFn) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in ALLOWED_IMPORTS:
                    add("forbidden_import", "error", node, f"禁止导入模块 {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                add("forbidden_import", "error", node, "禁止相对导入")
                continue
            root = (node.module or "").split(".")[0]
            if root not in ALLOWED_IMPORTS:
                add("forbidden_import", "error", node, f"禁止从 {node.module} 导入")


def _scan_nodes(tree: ast.AST, add: AddFn) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            reason = _dangerous_call_reason(node)
            if reason:
                add("dangerous_call", "error", node, reason)
            if isinstance(node.func, ast.Name) and node.func.id == "print":
                add("debug_print", "warn", node, "因子代码不应打印, 会污染调用方输出")
            for rule, severity, finder in (
                ("future_shift", "error", _shift_reason),
                ("future_centered_rolling", "error", _centered_rolling_reason),
                ("future_backfill", "error", _backfill_reason),
                ("future_full_series_stat", "error", _full_series_stat_reason),
            ):
                reason = finder(node)
                if reason:
                    add(rule, severity, node, reason)
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES:
                add("dangerous_call", "error", node, f"引用被禁名称 {node.id}")
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_DUNDERS:
                add("dunder_escape", "error", node, f"反射逃逸属性 .{node.attr}")
        elif isinstance(node, ast.Subscript):
            reason = _subscript_future_reason(node)
            if reason:
                severity, message = reason
                add("future_idx_offset", severity, node, message)
            reason = _tail_index_reason(node)
            if reason:
                add("future_tail_index", "error", node, reason)


def _scan_structure(tree: ast.AST, add: AddFn) -> None:
    compute_nodes = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "compute"
    ]
    if not compute_nodes:
        add("missing_compute", "error", tree, "缺少 compute(df, idx) 入口函数")

    for fn in compute_nodes:
        positional = len(fn.args.posonlyargs) + len(fn.args.args)
        if positional < 2 and fn.args.vararg is None:
            add(
                "bad_compute_signature",
                "error",
                fn,
                f"compute 需要 (df, idx) 两个位置参数, 实际 {positional} 个",
            )
        span = (fn.end_lineno or fn.lineno) - fn.lineno + 1
        if span > MAX_FUNCTION_LINES:
            add(
                "function_too_long",
                "warn",
                fn,
                f"函数体 {span} 行, 超过 {MAX_FUNCTION_LINES} 行, 难审计",
            )

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.While)
            and isinstance(node.test, ast.Constant)
            and node.test.value is True
        ):
            add("unbounded_loop", "warn", node, "while True 无界循环, 因子代码应保证可终止")


def scan_source(code: str, *, name: str = "<string>") -> ScanResult:
    """对一段因子/特征源码做静态体检。"""
    result = ScanResult(name=name, checked_lines=len(code.splitlines()))
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        result.parse_error = f"{exc.msg} (line {exc.lineno})"
        return result

    lines = code.splitlines()
    seen: set[tuple[str, int, int]] = set()

    def add(rule: str, severity: Severity, node: ast.AST, message: str) -> None:
        line = getattr(node, "lineno", 0) or 0
        col = (getattr(node, "col_offset", 0) or 0) + 1
        key = (rule, line, col)
        if key in seen:
            return
        seen.add(key)
        snippet = lines[line - 1].strip() if 0 < line <= len(lines) else ""
        if NOQA_MARK in snippet:
            return
        result.violations.append(Violation(rule, severity, line, col, message, snippet))

    _scan_imports(tree, add)
    _scan_nodes(tree, add)
    _scan_structure(tree, add)

    result.violations.sort(key=lambda v: (v.line, v.col, v.rule))
    return result


# --------------------------------------------------------------------------
# 内置样例(自校验)
# --------------------------------------------------------------------------

_DEMO_CASES: list[tuple[str, str, str]] = [
    (
        "干净时序因子",
        "ok",
        """def compute(df, idx):
    if idx < 20:
        return 0.0
    window = df["close"].iloc[idx - 19 : idx + 1]
    return float(window.std())
""",
    ),
    (
        "前视: idx + 1",
        "error",
        """def compute(df, idx):
    return float(df["close"].iloc[idx + 1])
""",
    ),
    (
        "前视: 开放上界切片",
        "error",
        """def compute(df, idx):
    return float(df["close"].iloc[idx:].mean())
""",
    ),
    (
        "前视: 负 shift",
        "error",
        """def compute(df, idx):
    return float(df["close"].shift(-1).iloc[idx])
""",
    ),
    (
        "前视: 居中滚动",
        "error",
        """def compute(df, idx):
    ma = df["close"].rolling(5, center=True).mean()
    return float(ma.iloc[idx])
""",
    ),
    (
        "前视: bfill",
        "error",
        """def compute(df, idx):
    filled = df["close"].bfill()
    return float(filled.iloc[idx])
""",
    ),
    (
        "前视: 全序列统计",
        "error",
        """def compute(df, idx):
    return float(df["close"].max() - df["close"].iloc[idx])
""",
    ),
    (
        "前视: 取尾值",
        "error",
        """def compute(df, idx):
    return float(df["close"].iloc[-1])
""",
    ),
    (
        "逃逸: exec",
        "error",
        """def compute(df, idx):
    exec("x = 1")
    return 0.0
""",
    ),
    (
        "逃逸: dunder",
        "error",
        """def compute(df, idx):
    return float(len(df.__class__.__subclasses__()))
""",
    ),
    (
        "越权: import os",
        "error",
        """import os


def compute(df, idx):
    return float(len(os.listdir(".")))
""",
    ),
    (
        "结构: 缺 compute",
        "error",
        """def factor(df, idx):
    return 0.0
""",
    ),
    (
        "结构: 调试输出",
        "warn",
        """def compute(df, idx):
    print("debug")
    return 0.0
""",
    ),
]


def _run_demo() -> int:
    rows: list[tuple[str, str, str, str, str]] = []
    failures = 0
    for title, expected, code in _DEMO_CASES:
        result = scan_source(code, name=title)
        if not result.ok:
            got = "error"
        elif result.warns:
            got = "warn"
        else:
            got = "ok"
        rules = ", ".join(sorted({v.rule for v in result.violations})) or "-"
        mark = "OK " if got == expected else "BAD"
        if got != expected:
            failures += 1
        rows.append((mark, title, expected, got, rules))

    width = max(len(r[1]) for r in rows)
    print(f"{'':3} {'样例':<{width}}  {'期望':<5} {'实测':<5} 规则")
    for mark, title, expected, got, rules in rows:
        print(f"{mark} {title:<{width}}  {expected:<5} {got:<5} {rules}")
    print()
    if failures:
        print(f"{failures}/{len(rows)} 个样例与期望不符")
        return 1
    print(f"{len(rows)}/{len(rows)} 个样例符合期望")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _collect_targets(raw_paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in raw_paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(sorted(path.rglob("*.py")))
        elif path.is_file():
            files.append(path)
        else:
            raise FileNotFoundError(raw)
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="feature_guard",
        description="因子/特征代码静态体检器(时序版, 只读不执行)",
    )
    parser.add_argument("paths", nargs="*", help="待检查的 .py 文件或目录")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    parser.add_argument("--quiet", action="store_true", help="只输出未通过的文件")
    parser.add_argument("--demo", action="store_true", help="跑内置样例并打印对照表")
    args = parser.parse_args(argv)

    if args.demo:
        return _run_demo()

    if not args.paths:
        parser.print_usage(sys.stderr)
        return 2

    try:
        targets = _collect_targets(args.paths)
    except FileNotFoundError as exc:
        print(f"路径不存在: {exc}", file=sys.stderr)
        return 2

    results = [scan_source(p.read_text(encoding="utf-8"), name=str(p)) for p in targets]

    if args.json:
        print(json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2))
    else:
        shown = [r for r in results if not args.quiet or not r.ok]
        if not shown:
            print(f"PASS {len(results)} 个文件, 无 error")
        else:
            print("\n\n".join(r.format_text() for r in shown))

    return 1 if any(not r.ok for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
