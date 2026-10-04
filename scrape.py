#!/usr/bin/env python3
"""Scrape any Fandom wiki via MediaWiki API into SQLite with FTS5."""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, NamedTuple, TypedDict

import requests

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S", level=logging.INFO
)
log = logging.getLogger("scrape")


class PageInfo(TypedDict):
    pageid: int
    title: str
    touched: str


class ParsedPage(TypedDict):
    html: str
    categories: list[str]
    images: list[str]


def parse_touched(s: str | None) -> datetime:
    """Parse a MediaWiki touched timestamp to a datetime."""
    if not s:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


SESSION: requests.Session = requests.Session()
RATE_LIMIT: float = 0.5  # seconds between requests
_wiki_name: str | None = None
_api_url: str | None = None


def _require_wiki_name() -> str:
    assert _wiki_name is not None, "init_wiki() must be called first"
    return _wiki_name


def _require_api_url() -> str:
    assert _api_url is not None, "init_wiki() must be called first"
    return _api_url


def init_wiki(name: str) -> None:
    global _wiki_name, _api_url
    _wiki_name = name
    _api_url = f"https://{name}.fandom.com/api.php"
    SESSION.headers["User-Agent"] = (
        f"FandomWikiMirror/1.0 ({name}; personal offline use; polite)"
    )


def verify_wiki_exists() -> bool:
    """Check that the wiki exists by making a lightweight API call."""
    try:
        r = SESSION.get(
            _require_api_url(),
            params={"action": "query", "meta": "siteinfo", "format": "json"},
        )
        return r.status_code == 200 and "query" in r.json()
    except Exception:
        return False


def api_get(params: dict[str, Any]) -> dict[str, Any]:
    params["format"] = "json"
    time.sleep(RATE_LIMIT)
    r = SESSION.get(_require_api_url(), params=params)
    r.raise_for_status()
    result: dict[str, Any] = r.json()
    return result


def get_all_pages() -> list[PageInfo]:
    """Return list of {pageid, title, touched} for all content pages (ns=0)."""
    pages: list[PageInfo] = []
    params: dict[str, Any] = {
        "action": "query",
        "list": "allpages",
        "aplimit": "500",
        "apnamespace": "0",
        "generator": "allpages",
        "gaplimit": "500",
        "gapnamespace": "0",
        "prop": "info",
    }
    while True:
        data = api_get(params)
        if "query" in data and "pages" in data["query"]:
            for p in data["query"]["pages"].values():
                pages.append(
                    PageInfo(
                        pageid=p["pageid"],
                        title=p["title"],
                        touched=p.get("touched", ""),
                    )
                )
        log.info("enumerated %d pages...", len(pages))
        if "continue" not in data:
            break
        params.update(data["continue"])
    log.info("enumerated %d pages total", len(pages))
    return pages


def get_parsed_page(title: str) -> ParsedPage | None:
    """Return parsed HTML and categories for a page."""
    data = api_get(
        {
            "action": "parse",
            "page": title,
            "prop": "text|categories|images",
            "disableeditsection": "true",
        }
    )
    if "error" in data:
        return None
    p = data["parse"]
    return ParsedPage(
        html=p["text"]["*"],
        categories=[c["*"] for c in p.get("categories", [])],
        images=[img for img in p.get("images", [])],
    )


def get_image_urls(filenames: list[str]) -> dict[str, str]:
    """Batch-resolve image filenames to URLs (up to 50 at a time)."""
    urls: dict[str, str] = {}
    for i in range(0, len(filenames), 50):
        batch = filenames[i : i + 50]
        titles = "|".join("File:" + f for f in batch)
        data = api_get(
            {
                "action": "query",
                "titles": titles,
                "prop": "imageinfo",
                "iiprop": "url",
            }
        )
        for page in data["query"]["pages"].values():
            if "imageinfo" in page:
                fname = page["title"].replace("File:", "", 1)
                urls[fname] = page["imageinfo"][0]["url"]
    return urls


def get_wikitext(pageids: list[int]) -> dict[int, str]:
    """Batch-fetch current wikitext by pageid (up to 50 at a time)."""
    texts: dict[int, str] = {}
    for i in range(0, len(pageids), 50):
        params: dict[str, Any] = {
            "action": "query",
            "pageids": "|".join(str(p) for p in pageids[i : i + 50]),
            "prop": "revisions",
            "rvprop": "content",
            "rvslots": "main",
        }
        while True:
            data = api_get(params)
            for p in data.get("query", {}).get("pages", {}).values():
                revs = p.get("revisions")
                if revs and "*" in revs[0]["slots"]["main"]:
                    texts[p["pageid"]] = revs[0]["slots"]["main"]["*"]
            if "continue" not in data:
                break
            params.update(data["continue"])
    return texts


