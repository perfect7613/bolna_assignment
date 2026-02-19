import asyncio
import os
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import aiohttp
from aiohttp import web
import feedparser

Incident = dict
EventHandler = Callable[[Incident], None]


@dataclass
class StatusPage:
    name: str
    feed_url: str
    etag: str | None = None
    last_modified: str | None = None
    seen_entries: dict[str, str] = field(default_factory=dict)
    poll_interval: float = 60.0


MIN_POLL_INTERVAL = 30.0
MAX_POLL_INTERVAL = 300.0
BACKOFF_FACTOR = 1.5
MAX_CONCURRENT = 20


PAGES: list[StatusPage] = [
    StatusPage(name="OpenAI API", feed_url="https://status.openai.com/feed.atom"),
]


def default_handler(event: Incident) -> None:
    print(f"[{event['timestamp']}] Product: {event['page']} - {event['components']}")
    print(f"Status: {event['title']}")


def parse_components(html: str) -> list[str]:
    components: list[str] = []
    for m in re.finditer(r"<li>(.+?)</li>", html, re.DOTALL):
        comp = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        comp = re.sub(
            r"\s*\((?:Operational|Degraded Performance|Partial Outage|Major Outage|Under Maintenance)\)\s*$",
            "", comp,
        )
        if comp:
            components.append(comp)
    return components


def format_timestamp(ts_str: str) -> str:
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, AttributeError):
        return ts_str or "unknown time"


async def poll_feed(
    session: aiohttp.ClientSession,
    page: StatusPage,
    semaphore: asyncio.Semaphore,
    on_incident: EventHandler,
) -> bool:
    headers: dict[str, str] = {}
    if page.etag:
        headers["If-None-Match"] = page.etag
    if page.last_modified:
        headers["If-Modified-Since"] = page.last_modified

    async with semaphore:
        try:
            async with session.get(
                page.feed_url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 304:
                    return False
                if resp.status != 200:
                    print(f"[{page.name}] HTTP {resp.status}")
                    return False
                page.etag = resp.headers.get("ETag")
                page.last_modified = resp.headers.get("Last-Modified")
                body = await resp.text()
        except Exception as exc:
            print(f"[{page.name}] Error: {exc}")
            return False

    feed = feedparser.parse(body)
    found_new = False

    for entry in reversed(feed.entries):
        entry_id = entry.get("id", entry.get("link", ""))
        updated = entry.get("updated", "")
        if page.seen_entries.get(entry_id) == updated:
            continue

        page.seen_entries[entry_id] = updated
        found_new = True
        title = entry.get("title", "Unknown incident")
        content_html = entry.get("summary", entry.get("content", [{}])[0].get("value", ""))
        components = parse_components(content_html)
        ts = format_timestamp(updated)

        on_incident({
            "page": page.name,
            "timestamp": ts,
            "components": ", ".join(components) if components else "General",
            "title": title,
        })

    return found_new

async def watch_page(
    session: aiohttp.ClientSession,
    page: StatusPage,
    semaphore: asyncio.Semaphore,
    on_incident: EventHandler,
) -> None:
    while True:
        found = await poll_feed(session, page, semaphore, on_incident)
        if found:
            page.poll_interval = MIN_POLL_INTERVAL
        else:
            page.poll_interval = min(
                page.poll_interval * BACKOFF_FACTOR, MAX_POLL_INTERVAL,
            )
        await asyncio.sleep(page.poll_interval)


LOG: deque[str] = deque(maxlen=200)


def log_handler(event: Incident) -> None:
    line = (
        f"[{event['timestamp']}] Product: {event['page']} - {event['components']}\n"
        f"Status: {event['title']}"
    )
    LOG.append(line)
    print(line)


async def handle_index(_request: web.Request) -> web.Response:
    body = "\n\n".join(LOG) if LOG else "No incidents yet. Monitoring..."
    return web.Response(text=body)


async def monitor(
    pages: list[StatusPage] | None = None,
    on_incident: EventHandler = log_handler,
) -> None:
    pages = pages or PAGES
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT, limit_per_host=2)

    print(f"Monitoring {len(pages)} status page(s).\n")

    session = aiohttp.ClientSession(connector=connector)
    for page in pages:
        asyncio.create_task(watch_page(session, page, semaphore, on_incident))

    app = web.Application()
    app.router.add_get("/", handle_index)
    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"Serving on port {port}")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(monitor())
    except KeyboardInterrupt:
        print("\nStopped.")

