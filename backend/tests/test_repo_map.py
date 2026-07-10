"""Repo map step: per-language symbol extraction, exclusions, caps."""

from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.steps.repo_map import build_repo_map, build_repo_map_sync, extract_symbols

FIXTURES: dict[str, str] = {
    "src/app.py": "class Config:\n    pass\n\ndef main():\n    pass\n",
    "src/lib.ts": (
        "export interface Shape { x: number }\nexport function area(s: Shape) { return 0 }\n"
    ),
    "src/ui.tsx": "export function App() { return <div/> }\n",
    "src/util.js": "class Helper { run() {} }\n",
    "src/Main.java": "public class Main { void run() {} }\n",
    "src/main.go": "package main\nfunc main() {}\ntype Server struct{}\n",
    "src/Program.cs": "public class Program { public static void Main() {} }\n",
    "src/lib.rs": "pub struct Engine;\npub fn start() {}\n",
    "README.md": "# readme\n",
    "node_modules/dep/index.js": "function hidden() {}\n",
    ".git/config": "[core]\n",
}


def make_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    for rel, content in FIXTURES.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return tmp_path


def test_symbols_extracted_for_every_supported_language(fixture_repo: Path) -> None:
    repo_map = build_repo_map_sync(fixture_repo, make_settings())
    for expected in (
        "class Config",
        "fn main",
        "interface Shape",
        "fn area",
        "fn App",
        "class Helper",
        "class Main",
        "type Server",
        "class Program",
        "struct Engine",
        "fn start",
    ):
        assert expected in repo_map.text, f"missing: {expected}"
    assert repo_map.symbol_count >= 11


def test_non_source_files_appear_without_symbols(fixture_repo: Path) -> None:
    repo_map = build_repo_map_sync(fixture_repo, make_settings())
    assert "README.md" in repo_map.text


def test_excluded_dirs_are_skipped(fixture_repo: Path) -> None:
    repo_map = build_repo_map_sync(fixture_repo, make_settings())
    assert "node_modules" not in repo_map.text
    assert "hidden" not in repo_map.text
    assert ".git/config" not in repo_map.text


def test_char_cap_truncates(fixture_repo: Path) -> None:
    repo_map = build_repo_map_sync(fixture_repo, make_settings(repo_map_max_chars=50))
    assert repo_map.truncated
    assert "[... repo map truncated ...]" in repo_map.text


def test_file_cap_truncates(fixture_repo: Path) -> None:
    repo_map = build_repo_map_sync(fixture_repo, make_settings(repo_map_max_files=2))
    assert repo_map.truncated
    assert repo_map.file_count == 2


def test_unparseable_file_does_not_fail_the_map(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_bytes(b"\xff\xfe\x00garbage \x00\xff def x(:")
    repo_map = build_repo_map_sync(tmp_path, make_settings())
    assert "broken.py" in repo_map.text  # still listed


async def test_missing_dir_is_typed_error(tmp_path: Path) -> None:
    with pytest.raises(AppError) as excinfo:
        await build_repo_map(tmp_path / "nope")
    assert excinfo.value.code == ErrorCode.REPO_MAP_FAILED


def test_extract_symbols_reports_line_numbers() -> None:
    symbols = extract_symbols("python", b"\n\ndef late():\n    pass\n")
    assert symbols[0].name == "late"
    assert symbols[0].line == 3