def wikitext_stem(title: str) -> str:
    """The filesystem-safe title part of a `.mediawiki` filename."""
    safe = re.sub(
        r'[\\/:%*?"<>|\x00-\x1f]',
        lambda m: f"%{ord(m.group()):02X}",
        title.replace(" ", "_"),
    )
    # Leave room for the suffix within the usual 255-byte filename limit
    return safe.encode()[:200].decode(errors="ignore")


def wikitext_filename(pageid: int, title: str) -> str:
    """Filesystem-safe `<title>.<pageid>.mediawiki` name.

    The pageid keeps names unique on case-insensitive filesystems, where
    redirects like "Foo Bar" and "Foo bar" would otherwise collide.
    """
    return f"{wikitext_stem(title)}.{pageid}.mediawiki"


def get_page_images() -> set[str]:
    """Return filenames of every image used on a content page (ns=0)."""
    names: set[str] = set()
    params: dict[str, Any] = {
        "action": "query",
        "generator": "allpages",
        "gapnamespace": "0",
        "gaplimit": "500",
        "prop": "images",
        "imlimit": "max",
    }
    while True:
        data = api_get(params)
        for p in data.get("query", {}).get("pages", {}).values():
            for img in p.get("images", []):
                # "File:Foo bar.png" -> "Foo bar.png" (namespace name varies by language)
                names.add(img["title"].split(":", 1)[1])
        if "continue" not in data:
            break
        params.update(data["continue"])
    log.info("found %d images/files used on content pages", len(names))
    return names


DEFAULT_HIVE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hive")


class HiveLayout(NamedTuple):
    """Where everything for one wiki lives: <hive>/<wiki>/... (see hive/AGENTS.md)."""

    root: str
    db: str
    status: str
    static: str
    images: str
    theme: str
    mediawiki: str


def hive_layout(hive: str, wiki: str) -> HiveLayout:
    root = os.path.join(hive, wiki)
    static = os.path.join(root, "static")
    return HiveLayout(
        root=root,
        db=os.path.join(root, f"{wiki}.db"),
        status=os.path.join(root, f".{wiki}.status"),
        static=static,
        images=os.path.join(static, "images"),
        theme=os.path.join(static, "theme.css"),
        mediawiki=os.path.join(root, "mediawiki"),
    )


_images_dir: str | None = None
# Which wiki uploads to download. "Images" are picked out by extension; every
# other upload (audio, video, fonts, ...) is a "file".
IMAGE_EXTENSIONS = frozenset(
    "apng avif bmp gif ico jpeg jpg png svg tif tiff webp".split()
)
_download_images: bool = True
_download_files: bool = True
_permitted_file_types: list[str] | None = None  # lowercase globs; None = all


def _require_images_dir() -> str:
    assert _images_dir is not None, "_images_dir must be set first"
    return _images_dir


def file_wanted(filename: str) -> bool:
    """Whether --no-images / --prohibit-files / --permit-file-types allow it."""
    name = filename.replace(" ", "_").lower()
    if os.path.splitext(name)[1].lstrip(".") in IMAGE_EXTENSIONS:
        return _download_images
    if not _download_files:
        return False
    if _permitted_file_types is None:
        return True
    return any(fnmatch.fnmatchcase(name, pat) for pat in _permitted_file_types)


def download_image(url: str, filename: str) -> str:
    """Download image into the wiki's images dir, return its local name."""
    safe_name = filename.replace("/", "_").replace("\\", "_").replace(" ", "_")
    local_path = os.path.join(_require_images_dir(), safe_name)
    if os.path.exists(local_path):
        return safe_name
    log.info("downloading %s", filename)
    time.sleep(RATE_LIMIT)
    r = SESSION.get(url, stream=True)
    r.raise_for_status()
    with open(local_path, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)
    return safe_name


