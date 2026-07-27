"""Job step (c): deterministic repo map via tree-sitter. No LLM.

Produces the planning agent's starting context: a file tree with the symbols
(classes, functions, types) defined in each source file. Best-effort per file:
an unparseable file still appears in the tree, just without symbols.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node
from tree_sitter_language_pack import SupportedLanguage, get_parser

from app.core.config import Settings, get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.schemas.repomap import RepoMap

logger = get_logger(__name__)

LANGUAGE_BY_EXTENSION: dict[str, SupportedLanguage] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".java": "java",
    ".go": "go",
    ".cs": "csharp",
    ".rs": "rust",
}

# node type -> human kind label, per language (verified against installed grammars)
_COMMON_TS_JS = {
    "function_declaration": "fn",
    "class_declaration": "class",
    "method_definition": "method",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
}
SYMBOL_NODE_KINDS: dict[str, dict[str, str]] = {
    "python": {"function_definition": "fn", "class_definition": "class"},
    "typescript": _COMMON_TS_JS,
    "tsx": _COMMON_TS_JS,
    "javascript": _COMMON_TS_JS,
    "java": {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "method_declaration": "method",
        "enum_declaration": "enum",
    },
    "go": {
        "function_declaration": "fn",
        "method_declaration": "method",
        "type_spec": "type",
    },
    "csharp": {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "struct_declaration": "struct",
        "enum_declaration": "enum",
        "method_declaration": "method",
    },
    "rust": {
        "function_item": "fn",
        "struct_item": "struct",
        "enum_item": "enum",
        "trait_item": "trait",
    },
}

EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".next",
    "dist",
    "build",
    "target",
    "vendor",
    ".idea",
    ".vscode",
    # Additional generated / tooling / cache dirs — keep the map high-signal so
    # the agent spends fewer tokens on noise.
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".tox",
    ".gradle",
    ".terraform",
    ".dart_tool",
    ".svelte-kit",
    ".turbo",
    ".cache",
    ".nuxt",
    ".parcel-cache",
    "coverage",
    "htmlcov",
    "out",
    "bin",
    "obj",
    "Pods",
    "__snapshots__",
}

_MAX_SYMBOLS_PER_FILE = 100


@dataclass(frozen=True)
class Symbol:
    kind: str
    name: str
    line: int  # 1-based


def extract_symbols(language: SupportedLanguage, source: bytes) -> list[Symbol]:
    kinds = SYMBOL_NODE_KINDS[language]
    parser = get_parser(language)
    tree = parser.parse(source)
    symbols: list[Symbol] = []

    def walk(node: Node) -> None:
        if len(symbols) >= _MAX_SYMBOLS_PER_FILE:
            return
        kind = kinds.get(node.type)
        if kind is not None:
            name_node = node.child_by_field_name("name")
            if name_node is not None and name_node.text is not None:
                symbols.append(
                    Symbol(
                        kind=kind,
                        name=name_node.text.decode(errors="replace"),
                        line=node.start_point[0] + 1,
                    )
                )
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return symbols


def _iter_files(root: Path, max_files: int) -> tuple[list[Path], bool]:
    files: list[Path] = []
    truncated = False
    stack = [root]
    while stack:
        directory = stack.pop()
        for entry in sorted(directory.iterdir(), key=lambda p: (p.is_dir(), p.name.lower())):
            if entry.is_dir():
                if entry.name not in EXCLUDED_DIRS:
                    stack.append(entry)
                continue
            if len(files) >= max_files:
                truncated = True
                return files, truncated
            files.append(entry)
    return files, truncated


def build_repo_map_sync(clone_path: Path, settings: Settings) -> RepoMap:
    if not clone_path.is_dir():
        raise AppError(ErrorCode.REPO_MAP_FAILED, internal_detail=f"missing dir {clone_path}")

    files, truncated = _iter_files(clone_path, settings.repo_map_max_files)
    lines: list[str] = []
    symbol_count = 0

    for file_path in sorted(files, key=lambda p: p.relative_to(clone_path).as_posix()):
        rel = file_path.relative_to(clone_path).as_posix()
        lines.append(rel)
        language = LANGUAGE_BY_EXTENSION.get(file_path.suffix.lower())
        if language is None:
            continue
        try:
            if file_path.stat().st_size > settings.repo_map_max_file_bytes:
                continue
            source = file_path.read_bytes()
            for symbol in extract_symbols(language, source):
                lines.append(f"  {symbol.kind} {symbol.name} (line {symbol.line})")
                symbol_count += 1
        except Exception:
            # Best effort: an unparseable file still appears in the tree.
            logger.warning("repo map: failed to parse %s", rel)

    text = "\n".join(lines)
    if len(text) > settings.repo_map_max_chars:
        text = text[: settings.repo_map_max_chars] + "\n[... repo map truncated ...]"
        truncated = True

    logger.info(
        "repo map: %d files, %d symbols%s",
        len(files),
        symbol_count,
        " (truncated)" if truncated else "",
    )
    return RepoMap(text=text, file_count=len(files), symbol_count=symbol_count, truncated=truncated)


async def build_repo_map(clone_path: Path) -> RepoMap:
    settings = get_settings()
    try:
        return await asyncio.to_thread(build_repo_map_sync, clone_path, settings)
    except AppError:
        raise
    except Exception as exc:
        raise AppError(
            ErrorCode.REPO_MAP_FAILED, internal_detail=f"{type(exc).__name__}: {exc}"
        ) from exc
