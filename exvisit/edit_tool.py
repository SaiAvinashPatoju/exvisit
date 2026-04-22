"""AST-located surgical edit primitive for Python source files."""
from __future__ import annotations

import argparse
import ast
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass
class LocatorSpan:
    locator: str
    start_line: int
    end_line: int
    start_col: int
    end_col: int
    start_char: int
    end_char: int
    start_byte: int
    end_byte: int


@dataclass
class EditResult:
    file_path: str
    locator: str
    replaced_count: int
    dry_run: bool
    span: LocatorSpan


def _line_offsets(text: str) -> List[int]:
    offsets = [0]
    running = 0
    for line in text.splitlines(keepends=True):
        running += len(line)
        offsets.append(running)
    return offsets


def _char_offset(offsets: Sequence[int], line: int, col: int) -> int:
    if line <= 0:
        raise ValueError(f"invalid 1-based line number: {line}")
    if line - 1 >= len(offsets):
        return offsets[-1]
    return offsets[line - 1] + col


def _iter_locators(body: Sequence[ast.stmt], prefix: str = "") -> Iterable[Tuple[str, ast.AST]]:
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            locator = f"{prefix}.{node.name}" if prefix else node.name
            yield locator, node
            child_body = getattr(node, "body", None)
            if isinstance(child_body, list):
                yield from _iter_locators(child_body, prefix=locator)


def resolve_locator_span(source_text: str, locator: str) -> LocatorSpan:
    tree = ast.parse(source_text)
    offsets = _line_offsets(source_text)
    spans: Dict[str, LocatorSpan] = {}
    for name, node in _iter_locators(tree.body):
        start_line = getattr(node, "lineno", None)
        end_line = getattr(node, "end_lineno", None)
        start_col = getattr(node, "col_offset", None)
        end_col = getattr(node, "end_col_offset", None)
        if not all(isinstance(value, int) for value in (start_line, end_line, start_col, end_col)):
            continue
        start_char = _char_offset(offsets, start_line, start_col)
        end_char = _char_offset(offsets, end_line, end_col)
        spans[name] = LocatorSpan(
            locator=name,
            start_line=start_line,
            end_line=end_line,
            start_col=start_col,
            end_col=end_col,
            start_char=start_char,
            end_char=end_char,
            start_byte=len(source_text[:start_char].encode("utf-8")),
            end_byte=len(source_text[:end_char].encode("utf-8")),
        )

    direct = spans.get(locator)
    if direct is not None:
        return direct
    suffix_matches = [span for name, span in spans.items() if name.endswith(f".{locator}") or name == locator]
    if len(suffix_matches) == 1:
        return suffix_matches[0]
    if not suffix_matches:
        raise KeyError(f"locator '{locator}' not found")
    raise KeyError(f"locator '{locator}' is ambiguous; use a longer qualifier")


def _find_unique_match(haystack: str, needle: str) -> int:
    positions = []
    start = 0
    while True:
        idx = haystack.find(needle, start)
        if idx < 0:
            break
        positions.append(idx)
        start = idx + len(needle)
    if not positions:
        raise ValueError("old snippet not found inside locator span")
    if len(positions) != 1:
        raise ValueError("old snippet matched multiple times inside locator span")
    return positions[0]


def apply_locator_edit(file_path: Path, locator: str, old_text: str, new_text: str, dry_run: bool = False) -> EditResult:
    raw_bytes = file_path.read_bytes()
    has_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
    source_text = raw_bytes.decode("utf-8-sig")
    span = resolve_locator_span(source_text, locator)
    scoped_text = source_text[span.start_char:span.end_char]
    local_idx = _find_unique_match(scoped_text, old_text)
    absolute_idx = span.start_char + local_idx
    updated_text = source_text[:absolute_idx] + new_text + source_text[absolute_idx + len(old_text):]
    if not dry_run:
        payload = updated_text.encode("utf-8")
        if has_bom:
            payload = b"\xef\xbb\xbf" + payload
        file_path.write_bytes(payload)
    return EditResult(
        file_path=str(file_path),
        locator=span.locator,
        replaced_count=1,
        dry_run=dry_run,
        span=span,
    )


def _load_text_arg(raw: Optional[str], file_arg: Optional[str]) -> str:
    if raw is not None:
        return raw
    if file_arg is None:
        raise ValueError("one of --old/--old-file or --new/--new-file is required")
    return Path(file_arg).read_text(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="exvisit_edit")
    parser.add_argument("--file", required=True)
    parser.add_argument("--locator", required=True)
    old_group = parser.add_mutually_exclusive_group(required=True)
    old_group.add_argument("--old", default=None)
    old_group.add_argument("--old-file", default=None)
    new_group = parser.add_mutually_exclusive_group(required=True)
    new_group.add_argument("--new", default=None)
    new_group.add_argument("--new-file", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    old_text = _load_text_arg(args.old, args.old_file)
    new_text = _load_text_arg(args.new, args.new_file)
    result = apply_locator_edit(Path(args.file), args.locator, old_text, new_text, dry_run=args.dry_run)
    if args.format == "json":
        print(json.dumps(asdict(result), indent=2))
    else:
        print(f"edited {result.file_path} at {result.locator} bytes={result.span.start_byte}..{result.span.end_byte}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
