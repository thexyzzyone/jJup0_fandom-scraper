#!/usr/bin/env python3
"""Local web server to browse and search a scraped Fandom wiki."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading

import requests as http_requests
from flask import (
    Flask,
    Response,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
)

log = logging.getLogger("server")
# /static/ is routed by static_files() below, not Flask's built-in handler
app: Flask = Flask(__name__, static_folder=None)
SHARED_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_db_path: str | None = None
_wiki_name: str | None = None
_status_path: str | None = None
_wiki_static: str | None = None  # <hive>/<wiki>/static


def _scrape_status() -> str | None:
    """Return 'pages', 'images', or None (done/not scraping)."""
    if _status_path and os.path.exists(_status_path):
        with open(_status_path) as f:
            return f.read().strip() or None
    return None


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        assert _db_path is not None, "_db_path must be set before serving"
        g.db = sqlite3.connect(_db_path)
        g.db.row_factory = sqlite3.Row
    return g.db  # type: ignore[no-any-return]


@app.teardown_appcontext
def close_db(exc: BaseException | None) -> None:
    db = g.pop("db", None)
    if db:
        db.close()


def _search(db: sqlite3.Connection, q: str, limit: int = 100) -> list[sqlite3.Row]:
    """Search with prefix matching, title matches sorted first."""
    # Strip FTS5 syntax characters that cause OperationalError
    cleaned = re.sub(r"[{}()\:\"*]", " ", q)
    words = cleaned.split()
    if not words:
        return []
    fts_q = " ".join(w + "*" for w in words)
    rows = db.execute(
        """SELECT p.pageid, p.title,
                  snippet(pages_fts, 1, '<mark>', '</mark>', '...', 40) as snip
           FROM pages_fts JOIN pages p ON p.pageid = pages_fts.rowid
           WHERE pages_fts MATCH ? ORDER BY rank LIMIT ?""",
        (fts_q, limit),
    ).fetchall()
    ql = q.lower()
    return sorted(
        rows, key=lambda r: (ql not in r["title"].lower(), r["title"].lower() != ql)
    )


@app.route("/")
def index() -> str:
    db = get_db()
    q = request.args.get("q", "").strip()
    if q:
        rows = _search(db, q)
        return render_template(
            "index.html",
            pages=rows,
            query=q,
            search=True,
            wiki_name=_wiki_name,
            scraping=_scrape_status() == "pages",
        )
    rows = db.execute("SELECT pageid, title FROM pages ORDER BY title").fetchall()
    return render_template(
        "index.html",
        pages=rows,
        query="",
        search=False,
        wiki_name=_wiki_name,
        scraping=_scrape_status() == "pages",
    )


def _remote_search(q: str, wiki: str, limit: int = 20) -> list[dict[str, str]]:
    """Search Fandom's MediaWiki API for pages matching q (#9)."""
    try:
        r = http_requests.get(
            f"https://{wiki}.fandom.com/api.php",
            params={
                "action": "opensearch",
                "search": q,
                "limit": str(limit),
                "format": "json",
            },
            headers={"User-Agent": "FandomWikiMirror/1.0 (search-proxy)"},
            timeout=5,
        )
        data = r.json()
        if len(data) >= 2:
            return [{"title": t, "snip": "(from Fandom)"} for t in data[1]]
    except Exception as e:
        log.debug("remote-search failed: %s", e)
    return []


@app.route("/api/search")
def api_search() -> Response:
    db = get_db()
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    local = _search(db, q, limit=20)
    results = [{"title": r["title"], "snip": r["snip"]} for r in local]
    # Merge remote results only while pages are being scraped (#9, #10)
    if _scrape_status() == "pages":
        wiki_slug: str = app.config.get("WIKI_SLUG", "")  # type: ignore[assignment]
        local_titles = {r["title"] for r in results}
        remote = _remote_search(q, wiki_slug)
        # Title matches from remote go first
        ql = q.lower()
        remote_new = [r for r in remote if r["title"] not in local_titles]
        remote_title = [r for r in remote_new if ql in r["title"].lower()]
        remote_other = [r for r in remote_new if ql not in r["title"].lower()]
        results = remote_title + results + remote_other
    return jsonify(results)


@app.route("/static/<path:filename>")
def static_files(filename: str) -> Response:
    """/static/<wiki>/... from the wiki's hive dir; anything else is shared."""
    prefix = f"{app.config.get('WIKI_SLUG', '')}/"
    if _wiki_static and filename.startswith(prefix):
        return send_from_directory(_wiki_static, filename[len(prefix) :])
    return send_from_directory(SHARED_STATIC, filename)


