"""AST and diff utilities shared by the analysis rules."""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass, field
from typing import Any

HUNK_HEADER_RE = re.compile(r"^@@ -(?P<os>\d+)(?:,(?P<ol>\d+))? \+(?P<ns>\d+)(?:,(?P<nl>\d+))? @@")


@dataclass
class DiffLine:
    new_lineno: int | None
    old_lineno: int | None
    kind: str  # "add" | "del" | "ctx"
    text: str


@dataclass
class Hunk:
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    lines: list[DiffLine] = field(default_factory=list)

    @property
    def added(self) -> list[DiffLine]:
        return [ln for ln in self.lines if ln.kind == "add"]

    @property
    def removed(self) -> list[DiffLine]:
        return [ln for ln in self.lines if ln.kind == "del"]


@dataclass
class FileDiff:
    path: str
    old_path: str = ""
    new_file: bool = False
    deleted_file: bool = False
    renamed: bool = False
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def changed_new_lines(self) -> set[int]:
        out: set[int] = set()
        for h in self.hunks:
            for ln in h.added:
                if ln.new_lineno is not None:
                    out.add(ln.new_lineno)
        return out

    @property
    def changed_old_lines(self) -> set[int]:
        out: set[int] = set()
        for h in self.hunks:
            for ln in h.removed:
                if ln.old_lineno is not None:
                    out.add(ln.old_lineno)
        return out

    def unified(self, context: int = 3) -> str:
        out: list[str] = [f"--- a/{self.old_path or self.path}", f"+++ b/{self.path}"]
        for h in self.hunks:
            out.append(f"@@ -{h.old_start},{h.old_lines} +{h.new_start},{h.new_lines} @@")
            for ln in h.lines:
                prefix = {"add": "+", "del": "-", "ctx": " "}[ln.kind]
                out.append(prefix + ln.text)
        return "\n".join(out)


def parse_unified_diff(diff_text: str) -> list[FileDiff]:
    """Parse a git/GitLab-style unified diff into structured FileDiff objects."""
    files: list[FileDiff] = []
    current: FileDiff | None = None
    hunk: Hunk | None = None
    old_no = new_no = 0

    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            current = FileDiff(path="")
            files.append(current)
            hunk = None
            m = re.match(r"diff --git a/(.+) b/(.+)$", raw)
            if m:
                current.old_path, current.path = m.group(1), m.group(2)
            continue
        if current is None:
            continue
        if raw.startswith("new file mode"):
            current.new_file = True
            continue
        if raw.startswith("deleted file mode"):
            current.deleted_file = True
            continue
        if raw.startswith("rename from "):
            current.renamed = True
            current.old_path = raw[len("rename from ") :]
            continue
        if raw.startswith("rename to "):
            current.renamed = True
            current.path = raw[len("rename to ") :]
            continue
        if raw.startswith("--- "):
            p = raw[4:].strip()
            current.old_path = p[2:] if p.startswith("a/") else p
            continue
        if raw.startswith("+++ "):
            p = raw[4:].strip()
            current.path = p[2:] if p.startswith("b/") else p
            continue
        if raw.startswith(("index ", "similarity ", "dissimilarity ", "old mode", "new mode", "Binary files")):
            continue
        m = HUNK_HEADER_RE.match(raw)
        if m:
            hunk = Hunk(
                old_start=int(m.group("os")),
                old_lines=int(m.group("ol") or 1),
                new_start=int(m.group("ns")),
                new_lines=int(m.group("nl") or 1),
            )
            current.hunks.append(hunk)
            old_no, new_no = hunk.old_start, hunk.new_start
            continue
        if hunk is None:
            continue
        if raw.startswith("+"):
            hunk.lines.append(DiffLine(new_no, None, "add", raw[1:]))
            new_no += 1
        elif raw.startswith("-"):
            hunk.lines.append(DiffLine(None, old_no, "del", raw[1:]))
            old_no += 1
        elif raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        else:
            hunk.lines.append(DiffLine(new_no, old_no, "ctx", raw[1:] if raw.startswith(" ") else raw))
            new_no += 1
            old_no += 1
    return [f for f in files if f.path]


