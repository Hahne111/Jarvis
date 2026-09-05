import html
import re
import urllib.request

from ddgs import DDGS


def search(query: str, max_results: int = 5) -> str:
    """Search the web via DuckDuckGo and return a formatted text summary."""
    try:
        results = DDGS().text(query, max_results=max_results)
        if not results:
            return "No results found."
        return "\n".join(f"- {r['title']}: {r['body']}" for r in results)
    except Exception as e:
        return f"Search failed: {e}"


def get_weather(location: str = "") -> str:
    """Get current weather using wttr.in (no API key needed)."""
    try:
        loc = location.replace(" ", "+") if location else ""
        url = f"https://wttr.in/{loc}?format=%l:+%C+%t+%h+%w"
        req = urllib.request.Request(url, headers={"User-Agent": "curl/7.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.read().decode("utf-8").strip()
    except Exception as e:
        return f"Weather unavailable: {e}"


def fetch_page(url: str, max_chars: int = 3000) -> str:
    """Fetch a URL and return its plain text content."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        raw = re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=re.DOTALL)
        raw = re.sub(r"<style[^>]*>.*?</style>", "", raw, flags=re.DOTALL)
        raw = re.sub(r"<[^>]+>", " ", raw)
        raw = html.unescape(raw)
        raw = re.sub(r"\s+", " ", raw).strip()
        return raw[:max_chars]
    except Exception as e:
        return f"Failed to fetch {url}: {e}"
