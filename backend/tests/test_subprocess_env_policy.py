"""Security invariant 6, static half: every subprocess the app launches states
its environment explicitly.

A child that silently inherits `os.environ` inherits every server secret. This
walks the AST of `backend/app/` and requires an `env=` keyword on each
`subprocess`/`asyncio` process launch, so adding one is a visible decision —
unless the call site is in `_ALLOWED_INHERITING_CALLS` below, with a reason.
It doesn't prove the env passed is safe; the canary run in
test_validation_isolation.py checks that for the one place that executes
untrusted repo code.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent / "app"

_SUBPROCESS_FUNCS = {"run", "Popen", "call", "check_call", "check_output"}
_ASYNCIO_FUNCS = {"create_subprocess_exec", "create_subprocess_shell"}

# (path relative to app/, enclosing function) -> why inheriting is acceptable.
_ALLOWED_INHERITING_CALLS: dict[tuple[str, str], str] = {
    ("jobs/runner.py", "_run_git_command"): (
        "git diff/add/rev-parse on the job workspace for diff collection; "
        "runs the trusted git binary, no repo code"
    ),
    ("steps/branch_prep.py", "_validate_branch_name"): (
        "`git check-ref-format --branch`: a pure string check, no repository access"
    ),
}


def _is_process_launch(func: ast.expr) -> bool:
    if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
        return False
    module, name = func.value.id, func.attr
    return (module == "subprocess" and name in _SUBPROCESS_FUNCS) or (
        module == "asyncio" and name in _ASYNCIO_FUNCS
    )


def _launches(call: ast.Call) -> bool:
    if _is_process_launch(call.func):
        return True
    # asyncio.to_thread(subprocess.run, argv, ..., env=...): the keywords
    # belong to the to_thread call.
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "to_thread"
        and bool(call.args)
        and _is_process_launch(call.args[0])
    )


def _inherits_env(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and _launches(node)
        and not any(keyword.arg == "env" for keyword in node.keywords)
    )


class _Finder(ast.NodeVisitor):
    """Records (enclosing function, line) of each launch without env=."""

    def __init__(self) -> None:
        self.found: list[tuple[str, int]] = []
        self._functions: list[str] = ["<module>"]

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._functions.append(node.name)
        self.generic_visit(node)
        self._functions.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Call(self, node: ast.Call) -> None:
        if _inherits_env(node):
            self.found.append((self._functions[-1], node.lineno))
        self.generic_visit(node)


def _find(tree: ast.AST) -> list[tuple[str, int]]:
    finder = _Finder()
    finder.visit(tree)
    return finder.found


def _process_launches_without_env() -> list[tuple[str, str, int]]:
    found: list[tuple[str, str, int]] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        relative = path.relative_to(APP_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found.extend((relative, function, line) for function, line in _find(tree))
    return found


def test_every_process_launch_passes_env_explicitly() -> None:
    offenders = [
        f"{path}:{line} in {function}()"
        for path, function, line in _process_launches_without_env()
        if (path, function) not in _ALLOWED_INHERITING_CALLS
    ]
    assert not offenders, (
        "These process launches inherit the backend's environment (every server "
        "secret). Pass env= explicitly, or add the call site to "
        "_ALLOWED_INHERITING_CALLS with a reason:\n" + "\n".join(offenders)
    )


def test_allowlist_has_no_stale_entries() -> None:
    present = {(path, function) for path, function, _ in _process_launches_without_env()}
    stale = set(_ALLOWED_INHERITING_CALLS) - present
    assert not stale, f"Remove allowlist entries that no longer match a call site: {stale}"


def test_the_check_detects_an_inheriting_call() -> None:
    source = (
        "import asyncio, subprocess\n"
        "def bad():\n"
        "    subprocess.run(['x'])\n"
        "async def also_bad():\n"
        "    await asyncio.to_thread(subprocess.run, ['x'])\n"
        "    def inner():\n"
        "        return subprocess.Popen(['x'])\n"
        "def fine():\n"
        "    subprocess.run(['x'], env={})\n"
    )
    assert _find(ast.parse(source)) == [("bad", 3), ("also_bad", 5), ("inner", 7)]
