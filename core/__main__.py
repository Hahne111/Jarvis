"""Run the JARVIS Core process:  python -m core  (binds 127.0.0.1:7870, no auth yet)."""

from __future__ import annotations

import os

import uvicorn

from core.api.app import create_app
from core.runtime import CoreRuntime


def main() -> None:
    runtime = CoreRuntime.build()
    runtime.recover()
    app = create_app(runtime)
    uvicorn.run(
        app,
        host=os.environ.get("JARVIS_CORE_HOST", "127.0.0.1"),
        port=int(os.environ.get("JARVIS_CORE_PORT", "7870")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