def diff_from_sources(path: str, old_src: str, new_src: str, context: int = 3) -> FileDiff:
    """Build a FileDiff from two full file contents (used in local demo mode)."""
    old_lines = old_src.splitlines()
    new_lines = new_src.splitlines()
    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    fd = FileDiff(path=path, old_path=path)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        o_start = i1 + 1 if i1 < len(old_lines) else i1
        n_start = j1 + 1 if j1 < len(new_lines) else j1
        hunk = Hunk(old_start=o_start, old_lines=i2 - i1, new_start=n_start, new_lines=j2 - j1)
        # leading context
        for k in range(max(0, i1 - context), i1):
            off_i = k - i1
            hunk.lines.append(DiffLine(j1 + off_i + 1, k + 1, "ctx", old_lines[k]))
            hunk.old_lines += 1
            hunk.new_lines += 1
        if tag in ("replace", "delete"):
            for k in range(i1, i2):
                hunk.lines.append(DiffLine(None, k + 1, "del", old_lines[k]))
        if tag in ("replace", "insert"):
            for k in range(j1, j2):
                hunk.lines.append(DiffLine(k + 1, None, "add", new_lines[k]))
        for k in range(i2, min(len(old_lines), i2 + context)):
            off_j = j2 + (k - i2)
            hunk.lines.append(DiffLine(off_j + 1, k + 1, "ctx", old_lines[k]))
            hunk.old_lines += 1
            hunk.new_lines += 1
        fd.hunks.append(hunk)
    return fd


def parse_source(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def functions_of(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def function_at_line(
    tree: ast.AST, lineno: int
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Innermost function enclosing ``lineno``."""
    best = None
    for fn in functions_of(tree):
        end = getattr(fn, "end_lineno", fn.lineno) or fn.lineno
        if fn.lineno <= lineno <= end:
            if best is None or fn.lineno >= best.lineno:
                best = fn
    return best


def functions_touching(tree: ast.AST, linenos: set[int]) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    if not linenos:
        return []
    out = []
    for fn in functions_of(tree):
        end = getattr(fn, "end_lineno", fn.lineno) or fn.lineno
        if any(fn.lineno <= ln <= end for ln in linenos):
            out.append(fn)
    return out


def enclosing_qualname(tree: ast.AST, target: ast.AST) -> str:
    """Dotted path (Class.method) for a node."""
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    parts: list[str] = []
    cur: ast.AST | None = target
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parts.append(cur.name)
        cur = parents.get(id(cur))
    return ".".join(reversed(parts))


def source_segment(source: str, node: ast.AST) -> str:
    try:
        return ast.get_source_segment(source, node) or ""
    except Exception:  # noqa: BLE001
        return ""


def lines_of(source: str, start: int, end: int) -> str:
    parts = source.splitlines()
    start = max(1, start)
    end = min(len(parts), end)
    return "\n".join(f"{i:>5} | {parts[i - 1]}" for i in range(start, end + 1))


def return_expressions(fn: ast.AST) -> list[ast.expr]:
    """Return expressions belonging directly to ``fn`` (not nested functions)."""
    out: list[ast.expr] = []

    class V(ast.NodeVisitor):
        def visit_FunctionDef(self, node):  # noqa: N802
            if node is not fn:
                return  # do not descend into nested functions
            self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Return(self, node):  # noqa: N802
            if node.value is not None:
                out.append(node.value)

    V().visit(fn)
    return out


def names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def calls_in(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def comparisons_in(node: ast.AST) -> list[ast.Compare]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Compare)]


def guard_statements(fn: ast.AST) -> list[ast.If]:
    """Top-level ``if`` statements in the function body (validation guards)."""
    return [s for s in getattr(fn, "body", []) if isinstance(s, ast.If)]


def raises_in(fn: ast.AST) -> list[ast.Raise]:
    return [n for n in ast.walk(fn) if isinstance(n, ast.Raise)]


def normalize_expr(node: ast.expr) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001
        return ""


def to_jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    return str(value)
