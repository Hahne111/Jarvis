import os
import yaml
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent.parent.parent / "config.yaml"


def _load_config():
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _check_allowed(path: str) -> Path:
    """Validate path against the sandbox; returns the expanded, resolved path.
    '~' is expanded so '~/Documents/x.txt' works the same way as the allowed_paths entries."""
    allowed = _load_config()["tools"]["allowed_paths"]
    target = Path(path).expanduser().resolve()
    for a in allowed:
        allowed_dir = Path(a).expanduser().resolve()
        # Use parents check — immune to startswith prefix attacks
        if target == allowed_dir or allowed_dir in target.parents:
            return target
    raise PermissionError(f"Path not in allowed list: {path}")


def read_file(path: str) -> str:
    target = _check_allowed(path)
    with open(target, encoding="utf-8") as f:
        return f.read()


def write_file(path: str, content: str) -> str:
    target = _check_allowed(path)
    with open(target, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Written to {path}."


def list_files(path: str) -> str:
    target = _check_allowed(path)
    return "\n".join(os.listdir(target))
