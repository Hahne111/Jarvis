"""Jarvis Web UI — runs alongside voice as a background thread."""
from __future__ import annotations
import asyncio
import json
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse

from jarvis.tts import speak_streamed, stop_speaking, is_speaking

_STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Jarvis")
_clients: list[WebSocket] = []
_loop: asyncio.AbstractEventLoop | None = None


# ─── Capture the REAL event loop at startup ───

@app.on_event("startup")
async def _capture_loop():
    global _loop
    _loop = asyncio.get_running_loop()
    print("[Web] Event loop captured — WebSocket broadcast ready.")


# ─── Thread-safe broadcast ───

def _broadcast_to_ws(event: dict) -> None:
    """Send event to all connected WebSocket clients.
    Thread-safe — can be called from voice/keyboard threads."""
    if not _loop or _loop.is_closed():
        return
    try:
        asyncio.run_coroutine_threadsafe(_async_broadcast(event), _loop)
    except RuntimeError:
        pass  # loop closed during shutdown


async def _async_broadcast(event: dict) -> None:
    """Actually send the event — runs on the event loop thread."""
    data = json.dumps(event)
    for ws in list(_clients):
        try:
            await ws.send_text(data)
        except Exception:
            if ws in _clients:
                _clients.remove(ws)


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((_STATIC_DIR / "index.html").read_text(encoding="utf-8"))


# ─── Provider APIs ───

@app.get("/api/providers")
async def api_providers():
    from jarvis.main import get_providers, get_active_provider
    providers = get_providers()
    active = get_active_provider()
    result = {}
    for key, prov in providers.items():
        result[key] = {
            "label": prov.get("label", key),
            "model": prov.get("model", ""),
            "type": prov.get("type", "ollama"),
            "base_url": prov.get("base_url", ""),
            "api_key": prov.get("api_key", ""),
        }
    return JSONResponse({"providers": result, "active": active})


@app.post("/api/providers/{provider_key}")
async def api_set_provider(provider_key: str):
    from jarvis.main import set_active_provider
    ok = set_active_provider(provider_key)
    if ok:
        return JSONResponse({"status": "ok", "active": provider_key})
    return JSONResponse({"status": "error", "message": "Unknown provider"}, status_code=400)


@app.put("/api/providers/{provider_key}")
async def api_upsert_provider(provider_key: str, request: Request):
    """Add or update a provider."""
    from jarvis.main import _load_config, _save_config
    body = await request.json()
    cfg = _load_config()
    providers = cfg.setdefault("llm", {}).setdefault("providers", {})
    providers[provider_key] = {
        "type": body.get("type", "openai"),
        "label": body.get("label", provider_key),
        "model": body.get("model", ""),
        "base_url": body.get("base_url", ""),
    }
    if body.get("api_key"):
        providers[provider_key]["api_key"] = body["api_key"]
    _save_config(cfg)
    return JSONResponse({"status": "ok", "provider": provider_key})


@app.delete("/api/providers/{provider_key}")
async def api_delete_provider(provider_key: str):
    """Delete a provider."""
    from jarvis.main import _load_config, _save_config, get_active_provider
    cfg = _load_config()
    providers = cfg.get("llm", {}).get("providers", {})
    if provider_key not in providers:
        return JSONResponse({"status": "error", "message": "Not found"}, status_code=404)
    if provider_key == get_active_provider():
        return JSONResponse({"status": "error", "message": "Cannot delete active provider"}, status_code=400)
    del providers[provider_key]
    _save_config(cfg)
    return JSONResponse({"status": "ok"})


# ─── Settings APIs ───

@app.get("/api/settings")
async def api_get_settings():
    """Return all configurable settings."""
    from jarvis.main import _load_config
    cfg = _load_config()
    return JSONResponse({
        "tts": cfg.get("tts", {}),
        "stt": cfg.get("stt", {}),
        "wake_word": cfg.get("wake_word", {}),
        "context": cfg.get("context", {}),
        "llm": {
            "temperature": cfg.get("llm", {}).get("temperature", 0.7),
            "active_provider": cfg.get("llm", {}).get("active_provider", "ollama"),
        },
        "tools": cfg.get("tools", {}),
    })


@app.put("/api/settings")
async def api_update_settings(request: Request):
    """Update settings. Accepts partial updates."""
    from jarvis.main import _load_config, _save_config
    body = await request.json()
    cfg = _load_config()

    if "tts" in body:
        cfg.setdefault("tts", {}).update(body["tts"])
    if "stt" in body:
        cfg.setdefault("stt", {}).update(body["stt"])
    if "wake_word" in body:
        cfg.setdefault("wake_word", {}).update(body["wake_word"])
    if "context" in body:
        cfg.setdefault("context", {}).update(body["context"])
    if "llm" in body:
        if "temperature" in body["llm"]:
            cfg.setdefault("llm", {})["temperature"] = body["llm"]["temperature"]
    if "tools" in body:
        cfg.setdefault("tools", {}).update(body["tools"])

    _save_config(cfg)
    return JSONResponse({"status": "ok"})


# ─── Mute APIs ───

@app.get("/api/mute")
async def api_get_mute():
    from jarvis.main import is_muted
    return JSONResponse({"muted": is_muted()})


@app.post("/api/mute")
async def api_toggle_mute():
    from jarvis.main import toggle_mute, is_muted
    toggle_mute()
    return JSONResponse({"muted": is_muted()})


# ─── WebSocket ───

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _clients.append(ws)
    await ws.send_text(json.dumps({"type": "connected", "message": "Jarvis online."}))
    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)

            if msg.get("type") == "chat":
                user_text = msg["text"]

                # "stop" typed in chat = abort everything
                from jarvis.main import _is_stop_command
                if _is_stop_command(user_text):
                    from jarvis.main import abort_all
                    abort_all()
                    await ws.send_text(json.dumps({"type": "response", "text": "Stopped."}))
                    continue

                from jarvis.main import _process_request, _Aborted
                loop = asyncio.get_event_loop()
                try:
                    response = await loop.run_in_executor(
                        None, _process_request, user_text,
                    )
                    from jarvis.main import _speak_streamed_if_unmuted
                    threading.Thread(target=_speak_streamed_if_unmuted, args=(response,), daemon=True).start()
                except _Aborted:
                    await ws.send_text(json.dumps({"type": "response", "text": "Stopped."}))
                except Exception as e:
                    await ws.send_text(json.dumps({"type": "response", "text": f"Error: {e}"}))

            elif msg.get("type") == "interrupt":
                from jarvis.main import abort_all
                abort_all()

    except WebSocketDisconnect:
        if ws in _clients:
            _clients.remove(ws)


def start_web_background(port: int = 7860) -> None:
    """Launch web UI in a daemon thread. Non-blocking."""
    from jarvis.main import register_event_listener
    register_event_listener(_broadcast_to_ws)

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        # localhost only: the UI has no auth, exposes provider API keys and runs tools
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        loop.run_until_complete(server.serve())

    t = threading.Thread(target=_run, daemon=True)
    t.start()
