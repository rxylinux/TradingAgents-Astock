"""N04 evaluation CLI: run, compare, digest."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List, Optional


def _die(msg: str, code: int = 2) -> None:
    print(f"错误: {msg}", file=sys.stderr)
    sys.exit(code)


def _load_json(path: Path, what: str) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        _die(f"无法读取{what} {path}: {e}", 2)


def _write_output(output_dir: Path, files: dict) -> None:
    """Create output dir and write files; on failure clean only what we created."""
    if output_dir.exists():
        _die(f"输出目录已存在，拒绝覆盖: {output_dir}", 2)
    created_files: List[Path] = []
    created_dir = False
    original_error: Exception | None = None
    try:
        output_dir.mkdir(parents=True)
        created_dir = True
        for name, content in files.items():
            p = output_dir / name
            created_files.append(p)  # Register BEFORE write so partial files get cleaned
            p.write_text(content, encoding="utf-8")
    except OSError as e:
        original_error = e
    finally:
        if original_error is not None:
            # Clean only files we actually created in this run; then remove
            # the directory we created (it contains only our files). Use
            # shutil to handle non-empty case, then report the original error.
            # Clean ONLY files we created (including the one being written);
            # never rmtree — other writers' files must survive. Then try rmdir
            # which only succeeds if the directory is now empty (no foreign files).
            for p in reversed(created_files):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
            if created_dir:
                try:
                    output_dir.rmdir()  # Only removes if empty; foreign files survive
                except OSError:
                    pass  # Directory not empty (other writer's file remains) — OK
            _die(f"写入输出失败: {original_error}", 2)


def cmd_run(args: list) -> int:
    suite_path = sub_path = out_dir = None
    i = 0
    while i < len(args):
        if args[i] == "--suite" and i + 1 < len(args):
            suite_path = args[i + 1]
            i += 2
        elif args[i] == "--submission" and i + 1 < len(args):
            sub_path = args[i + 1]
            i += 2
        elif args[i] == "--output-dir" and i + 1 < len(args):
            out_dir = args[i + 1]
            i += 2
        else:
            _die(f"未知参数: {args[i]}", 2)

    if not all([suite_path, sub_path, out_dir]):
        _die("run 需要 --suite FILE --submission FILE --output-dir DIR", 2)

    from .schema import (
        EvaluationInputError,
        validate_all_report_paths,
        validate_submission,
        validate_suite,
    )

    suite_file = Path(suite_path)
    sub_file = Path(sub_path)
    suite = _load_json(suite_file, "案例集")
    submission = _load_json(sub_file, "提交清单")

    try:
        validate_suite(suite)
        validate_submission(submission, suite)
        # Pre-flight all report paths: format violations reject the entire submission
        validate_all_report_paths(submission, sub_file.parent)
    except EvaluationInputError as e:
        _die(str(e), 2)

    from .grader import evaluate_suite

    def loader(path_str: str) -> dict:
        resolved = sub_file.parent / path_str
        return json.loads(resolved.read_text(encoding="utf-8"))

    try:
        result = evaluate_suite(suite, submission, report_loader=loader)
    except EvaluationInputError as e:
        _die(str(e), 2)

    from .render import render_evaluation_markdown

    md = render_evaluation_markdown(result)
    _write_output(Path(out_dir), {
        "evaluation.json": json.dumps(result, ensure_ascii=False, indent=2),
        "evaluation.md": md,
    })
    print(f"评测完成: {out_dir}/evaluation.json + evaluation.md")
    return 0


def cmd_compare(args: list) -> int:
    baseline = candidate = out_dir = None
    fail_on_regression = False
    i = 0
    while i < len(args):
        if args[i] == "--baseline" and i + 1 < len(args):
            baseline = args[i + 1]
            i += 2
        elif args[i] == "--candidate" and i + 1 < len(args):
            candidate = args[i + 1]
            i += 2
        elif args[i] == "--output-dir" and i + 1 < len(args):
            out_dir = args[i + 1]
            i += 2
        elif args[i] == "--fail-on-regression":
            fail_on_regression = True
            i += 1
        else:
            _die(f"未知参数: {args[i]}", 2)

    if not all([baseline, candidate, out_dir]):
        _die("compare 需要 --baseline FILE --candidate FILE --output-dir DIR [--fail-on-regression]", 2)

    base_eval = _load_json(Path(baseline), "基线评测")
    cand_eval = _load_json(Path(candidate), "候选评测")

    from .comparison import compare_evaluations

    try:
        result = compare_evaluations(base_eval, cand_eval)
    except ValueError as e:
        _die(str(e), 2)

    from .render import render_comparison_markdown

    md = render_comparison_markdown(result)
    _write_output(Path(out_dir), {
        "comparison.json": json.dumps(result, ensure_ascii=False, indent=2),
        "comparison.md": md,
    })
    print(f"对比完成: {out_dir}/comparison.json + comparison.md")

    # Gate on per-check regressions, not just overall degraded count
    if fail_on_regression and result["degraded_checks"]:
        print(
            f"发现 {len(result['degraded_checks'])} 个检查级退化 "
            f"(涉及 {result['summary']['degraded']} 个退化 trial)",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_digest(args: list) -> int:
    if len(args) != 1:
        _die("digest 需要 FILE", 2)
    data = _load_json(Path(args[0]), "文件")
    from .schema import canonical_digest
    try:
        print(canonical_digest(data))
    except Exception as e:
        _die(str(e), 2)
    return 0


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(
            "用法: python -m tradingagents.evaluation <command>\n\n"
            "Commands:\n"
            "  run --suite SUITE.json --submission SUB.json --output-dir DIR\n"
            "  compare --baseline BASE/evaluation.json --candidate CAND/evaluation.json\n"
            "         --output-dir DIR [--fail-on-regression]\n"
            "  digest FILE.json\n"
        )
        return 0 if argv else 2

    cmd, rest = argv[0], argv[1:]
    if cmd == "run":
        return cmd_run(rest)
    elif cmd == "compare":
        return cmd_compare(rest)
    elif cmd == "digest":
        return cmd_digest(rest)
    else:
        _die(f"未知命令: {cmd}", 2)


if __name__ == "__main__":
    sys.exit(main())
