import os
import yaml
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent.parent.parent / "config.yaml"


def _load_config():
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _check_allowed(path: str) -> None:
    allowed = _load_config()["tools"]["allowed_paths"]
    target = Path(path).resolve()
    for a in allowed:
        allowed_dir = Path(a).expanduser().resolve()
        # Use parents check — immune to startswith prefix attacks
        if target == allowed_dir or allowed_dir in target.parents:
            return
    raise PermissionError(f"Path not in allowed list: {path}")


def read_file(path: str) -> str:
    _check_allowed(path)
    with open(path, encoding="utf-8") as f:
        return f.read()


def write_file(path: str, content: str) -> str:
    _check_allowed(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Written to {path}."


def list_files(path: str) -> str:
    _check_allowed(path)
    return "\n".join(os.listdir(path))
