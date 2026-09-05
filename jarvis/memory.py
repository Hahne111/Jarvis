from __future__ import annotations
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import chromadb
from sqlalchemy import create_engine, text

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def _resolve_path(raw: str) -> Path:
    """Resolve a path that may be relative (relative to the repo root = config dir)."""
    p = Path(raw)
    if p.is_absolute():
        return p
    return (_CONFIG_PATH.parent / raw).resolve()


class Memory:
    def __init__(
        self,
        db_path: str | None = None,
        chroma_path: str | None = None,
        top_k: int | None = None,
    ):
        import yaml

        _cfg_top_k = 3  # default if config not available
        if db_path is None or chroma_path is None:
            with open(_CONFIG_PATH) as f:
                cfg = yaml.safe_load(f)["memory"]
            db_path = db_path or str(_resolve_path(cfg["db_path"]))
            chroma_path = chroma_path or str(_resolve_path(cfg["chroma_path"]))
            _cfg_top_k = cfg.get("top_k", 3)
        else:
            # paths provided directly (e.g. from tests); try to read top_k from config
            try:
                with open(_CONFIG_PATH) as f:
                    cfg = yaml.safe_load(f)["memory"]
                _cfg_top_k = cfg.get("top_k", 3)
            except Exception:
                pass

        self._top_k: int = top_k if top_k is not None else _cfg_top_k

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(chroma_path).mkdir(parents=True, exist_ok=True)

        self._engine = create_engine(f"sqlite:///{db_path}")
        self._chroma = chromadb.PersistentClient(path=chroma_path)
        self._facts = self._chroma.get_or_create_collection("facts")
        self._setup_db()

    def _setup_db(self) -> None:
        with self._engine.connect() as conn:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS summaries "
                "(id INTEGER PRIMARY KEY, content TEXT, created_at TEXT)"
            ))
            conn.commit()

    def store_fact(self, fact: str) -> None:
        """Store a semantic fact. Identical facts are deduplicated by hash ID."""
        fact_id = hashlib.md5(fact.encode()).hexdigest()
        self._facts.upsert(ids=[fact_id], documents=[fact])

    def search_facts(self, query: str, top_k: int | None = None) -> list[str]:
        """Return semantically relevant facts for the query."""
        k = top_k if top_k is not None else self._top_k
        count = self._facts.count()
        if count == 0:
            return []
        results = self._facts.query(query_texts=[query], n_results=min(k, count))
        return [d for d in results.get("documents", [[]])[0] if d]

    def save_conversation_summary(self, summary: str) -> None:
        with self._engine.connect() as conn:
            conn.execute(
                text("INSERT INTO summaries (content, created_at) VALUES (:c, :t)"),
                {"c": summary, "t": datetime.now(timezone.utc).isoformat()}
            )
            conn.commit()

    def get_recent_summaries(self, n: int = 5) -> list[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT content FROM summaries ORDER BY id DESC LIMIT :n"),
                {"n": n}
            ).fetchall()
        return [r[0] for r in rows]

    def extract_and_store_facts(self, assistant_response: str, user_message: str) -> None:
        """Store user message as a fact when it contains personal info or preferences."""
        triggers = [
            "my name is", "i am", "i'm", "i work", "i like", "i hate",
            "i love", "i prefer", "i need", "i want", "i use", "i have",
            "i live", "i go to", "i study", "i play", "i own",
            "my favorite", "my email", "my phone", "my address",
            "my birthday", "my password", "my project", "my computer",
            "remember that", "remember this", "don't forget", "keep in mind",
            "call me", "i usually", "i always", "i never",
        ]
        if any(t in user_message.lower() for t in triggers):
            self.store_fact(user_message)
