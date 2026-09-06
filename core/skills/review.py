"""Static skill review (SPEC §15.1 "security review", Phase 12 step 77).

Before a skill is even test-run, the reviewer reads its manifest and parses every Python file
with ``ast``. A skill may import only an allowlist of pure modules plus ``skills.sdk``; anything
that reaches the OS, files, network, processes, dynamic code or the Core internals is a finding
that rejects the skill. Findings are facts (file, line, rule), the report is an event
(``skill.reviewed``); nothing here executes skill code.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from skills.sdk import ManifestError, SkillManifest, load_manifest

ALLOWED_MODULES = frozenset(
    {
        "skills.sdk",
        "typing",
        "dataclasses",
        "json",
        "math",
        "re",
        "datetime",
        "enum",
        "collections",
        "itertools",
        "functools",
        "statistics",
        "decimal",
        "fractions",
        "string",
        "textwrap",
        "uuid",
        "asyncio",
        "random",
        "abc",
        "__future__",
        "operator",
        "heapq",
        "bisect",
        "copy",
        "unicodedata",
        "difflib",
    }
)
ALLOWED_TEST_MODULES = ALLOWED_MODULES | {"pytest", "skill"}
FORBIDDEN_CALLS = frozenset(
    {"eval", "exec", "compile", "open", "__import__", "globals", "locals", "breakpoint", "input"}
)
FORBIDDEN_ATTRS = frozenset(
    {
        "__subclasses__",
        "__globals__",
        "__code__",
        "__builtins__",
        "__loader__",
        "__spec__",
        "__import__",
    }
)
MAX_FILES = 50
MAX_FILE_BYTES = 200_000


@dataclass(frozen=True)
class Finding:
    rule: str
    message: str
    file: str | None = None
    line: int | None = None
    severity: str = "reject"  # reject | warn

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "severity": self.severity,
        }


@dataclass
class ReviewReport:
    path: str
    manifest: SkillManifest | None
    findings: list[Finding] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    sha256: str | None = None

    @property
    def ok(self) -> bool:
        return self.manifest is not None and not any(f.severity == "reject" for f in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "ok": self.ok,
            "skill": self.manifest.name if self.manifest else None,
            "version": self.manifest.version if self.manifest else None,
            "sha256": self.sha256,
            "files": list(self.files),
            "findings": [f.to_dict() for f in self.findings],
        }


def tree_sha256(root: Path) -> tuple[str, list[str]]:
    h = hashlib.sha256()
    files = sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    rel = []
    for p in files:
        r = p.relative_to(root).as_posix()
        rel.append(r)
        h.update(r.encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest(), rel


class SkillReviewer:
    def __init__(self, *, allowed: frozenset[str] = ALLOWED_MODULES) -> None:
        self._allowed = allowed

    def review(self, path: Path | str) -> ReviewReport:
        root = Path(path).resolve()
        report = ReviewReport(path=str(root), manifest=None)
        if not root.is_dir():
            report.findings.append(Finding("path", f"{root} is not a directory"))
            return report
        try:
            report.manifest = load_manifest(root)
        except ManifestError as exc:
            report.findings.append(Finding("manifest", str(exc), "manifest.json"))
            return report
        m = report.manifest
        report.sha256, report.files = tree_sha256(root)
        if len(report.files) > MAX_FILES:
            report.findings.append(
                Finding("size", f"too many files ({len(report.files)} > {MAX_FILES})")
            )
        module_file, _, class_name = m.entrypoint.partition(":")
        entry = root / module_file
        if not entry.is_file():
            report.findings.append(
                Finding("entrypoint", f"entrypoint file {module_file!r} not found", module_file)
            )
        for p in root.rglob("*"):
            if p.is_symlink():
                report.findings.append(
                    Finding(
                        "symlink",
                        "symlinks are not allowed in a skill",
                        p.relative_to(root).as_posix(),
                    )
                )
            if p.is_file() and p.stat().st_size > MAX_FILE_BYTES:
                report.findings.append(
                    Finding("size", "file larger than 200 kB", p.relative_to(root).as_posix())
                )
        tests_dir = root / m.tests
        if not tests_dir.is_dir() or not any(tests_dir.glob("test_*.py")):
            report.findings.append(
                Finding("tests", f"no tests found under {m.tests!r} (test_*.py)", m.tests)
            )
        for py in sorted(root.rglob("*.py")):
            if "__pycache__" in py.parts:
                continue
            rel = py.relative_to(root).as_posix()
            in_tests = rel.startswith(m.tests.rstrip("/") + "/")
            report.findings.extend(self._scan(py, rel, in_tests))
            if py == entry and class_name:
                try:
                    tree = ast.parse(py.read_text(encoding="utf-8"))
                except SyntaxError:
                    tree = None
                if tree is not None and not any(
                    isinstance(n, ast.ClassDef) and n.name == class_name for n in ast.walk(tree)
                ):
                    report.findings.append(
                        Finding(
                            "entrypoint", f"class {class_name!r} not found in {module_file}", rel
                        )
                    )
        declared = {c.name for c in m.capabilities}
        if entry.is_file():
            try:
                tree = ast.parse(entry.read_text(encoding="utf-8"))
                defined = {
                    n.name
                    for n in ast.walk(tree)
                    if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)
                }
                for cap in sorted(declared - defined):
                    report.findings.append(
                        Finding(
                            "capability", f"capability {cap!r} has no handler method", module_file
                        )
                    )
            except SyntaxError:
                pass
        return report

    def _scan(self, py: Path, rel: str, in_tests: bool) -> list[Finding]:
        out: list[Finding] = []
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=rel)
        except SyntaxError as exc:
            return [Finding("syntax", f"syntax error: {exc.msg}", rel, exc.lineno)]
        allowed = ALLOWED_TEST_MODULES if in_tests else self._allowed
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if not self._allowed_module(alias.name, allowed):
                        out.append(
                            Finding(
                                "import",
                                f"import of {alias.name!r} is not allowed in a skill",
                                rel,
                                node.lineno,
                            )
                        )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if node.level and node.level > 0:
                    if not in_tests:
                        out.append(
                            Finding("import", "relative imports are not allowed", rel, node.lineno)
                        )
                    continue
                if not self._allowed_module(mod, allowed):
                    out.append(
                        Finding(
                            "import",
                            f"import from {mod!r} is not allowed in a skill",
                            rel,
                            node.lineno,
                        )
                    )
            elif isinstance(node, ast.Call):
                fn = node.func
                name = (
                    fn.id
                    if isinstance(fn, ast.Name)
                    else fn.attr
                    if isinstance(fn, ast.Attribute)
                    else None
                )
                if name in FORBIDDEN_CALLS:
                    out.append(
                        Finding("call", f"call to {name}() is not allowed", rel, node.lineno)
                    )
            elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
                out.append(
                    Finding("attribute", f"access to {node.attr} is not allowed", rel, node.lineno)
                )
            elif isinstance(node, ast.Name) and node.id in ("__builtins__", "__loader__"):
                out.append(
                    Finding("attribute", f"access to {node.id} is not allowed", rel, node.lineno)
                )
        return out

    @staticmethod
    def _allowed_module(name: str, allowed: frozenset[str]) -> bool:
        if not name:
            return False
        if name in allowed:
            return True
        # allow submodules of allowed packages (typing.io, collections.abc) but never core/adapters
        top = name.split(".")[0]
        return top in allowed and not name.startswith(("core", "adapters", "voice", "apps"))
