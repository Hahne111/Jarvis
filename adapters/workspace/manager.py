"""WorkspaceManager: one isolated directory per mission, path-sandboxed, with file versions.

Rules (SPEC §12.2 Coding Safety, SECURITY.md §2 rule 4):
- every path is resolved and must stay inside the mission's workspace (no ``..``, no absolute
  paths, no symlinks pointing outside);
- writes keep the previous content under ``.jarvis/versions/`` so they are reversible;
- commands run only from an allowlist, with a timeout, a CPU limit and a scrubbed environment.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

MAX_READ_BYTES = 512_000
VERSION_DIR = ".jarvis/versions"
DEFAULT_ALLOWED_COMMANDS = (
    "python",
    "python3",
    "pytest",
    "node",
    "npm",
    "pnpm",
    "npx",
    "git",
    "ls",
    "cat",
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


class WorkspaceError(ValueError):
    pass


@dataclass(frozen=True)
class RunResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    command: list[str]

    def to_dict(self) -> dict:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout[-4000:],
            "stderr": self.stderr[-4000:],
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "command": self.command,
        }


@dataclass
class WorkspaceManager:
    root: Path
    allowed_commands: tuple[str, ...] = DEFAULT_ALLOWED_COMMANDS
    max_run_seconds: int = 120
    cpu_seconds: int = 60
    max_output_bytes: int = 200_000
    _runs: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # -- paths -----------------------------------------------------------------------------------

    def workspace(self, workspace_id: str) -> Path:
        if not _SAFE_ID.match(workspace_id or ""):
            raise WorkspaceError(f"invalid workspace id {workspace_id!r}")
        ws = self.root / workspace_id
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    def resolve(self, workspace_id: str, rel: str, *, must_exist: bool = False) -> Path:
        ws = self.workspace(workspace_id)
        if rel is None or rel == "":
            rel = "."
        if os.path.isabs(rel) or rel.startswith(("~", "\\\\")):
            raise WorkspaceError("absolute paths are not allowed")
        candidate = ws / rel
        # Reject traversal even if the target does not exist yet.
        if any(part == ".." for part in Path(rel).parts):
            raise WorkspaceError("path traversal is not allowed")
        resolved = candidate.resolve()
        if resolved != ws and ws not in resolved.parents:
            raise WorkspaceError("path escapes the workspace")
        if VERSION_DIR.split("/")[0] in Path(rel).parts:
            raise WorkspaceError("the .jarvis directory is managed by the core")
        if must_exist and not resolved.exists():
            raise WorkspaceError(f"no such file: {rel}")
        return resolved

    # -- files -----------------------------------------------------------------------------------

    def list(self, workspace_id: str, rel: str = ".") -> list[dict]:
        base = self.resolve(workspace_id, rel, must_exist=True)
        ws = self.workspace(workspace_id)
        out = []
        for p in sorted(base.rglob("*") if base.is_dir() else [base]):
            if VERSION_DIR.split("/")[0] in p.relative_to(ws).parts:
                continue
            if p.is_symlink():
                continue  # links are never followed (they could point outside the sandbox)
            out.append(
                {
                    "path": p.relative_to(ws).as_posix(),
                    "dir": p.is_dir(),
                    "size": p.stat().st_size if p.is_file() else None,
                }
            )
        return out

    def read(self, workspace_id: str, rel: str) -> str:
        p = self.resolve(workspace_id, rel, must_exist=True)
        if not p.is_file():
            raise WorkspaceError(f"not a file: {rel}")
        if p.stat().st_size > MAX_READ_BYTES:
            raise WorkspaceError(f"file too large to read ({p.stat().st_size} bytes)")
        return p.read_text(encoding="utf-8", errors="replace")

    def write(self, workspace_id: str, rel: str, content: str) -> dict:
        p = self.resolve(workspace_id, rel)
        if p.exists() and not p.is_file():
            raise WorkspaceError(f"not a file: {rel}")
        ws = self.workspace(workspace_id)
        previous = p.read_text(encoding="utf-8", errors="replace") if p.exists() else None
        version = None
        if previous is not None:
            vdir = ws / VERSION_DIR / p.relative_to(ws).parent
            vdir.mkdir(parents=True, exist_ok=True)
            version = vdir / f"{p.name}.{int(time.time() * 1000)}.bak"
            shutil.copy2(p, version)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {
            "path": p.relative_to(ws).as_posix(),
            "bytes": len(content.encode("utf-8")),
            "sha256": self.sha256(content),
            "created": previous is None,
            "previous_version": version.relative_to(ws).as_posix() if version else None,
            "diff": self._diff(previous or "", content, p.relative_to(ws).as_posix()),
        }

    def diff(self, workspace_id: str, rel: str) -> str:
        """Diff between the latest saved version and the current file."""
        p = self.resolve(workspace_id, rel, must_exist=True)
        ws = self.workspace(workspace_id)
        vdir = ws / VERSION_DIR / p.relative_to(ws).parent
        versions = sorted(vdir.glob(f"{p.name}.*.bak")) if vdir.exists() else []
        before = versions[-1].read_text(encoding="utf-8", errors="replace") if versions else ""
        return self._diff(
            before, p.read_text(encoding="utf-8", errors="replace"), p.relative_to(ws).as_posix()
        )

    @staticmethod
    def _diff(before: str, after: str, name: str) -> str:
        return "".join(
            difflib.unified_diff(
                before.splitlines(True), after.splitlines(True), f"a/{name}", f"b/{name}"
            )
        )

    @staticmethod
    def sha256(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def file_sha256(self, workspace_id: str, rel: str) -> str | None:
        try:
            return self.sha256(self.read(workspace_id, rel))
        except WorkspaceError:
            return None

    # -- runs ------------------------------------------------------------------------------------

    async def run(
        self,
        workspace_id: str,
        command: str,
        args: list[str] | None = None,
        *,
        timeout_s: int | None = None,
        on_output=None,
    ) -> RunResult:
        args = [str(a) for a in (args or [])]
        exe = os.path.basename(command)
        if exe not in self.allowed_commands or exe != command:
            raise WorkspaceError(
                f"command {command!r} is not on the allowlist {self.allowed_commands}"
            )
        if any("\x00" in a or a.startswith("~") for a in args):
            raise WorkspaceError("invalid argument")
        for a in args:
            if os.path.isabs(a) or ".." in Path(a).parts:
                raise WorkspaceError(f"argument {a!r} points outside the workspace")
        ws = self.workspace(workspace_id)
        resolved_exe = sys.executable if exe in ("python", "python3") else shutil.which(exe)
        if not resolved_exe:
            raise WorkspaceError(f"{exe} is not installed")
        timeout = min(int(timeout_s or self.max_run_seconds), self.max_run_seconds)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(ws),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "JARVIS_WORKSPACE": str(ws),
        }
        cpu = self.cpu_seconds

        def _limits() -> None:  # POSIX only; runs in the child before exec
            try:
                import resource

                resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
            except Exception:  # noqa: S110 - best effort on platforms without rlimits
                pass

        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            resolved_exe,
            *args,
            cwd=str(ws),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=_limits if os.name == "posix" else None,
        )
        out_buf, err_buf = [], []
        total = 0

        async def pump(stream, buf, name):
            nonlocal total
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    return
                total += len(chunk)
                if total <= self.max_output_bytes:
                    text = chunk.decode("utf-8", errors="replace")
                    buf.append(text)
                    if on_output is not None:
                        await on_output(name, text)

        timed_out = False
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    pump(proc.stdout, out_buf, "stdout"),
                    pump(proc.stderr, err_buf, "stderr"),
                    proc.wait(),
                ),
                timeout=timeout,
            )
        except TimeoutError:
            timed_out = True
            proc.kill()
            await proc.wait()
        return RunResult(
            exit_code=proc.returncode if not timed_out else None,
            stdout="".join(out_buf),
            stderr="".join(err_buf),
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=timed_out,
            command=[exe, *args],
        )