@app.route("/wiki/<path:title>")
def page(title: str) -> str | tuple[str, int] | Response:
    db = get_db()
    normalized = title.replace("_", " ")
    row = db.execute("SELECT * FROM pages WHERE title = ?", (normalized,)).fetchone()
    wiki_slug: str = app.config.get("WIKI_SLUG", "")  # type: ignore[assignment]
    # On-demand fetch if page not in DB (#8)
    if not row:
        log.info("page-proxy: fetching '%s' on demand", normalized)
        try:
            api_url = f"https://{wiki_slug}.fandom.com/api.php"
            r = http_requests.get(
                api_url,
                params={
                    "action": "parse",
                    "page": normalized,
                    "prop": "text|categories|images",
                    "disableeditsection": "true",
                    "format": "json",
                },
                headers={"User-Agent": "FandomWikiMirror/1.0 (page-proxy)"},
                timeout=15,
            )
            data = r.json()
            if "error" not in data:
                from scrape import rewrite_html, strip_text

                parsed = data["parse"]
                html = rewrite_html(parsed["text"]["*"], {})
                categories = [c["*"] for c in parsed.get("categories", [])]
                row = {
                    "title": parsed["title"],
                    "html": html,
                    "categories": json.dumps(categories),
                }
        except Exception as e:
            log.debug("page-proxy: failed '%s': %s", normalized, e)
    if not row:
        fandom_url = f"https://{wiki_slug}.fandom.com/wiki/{title}"
        return (
            f'<p>Page not found. <a href="{fandom_url}">View on Fandom</a></p>',
            404,
        )
    # Follow MediaWiki redirects
    if '<div class="redirectMsg">' in row["html"]:
        m = re.search(r'href="/wiki/([^"]+)"', row["html"])
        if m:
            return redirect("/wiki/" + m.group(1))
    categories_list: list[str] = json.loads(row["categories"])
    has_full_css: bool = app.config.get("HAS_FULL_CSS", True)  # type: ignore[assignment]
    fandom_url = f"https://{wiki_slug}.fandom.com/wiki/{title}"
    return render_template(
        "page.html",
        page=row,
        categories=categories_list,
        has_full_css=has_full_css,
        wiki_slug=wiki_slug,
        fandom_url=fandom_url,
    )


@app.route("/image-proxy/<wiki>/<path:filename>")
def image_proxy(wiki: str, filename: str) -> Response | tuple[str, int]:
    """Fetch missing image from remote, cache locally, and serve it (#5)."""
    # Only the wiki being served has a cache dir; this also keeps `wiki` from
    # steering the cache path anywhere else
    if wiki != app.config.get("WIKI_SLUG") or not _wiki_static:
        return "Unknown wiki", 404
    safe_name = filename.replace("/", "_").replace("\\", "_").replace(" ", "_")
    local_path = os.path.join(_wiki_static, "images", safe_name)
    if os.path.exists(local_path):
        return send_file(local_path)
    # Resolve URL via MediaWiki API
    api_url = f"https://{wiki}.fandom.com/api.php"
    try:
        r = http_requests.get(
            api_url,
            params={
                "action": "query",
                "titles": "File:" + filename,
                "prop": "imageinfo",
                "iiprop": "url",
                "format": "json",
            },
            headers={"User-Agent": "FandomWikiMirror/1.0 (image-proxy)"},
            timeout=10,
        )
        pages = r.json().get("query", {}).get("pages", {})
        url = None
        for page in pages.values():
            if "imageinfo" in page:
                url = page["imageinfo"][0]["url"]
                break
        if not url:
            log.debug("image-proxy: not found on remote: %s", filename)
            return "Image not found", 404
        log.debug("image-proxy: fetching %s from %s", filename, url)
        img_r = http_requests.get(url, timeout=30)
        img_r.raise_for_status()
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(img_r.content)
        log.debug("image-proxy: cached %s", safe_name)
        return send_file(local_path)
    except Exception as e:
        log.debug("image-proxy: failed %s: %s", filename, e)
        return "Image fetch failed", 502


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("wiki", help="Wiki name (e.g. spiritfarer)")
    p.add_argument(
        "--hive",
        default=None,
        help="Data root; the wiki is read from <hive>/<wiki>/ (default: hive/ next "
        "to this script)",
    )
    p.add_argument("--no-scrape", action="store_true", help="Skip scraping, just serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    args = p.parse_args()

    logging.basicConfig(
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
        level=getattr(logging, args.log_level),
    )

    # Imported after logging is configured: scrape sets up logging on import
    from scrape import DEFAULT_HIVE, hive_layout

    hive: str = args.hive or DEFAULT_HIVE
    layout = hive_layout(hive, args.wiki)
    _db_path = layout.db
    _status_path = layout.status
    _wiki_static = layout.static

    if not args.no_scrape:
        from scrape import init_wiki, verify_wiki_exists

        init_wiki(args.wiki)
        if not verify_wiki_exists():
            log.error("Wiki '%s' does not exist on Fandom.", args.wiki)
            sys.exit(1)

        def _scrape() -> None:
            cmd = [
                sys.executable,
                os.path.join(os.path.dirname(__file__), "scrape.py"),
                args.wiki,
                "--hive",
                hive,
            ]
            subprocess.run(cmd)

        # Exists before the first request, even while the scraper starts up
        os.makedirs(layout.root, exist_ok=True)
        log.info("Scraping %s in background...", args.wiki)
        threading.Thread(target=_scrape, daemon=True).start()

    _wiki_name = args.wiki.replace("-", " ").title()
    css_path = os.path.join(SHARED_STATIC, "fandom-all.css")
    has_full_css = os.path.exists(css_path) and os.path.getsize(css_path) > 5000
    if not has_full_css:
        log.warning("Full Fandom CSS not found — using fallback styles.")
        log.warning(
            "For best results, see README for browser CSS extraction instructions."
        )
    app.config["HAS_FULL_CSS"] = has_full_css
    app.config["WIKI_SLUG"] = args.wiki
    app.run(host=args.host, port=args.port, debug=True, use_reloader=False)