def rewrite_html(html: str, image_map: dict[str, str]) -> str:
    """Replace fandom image/link URLs with local paths."""
    for orig_url, local_name in image_map.items():
        html = html.replace(
            orig_url, f"/static/{_require_wiki_name()}/images/{local_name}"
        )
    # Rewrite data-src (lazy loaded images on fandom)
    html = re.sub(r' data-src="([^"]*)"', lambda m: f' src="{m.group(1)}"', html)
    # Rewrite internal wiki links to local routes
    html = re.sub(
        r'href="https://[^"]*\.fandom\.com/wiki/([^"]*)"', r'href="/wiki/\1"', html
    )
    return html


def strip_text(html: str) -> str:
    """Rough plaintext extraction for FTS indexing."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS pages (
            pageid INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            html TEXT NOT NULL,
            plaintext TEXT NOT NULL,
            categories TEXT NOT NULL DEFAULT '[]',
            touched TEXT NOT NULL DEFAULT ''
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
            title, plaintext, content=pages, content_rowid=pageid
        );
        CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
            INSERT INTO pages_fts(rowid, title, plaintext) VALUES (new.pageid, new.title, new.plaintext);
        END;
        CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, title, plaintext) VALUES('delete', old.pageid, old.title, old.plaintext);
        END;
        CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, title, plaintext) VALUES('delete', old.pageid, old.title, old.plaintext);
            INSERT INTO pages_fts(rowid, title, plaintext) VALUES (new.pageid, new.title, new.plaintext);
        END;
        CREATE TABLE IF NOT EXISTS wikitext_pages (
            pageid INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            touched TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS wikitext_text (
            pageid INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            plaintext TEXT NOT NULL,
            filename TEXT NOT NULL,
            mtime_ns INTEGER NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS wikitext_fts USING fts5(
            title, plaintext, content=wikitext_text, content_rowid=pageid
        );
        CREATE TRIGGER IF NOT EXISTS wikitext_ai AFTER INSERT ON wikitext_text BEGIN
            INSERT INTO wikitext_fts(rowid, title, plaintext) VALUES (new.pageid, new.title, new.plaintext);
        END;
        CREATE TRIGGER IF NOT EXISTS wikitext_ad AFTER DELETE ON wikitext_text BEGIN
            INSERT INTO wikitext_fts(wikitext_fts, rowid, title, plaintext) VALUES('delete', old.pageid, old.title, old.plaintext);
        END;
        CREATE TRIGGER IF NOT EXISTS wikitext_au AFTER UPDATE ON wikitext_text BEGIN
            INSERT INTO wikitext_fts(wikitext_fts, rowid, title, plaintext) VALUES('delete', old.pageid, old.title, old.plaintext);
            INSERT INTO wikitext_fts(rowid, title, plaintext) VALUES (new.pageid, new.title, new.plaintext);
        END;
    """)
    conn.commit()
    return conn


FTS_HTML = "html"
FTS_MEDIAWIKI = "mediawiki"


def get_fts_mode(conn: sqlite3.Connection) -> str:
    """Which text the database's search index holds: FTS_HTML or FTS_MEDIAWIKI."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'fts'").fetchone()
    except sqlite3.OperationalError:  # database from before --fts
        return FTS_HTML
    return str(row[0]) if row else FTS_HTML


def fts_tables(conn: sqlite3.Connection) -> tuple[str, str]:
    """(FTS table, its content table) for the database's search index."""
    if get_fts_mode(conn) == FTS_MEDIAWIKI:
        return "wikitext_fts", "wikitext_text"
    return "pages_fts", "pages"


def set_fts_mode(conn: sqlite3.Connection, mode: str) -> None:
    """Record the index mode and keep only that index's text.

    FTS_MEDIAWIKI empties `pages.plaintext` (HTML text isn't searched);
    FTS_HTML refills it from the stored HTML and empties the wikitext index.
    Does nothing if `mode` is already the recorded one.
    """
    recorded = conn.execute("SELECT value FROM meta WHERE key = 'fts'").fetchone()
    if recorded is not None and recorded[0] == mode:
        return
    log.info("search index: switching to %s", mode)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('fts', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (mode,),
    )
    if mode == FTS_MEDIAWIKI:
        conn.execute("UPDATE pages SET plaintext = '' WHERE plaintext != ''")
    else:
        conn.execute("DELETE FROM wikitext_text")
        rows = conn.execute(
            "SELECT pageid, html FROM pages WHERE plaintext = '' AND html != ''"
        ).fetchall()
        conn.executemany(
            "UPDATE pages SET plaintext = ? WHERE pageid = ?",
            [(strip_text(html), pageid) for pageid, html in rows],
        )
    # Drop entries left behind by older versions, which replaced rows
    # without removing their old text from the index
    conn.execute("INSERT INTO pages_fts(pages_fts) VALUES ('rebuild')")
    conn.commit()


def wikitext_to_text(text: str) -> str:
    """Rough plaintext of wikitext for search: markup removed, values kept.

    Template names and parameters stay as words (an infobox's
    `species = Owl` becomes "species Owl"), so they're searchable too.
    """
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    # Table/tag formatting like align="center" or style="width:5em" (pages
    # often leave the quote unclosed, so a | or line break also ends it)
    text = re.sub(r'\b[\w-]+\s*=\s*"[^"|\n]*"?', " ", text)
    text = re.sub(r"\|-+", " ", text)  # table row separators
    # [[File:x.png|thumb|Caption]] -> Caption; [[Page|label]] -> label
    text = re.sub(r"\[\[(?:[^\]|]*\|)*([^\]|]*)\]\]", lambda m: f" {m.group(1)} ", text)
    text = re.sub(r"\[https?://\S+\s*([^\]]*)\]", r" \1 ", text)  # [url label]
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"__[A-Z]+__", " ", text)  # __TOC__, __NOTOC__
    text = re.sub(r"'{2,}|={2,}|[{}|=\[\]]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_wikitext_filename(name: str) -> tuple[str, int] | None:
    """(title, pageid) from a `<Title>.<pageid>.mediawiki` filename."""
    m = re.fullmatch(r"(.*)\.(\d+)\.mediawiki", name)
    if not m:
        return None
    return urllib.parse.unquote(m.group(1)).replace("_", " "), int(m.group(2))


def index_wikitext(conn: sqlite3.Connection, mediawiki_dir: str) -> None:
    """Sync the wikitext search index with the .mediawiki files on disk.

    Files that are new or changed since they were indexed are (re)read;
    index entries whose file is gone are dropped.
    """
    indexed: dict[int, tuple[str, int]] = {
        r[0]: (r[1], r[2])
        for r in conn.execute("SELECT pageid, filename, mtime_ns FROM wikitext_text")
    }
    on_disk: dict[int, tuple[str, int, str]] = {}
    names = os.listdir(mediawiki_dir) if os.path.isdir(mediawiki_dir) else []
    for name in names:
        parsed = parse_wikitext_filename(name)
        if parsed is None:
            continue
        title, pageid = parsed
        mtime = os.stat(os.path.join(mediawiki_dir, name)).st_mtime_ns
        if pageid not in on_disk or mtime > on_disk[pageid][1]:
            on_disk[pageid] = (name, mtime, title)

    changed = [
        pid
        for pid, (name, mtime, _) in on_disk.items()
        if indexed.get(pid) != (name, mtime)
    ]
    for pageid in changed:
        name, mtime, title = on_disk[pageid]
        with open(os.path.join(mediawiki_dir, name), encoding="utf-8") as f:
            plaintext = wikitext_to_text(f.read())
        conn.execute(
            "INSERT INTO wikitext_text (pageid, title, plaintext, filename, mtime_ns) "
            "VALUES (?,?,?,?,?) ON CONFLICT(pageid) DO UPDATE SET title = "
            "excluded.title, plaintext = excluded.plaintext, filename = "
            "excluded.filename, mtime_ns = excluded.mtime_ns",
            (pageid, title, plaintext, name, mtime),
        )
    gone = indexed.keys() - on_disk.keys()
    conn.executemany(
        "DELETE FROM wikitext_text WHERE pageid = ?", [(pid,) for pid in gone]
    )
    conn.commit()
    log.info(
        "wikitext index: %d pages, %d updated, %d removed",
        len(on_disk),
        len(changed),
        len(gone),
    )


def find_stale(pages: list[PageInfo], existing: dict[int, str]) -> list[PageInfo]:
    """Return pages missing from `existing` (pageid -> touched), then newer ones."""
    new = [p for p in pages if p["pageid"] not in existing]
    updated = [
        p
        for p in pages
        if p["pageid"] in existing
        and parse_touched(p["touched"]) > parse_touched(existing[p["pageid"]])
    ]
    log.info(
        "Found %d pages, %d already stored, %d new, %d updated",
        len(pages),
        len(existing),
        len(new),
        len(updated),
    )
    if updated[:3]:
        for p in updated[:3]:
            log.info(
                "  e.g. %s: db=%r api=%r",
                p["title"],
                existing[p["pageid"]],
                p["touched"],
            )
    return new + updated  # new pages first


WIKITEXT_MANIFEST = "_index.json"


def scrape_wikitext(
    pages: list[PageInfo], out_dir: str, tracking: str, db_path: str
) -> None:
    """Save raw wikitext of new/changed pages to `<out_dir>/*.mediawiki`.

    `tracking` picks where each page's last-seen `touched` is kept so later
    runs only fetch new/changed pages: "db" (wikitext_pages table in db_path),
    "manifest" (<out_dir>/_index.json) or "none" (refetch everything).
    """
    os.makedirs(out_dir, exist_ok=True)
    conn = init_db(db_path) if tracking == "db" else None
    manifest_path = os.path.join(out_dir, WIKITEXT_MANIFEST)
    manifest: dict[str, dict[str, str]] = {}
    existing: dict[int, str] = {}
    if conn:
        existing = {
            r[0]: r[1]
            for r in conn.execute("SELECT pageid, touched FROM wikitext_pages")
        }
    elif tracking == "manifest" and os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        existing = {int(k): v["touched"] for k, v in manifest.items()}

    # pageid -> files already on disk, recovered from the `.<pageid>.mediawiki` suffix
    on_disk: dict[int, list[str]] = {}
    for name in os.listdir(out_dir):
        m = re.search(r"\.(\d+)\.mediawiki$", name)
        if m:
            on_disk.setdefault(int(m.group(1)), []).append(name)

    stale = find_stale(pages, existing)
    for i in range(0, len(stale), 50):
        batch = stale[i : i + 50]
        texts = get_wikitext([p["pageid"] for p in batch])
        saved: list[PageInfo] = []
        for page in batch:
            text = texts.get(page["pageid"])
            if text is None:
                log.warning("SKIP (no wikitext): %s", page["title"])
                continue
            filename = wikitext_filename(page["pageid"], page["title"])
            with open(os.path.join(out_dir, filename), "w", encoding="utf-8") as f:
                f.write(text)
            # Page was renamed: drop the file saved under its old title
            for old in on_disk.get(page["pageid"], []):
                if old != filename:
                    os.remove(os.path.join(out_dir, old))
            on_disk[page["pageid"]] = [filename]
            saved.append(page)

        if conn:
            conn.executemany(
                "INSERT OR REPLACE INTO wikitext_pages (pageid, title, touched) VALUES (?,?,?)",
                [(p["pageid"], p["title"], p["touched"]) for p in saved],
            )
            conn.commit()
        elif tracking == "manifest":
            for p in saved:
                manifest[str(p["pageid"])] = {
                    "title": p["title"],
                    "touched": p["touched"],
                }
            # Write-then-rename so a crash never leaves a truncated manifest
            with open(manifest_path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=1, sort_keys=True)
            os.replace(manifest_path + ".tmp", manifest_path)
        log.info("  %d/%d wikitext pages saved", min(i + 50, len(stale)), len(stale))

    if conn:
        conn.close()


def download_missing_images(filenames: set[str], img_dir: str) -> dict[str, str]:
    """Download images not yet in img_dir; return {remote url: local name}."""
    wanted = {f for f in filenames if file_wanted(f)}
    if len(wanted) < len(filenames):
        log.info(
            "Skipping %d images/files excluded by the download flags",
            len(filenames) - len(wanted),
        )
    filenames = wanted
    local_files = set(os.listdir(img_dir))
    needed = [
        f
        for f in filenames
        if f.replace("/", "_").replace("\\", "_").replace(" ", "_") not in local_files
    ]
    image_map: dict[str, str] = {}
    if not needed:
        return image_map
    log.info(
        "Downloading %d missing images (%d already local)...",
        len(needed),
        len(filenames) - len(needed),
    )
    for i in range(0, len(needed), 50):
        batch = needed[i : i + 50]
        batch_urls = get_image_urls(batch)
        for fname, url in batch_urls.items():
            try:
                local = download_image(url, fname)
                image_map[url] = local
            except Exception as e:
                log.warning("FAILED %s: %s", fname, e)
        log.info("  %d/%d images done", min(i + 50, len(needed)), len(needed))
    return image_map


def scrape_html(
    pages: list[PageInfo],
    db_path: str,
    img_dir: str,
    with_media: bool,
    index_html: bool = True,
) -> None:
    """Store rendered HTML of new/changed pages in the DB, plus their media.

    `with_media` turns image/file downloads on at all; file_wanted() then
    decides each one. Without `index_html` (--fts=mediawiki) the HTML's text
    isn't stored for search.
    """

    def plaintext(html: str) -> str:
        return strip_text(html) if index_html else ""

    conn = init_db(db_path)
    c = conn.cursor()

    # Signal: scraping pages (#10)
    status_path = os.path.join(
        os.path.dirname(db_path), f".{_require_wiki_name()}.status"
    )
    with open(status_path, "w") as f:
        f.write("pages")

    # Track what we already have
    existing: dict[int, str] = {
        r[0]: r[1] for r in c.execute("SELECT pageid, touched FROM pages").fetchall()
    }

    stale = find_stale(pages, existing)

    local_files: set[str] = set(os.listdir(img_dir)) if with_media else set()

    for i, page in enumerate(stale):
        log.info("[%d/%d] %s", i + 1, len(stale), page["title"])
        parsed = get_parsed_page(page["title"])
        if not parsed:
            log.warning("SKIP (error): %s", page["title"])
            continue

        # Download this page's images immediately (#6)
        needed = [
            f
            for f in parsed["images"]
            if f.replace("/", "_").replace("\\", "_").replace(" ", "_")
            not in local_files
            and file_wanted(f)
        ]
        if needed and with_media:
            image_urls = get_image_urls(needed)
            image_map: dict[str, str] = {}
            for fname, url in image_urls.items():
                try:
                    local = download_image(url, fname)
                    image_map[url] = local
                    local_files.add(local)
                except Exception as e:
                    log.warning("FAILED %s: %s", fname, e)
            # Rewrite HTML with local image paths immediately (#4)
            html = rewrite_html(parsed["html"], image_map)
        else:
            html = rewrite_html(parsed["html"], {})

        # An upsert, not INSERT OR REPLACE: REPLACE skips the delete trigger,
        # which would leave the old text in the search index
        c.execute(
            "INSERT INTO pages (pageid, title, html, plaintext, categories, touched) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(pageid) DO UPDATE SET title = "
            "excluded.title, html = excluded.html, plaintext = excluded.plaintext, "
            "categories = excluded.categories, touched = excluded.touched",
            (
                page["pageid"],
                page["title"],
                html,
                plaintext(html),
                json.dumps(parsed["categories"]),
                page["touched"],
            ),
        )
        if (i + 1) % 20 == 0:
            conn.commit()
            log.info("committed %d pages", i + 1)

    conn.commit()

    if with_media:
        # Signal: pages done, images phase starting (#10)
        with open(status_path, "w") as f:
            f.write("images")

        # Catch up: download missing images for already-stored pages
        all_image_filenames: set[str] = set()
        for row in c.execute("SELECT html FROM pages"):
            for m in re.findall(r'data-image-key="([^"]+)"', row[0]):
                all_image_filenames.add(urllib.parse.unquote(m))

        image_map_catchup = download_missing_images(all_image_filenames, img_dir)

        # Rewrite HTML in stored pages to use local images
        if image_map_catchup:
            log.info("Rewriting image URLs in stored pages...")
            for row in c.execute("SELECT pageid, html FROM pages").fetchall():
                new_html = rewrite_html(row[1], image_map_catchup)
                if new_html != row[1]:
                    c.execute(
                        "UPDATE pages SET html=?, plaintext=? WHERE pageid=?",
                        (new_html, plaintext(new_html), row[0]),
                    )
            conn.commit()

    conn.close()
    # Signal: all done (#10)
    if os.path.exists(status_path):
        os.remove(status_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape a Fandom wiki into SQLite")
    parser.add_argument(
        "wiki", help="Wiki subdomain (e.g. spiritfarer, hollowknight, stardewvalley)"
    )
    parser.add_argument(
        "--hive",
        default=DEFAULT_HIVE,
        help="Output root; everything goes in <hive>/<wiki>/ (default: hive/ next "
        "to this script)",
    )
    parser.add_argument(
        "--with-mediawiki",
        action="store_true",
        help="Also save raw wikitext to <hive>/<wiki>/mediawiki/<Title>.<pageid>.mediawiki",
    )
    parser.add_argument(
        "--fts",
        choices=[FTS_HTML, FTS_MEDIAWIKI],
        default=FTS_HTML,
        help="What the search index is built from: html = the rendered pages' "
        "text (default); mediawiki = only the wikitext files (implies "
        "--with-mediawiki; HTML text is not indexed even if stored)",
    )
    parser.add_argument(
        "--mediawiki-tracking",
        choices=["db", "manifest", "none"],
        default=None,
        help="With --with-mediawiki: where to remember each page's last-seen "
        "revision so reruns only fetch changes. db = table in <wiki>.db "
        "(default), manifest = mediawiki/_index.json, "
        "none = refetch every page every run",
    )
    parser.add_argument(
        "--no-html",
        action="store_true",
        help="Don't store rendered HTML (the result can't be served by server.py)",
    )
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="Don't download images (png, jpg, gif, webp, svg, ...)",
    )
    files = parser.add_mutually_exclusive_group()
    files.add_argument(
        "--prohibit-files",
        action="store_true",
        help="Don't download non-image files (audio, video, fonts, ...)",
    )
    files.add_argument(
        "--permit-file-types",
        metavar="GLOBS",
        default=None,
        help="Only download non-image files whose names match one of these "
        "comma-separated, case-insensitive globs, e.g. '*.ogg,*.m4v'. "
        "An empty list is the same as --prohibit-files",
    )
    parser.add_argument(
        "--no-style",
        action="store_true",
        help="Don't download theme CSS or other styling files",
    )
    args = parser.parse_args()
    with_html: bool = not args.no_html
    permitted: list[str] | None = None
    if args.permit_file_types is not None:
        permitted = [
            p.strip().lower() for p in args.permit_file_types.split(",") if p.strip()
        ]
    with_images: bool = not args.no_images
    with_files: bool = not (args.prohibit_files or permitted == [])
    with_media: bool = with_images or with_files
    with_style: bool = not args.no_style
    # The wikitext index needs the wikitext
    with_mediawiki: bool = args.with_mediawiki or args.fts == FTS_MEDIAWIKI
    if args.mediawiki_tracking and not with_mediawiki:
        parser.error("--mediawiki-tracking requires --with-mediawiki")
    if not (with_html or with_media or with_style or with_mediawiki):
        parser.error("nothing to scrape: every kind of content is disabled")

    init_wiki(args.wiki)
    layout = hive_layout(args.hive, args.wiki)
    global _images_dir, _download_images, _download_files, _permitted_file_types
    _images_dir = layout.images
    _download_images = with_images
    _download_files = with_files
    _permitted_file_types = permitted

    log.info("Verifying wiki '%s' exists...", args.wiki)
    if not verify_wiki_exists():
        log.error("Wiki '%s' does not exist on Fandom. Aborting.", args.wiki)
        sys.exit(1)

    os.makedirs(layout.root, exist_ok=True)
    if with_media:
        os.makedirs(layout.images, exist_ok=True)

    if with_style:
        # Download theme variables (not behind Cloudflare)
        theme_url = f"https://{args.wiki}.fandom.com/wikia.php?controller=ThemeApi&method=themeVariables"
        log.info("Downloading theme variables from %s...", theme_url)
        theme_css = SESSION.get(theme_url).text
        os.makedirs(layout.static, exist_ok=True)
        with open(layout.theme, "w") as f:
            f.write(theme_css)
        log.info("Saved to %s", layout.theme)

    pages = get_all_pages() if with_html or with_mediawiki else []

    # Record the index mode first, so the web UI searches the right index
    # while the scrape runs; an HTML-only hive with no database yet gets
    # one from scrape_html()
    if args.fts == FTS_MEDIAWIKI or with_html or os.path.exists(layout.db):
        conn = init_db(layout.db)
        set_fts_mode(conn, args.fts)
        conn.close()

    # Wikitext first: 50 pages per request, so it's done long before the HTML
    if with_mediawiki:
        scrape_wikitext(
            pages, layout.mediawiki, args.mediawiki_tracking or "db", layout.db
        )
    if args.fts == FTS_MEDIAWIKI:
        conn = init_db(layout.db)
        index_wikitext(conn, layout.mediawiki)
        conn.close()

    if with_html:
        scrape_html(pages, layout.db, layout.images, with_media, args.fts == FTS_HTML)
    elif with_media:
        # No HTML to read image names from, so ask the API what pages use
        download_missing_images(get_page_images(), layout.images)

    log.info("Done!")


if __name__ == "__main__":
    main()
