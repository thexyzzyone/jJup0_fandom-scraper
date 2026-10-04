# Fandom Wiki Mirror

A local, searchable mirror for **any** [Fandom](https://www.fandom.com/) wiki with full-text search, local images, and faithful visual styling.

> ⚠️ **Disclaimer**: This project was primarily written by Claude Opus 4.6 (Anthropic) with human guidance. While it works, the code has not been thoroughly audited. Use at your own risk — review the scraping behavior before running against any wiki, and be respectful of Fandom's servers.

## Why

Scrapes an entire wiki via the MediaWiki API, stores it in SQLite with FTS5 full-text search, and serves it locally — looking almost identical to the original.

## Quick Start

```bash
pip install -r requirements.txt

# Scrape and serve any Fandom wiki (use the subdomain name)
python server.py spiritfarer
# Open http://localhost:5000
```

That's it. The server will scrape the wiki on first run (and update on subsequent runs), then start serving.

You can also scrape and serve separately:

```bash
# Scrape only
python scrape.py spiritfarer

# Serve only (skip scraping)
python server.py spiritfarer --no-scrape
```

All output goes in `hive/`, one folder per wiki named after its subdomain:

```
hive/
  AGENTS.md                Layout guide for AI agents (committed)
  spiritfarer/
    spiritfarer.db         Pages + full-text index
    mediawiki/             Raw wikitext (--with-mediawiki)
    static/
      theme.css            Per-wiki colors and fonts
      images/              Images and other uploads
```

`hive/` is committed empty apart from its `.gitignore` and [`hive/AGENTS.md`](hive/AGENTS.md), which explains the layout, database schema and useful queries so an AI agent can use a scraped wiki as a knowledge base. Pass `--hive <dir>` to `scrape.py` or `server.py` to put it somewhere else.

Upgrading from the old layout (`<wiki>.db` in the repo root, `static/<wiki>/`): move `<wiki>.db` and any `.<wiki>.status` to `hive/<wiki>/`, and `static/<wiki>/` to `hive/<wiki>/static/`. Page links and image URLs in the database don't change.

### Choosing what to scrape

By default the scraper stores rendered HTML (with its search index), downloads every image and other file used on article pages, and downloads the wiki's theme CSS. These flags change that, and any combination is allowed except `--prohibit-files` with `--permit-file-types`:

| Flag | Effect |
|---|---|
| `--with-mediawiki` | Also save each page's raw wikitext to `hive/<wiki>/mediawiki/<Title>.<pageid>.mediawiki` |
| `--no-html` | Don't store rendered HTML. `server.py` has nothing to serve from this database. |
| `--no-images` | Don't download images. Stored HTML keeps pointing at Fandom's servers, so images only load while you're online. |
| `--prohibit-files` | Don't download non-image files (audio, video, fonts, documents, ...) |
| `--permit-file-types='*.ogg,*.m4v'` | Only download non-image files whose names match one of these comma-separated globs. An empty list is the same as `--prohibit-files`. |
| `--no-style` | Don't download theme CSS (`hive/<wiki>/static/theme.css`) |
| `--fts=html` / `--fts=mediawiki` | What the search index is built from. `html` (the default): the rendered pages' text. `mediawiki`: only the wikitext files, even if HTML is also stored; turns on `--with-mediawiki` by itself. |

```bash
# Everything, plus wikitext files
python scrape.py spiritfarer --with-mediawiki

# Searchable text with no media
python scrape.py spiritfarer --no-images --prohibit-files --no-style

# Images plus OGG audio, nothing else
python scrape.py spiritfarer --permit-file-types='*.ogg'

# Wikitext files only
python scrape.py spiritfarer --with-mediawiki --no-html --no-images --prohibit-files --no-style
```

Turning everything off is an error. A later run with fewer restrictions downloads whatever was skipped and switches stored pages to the local copies. With `--no-html`, images and files are still downloaded unless their flags say otherwise: the scraper asks the API for every upload used on an article page, which is the same set a normal scrape downloads.

#### Images and files

Wikis upload more than pictures: audio (e.g. `.wav`, `.ogg`), video (`.mp4`), fonts and documents all appear on article pages. An upload counts as an **image** if its extension is one of `apng avif bmp gif ico jpeg jpg png svg tif tiff webp`; everything else, including extensionless entries such as embedded YouTube videos, is a **file**. `--no-images` controls images only; `--prohibit-files` and `--permit-file-types` control files only, so `--permit-file-types` can't exclude images.

`--permit-file-types` globs use `*` and `?`, are case-insensitive, and are matched against the saved filename (spaces become underscores), so `--permit-file-types='*.ogg,Albert_*'` also works. Quote the list: zsh otherwise tries to expand the `*` itself and fails with "no matches found". Files already downloaded by an earlier run are kept even if a later run excludes them.

#### Wikitext files

Each `.mediawiki` file is the page's source exactly as stored on the wiki. Filenames end in the pageid so pages whose titles differ only by case (common with redirects) don't overwrite each other on case-insensitive filesystems. Characters that aren't safe in filenames (`/ \ : % * ? " < > |`) are percent-encoded, and spaces become underscores. When a page is renamed on the wiki, its old file is deleted.

`--mediawiki-tracking` sets where the scraper remembers which wikitext it has already saved, so reruns only fetch new or changed pages:

| `--mediawiki-tracking` | Bookkeeping | Reruns |
|---|---|---|
| `db` (default) | `wikitext_pages` table in `<wiki>.db` (pageid, title, timestamp; never the page text) | Fetch only new or changed pages |
| `manifest` | `hive/<wiki>/mediawiki/_index.json` | Fetch only new or changed pages |
| `none` | Nothing | Fetch every page again |

With `--no-html`, a `manifest` or `none` run creates no database at all (unless `--fts=mediawiki` needs one for its index). Wikitext is fetched 50 pages per request, so even `none` is fast compared with an HTML scrape.

#### Search index from wikitext

`--fts=mediawiki` builds the search index from the `.mediawiki` files instead of the HTML: markup is stripped, but template fields stay as words, so an infobox's `species = Owl` is searchable. Combined with `--no-html` the scraper never requests a rendered page, so a whole wiki takes a few requests per 50 pages (Spiritfarer's 864 pages index in about 15 seconds) and the database stays small. What you give up is text that only exists once Fandom expands templates, such as navboxes and generated lists, and the web UI if there's no HTML.

The index tracks the files on disk: each run re-reads only files that changed and drops entries whose file is gone. The chosen mode is recorded in the database, and `server.py` and `mcp_serve.py` search whichever index it names. Switching back to `--fts=html` refills the HTML index from the stored HTML without re-downloading anything.

### Optional: Full Fandom CSS (best visual fidelity)

The scraper auto-downloads per-wiki theme variables (colors, fonts, background), but Fandom's full layout CSS is behind Cloudflare and can't be fetched programmatically. For pixel-perfect styling, extract it once from your browser:

1. Open any page on any Fandom wiki (e.g. `https://spiritfarer.fandom.com/wiki/Air_Draft`)
2. Open browser console (F12 → Console)
3. Paste and run:

```js
await Promise.all(
  [...document.querySelectorAll('link[rel="stylesheet"]')].map(async l => {
    const r = await fetch(l.href);
    return {href: l.href, css: await r.text()};
  })
).then(sheets => {
  const blob = new Blob([sheets.map(s => `/* ${s.href} */\n${s.css}`).join('\n\n')], {type:'text/css'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
  a.download = 'fandom-all.css'; a.click();
});
```

1. Move the downloaded file to `static/fandom-all.css`

This only needs to be done once — the CSS is shared across all wikis. Per-wiki theming comes from `hive/<wiki>/static/theme.css`, which the scraper downloads automatically.

Without this step, the built-in fallback CSS handles infoboxes, tables, tabs, and galleries — just not pixel-perfect. The server will show a warning banner when the full CSS is missing.

## Architecture

```
scrape.py                  MediaWiki API scraper → SQLite + local images
server.py                  Flask web server with FTS5 search
mcp_serve.py               MCP server over the hive for AI agents (needs .env, see below)
.env.example               Settings template for mcp_serve.py
static/
  fandom-all.css           Fandom's layout CSS (extracted from browser, shared across wikis)
hive/                      All scraped data (contents git-ignored)
  AGENTS.md                Layout guide for AI agents
  <wiki>/
    <wiki>.db              SQLite database per wiki (pages + FTS5 index)
    mediawiki/             Raw wikitext files (--with-mediawiki)
    static/
      theme.css            Per-wiki theme variables (auto-downloaded by scraper)
      images/              Wiki images and other uploads, named by original filename
templates/
  index.html               Search/browse page
  page.html                Wiki page viewer
```

### CSS Load Order

1. `hive/<wiki>/static/theme.css` (served as `/static/<wiki>/theme.css`) — per-wiki CSS variables (colors, fonts, background image)
2. `static/fandom-all.css` — shared Fandom layout CSS
3. Inline fallback CSS — covers infoboxes, tables, tabs, galleries when full CSS is missing

## How It Works

### Scraping

Uses the MediaWiki API exclusively — no HTML scraping or browser automation.

1. `action=query&list=allpages` — enumerate all content pages
2. `action=parse&prop=text|categories|images` — rendered HTML per page
3. `action=query&prop=imageinfo&iiprop=url` — batch-resolve image URLs (50 at a time)
4. Download images to `hive/<wiki>/static/images/`
5. Rewrite HTML: remote image URLs → local paths, wiki links → local routes
6. Store in SQLite with FTS5 triggers for automatic search indexing
7. `wikia.php?controller=ThemeApi&method=themeVariables` — download theme CSS

Rate limited to 0.5s between requests. Resumable — skips already-scraped pages and existing images.

`--no-images` and the file flags limit which uploads steps 3 and 4 fetch (skipping them entirely when both kinds are off), `--no-style` skips step 7, and `--no-html` skips steps 2 and 5–6 (images then come from `action=query&generator=allpages&prop=images`). `--with-mediawiki` adds `action=query&prop=revisions&rvprop=content&rvslots=main`, 50 pageids per request.

### Search

SQLite FTS5 with prefix matching (`word*`). Live search via `/api/search` JSON endpoint with 200ms debounce. Title matches sorted first. URL updates via `replaceState`. Ctrl+Shift+F hotkey on wiki pages jumps to search.

### Serving

```
python server.py <wiki> [--no-scrape] [--hive DIR] [--host 0.0.0.0] [--port 5000]
```

- `/` — search/browse all pages
- `/wiki/<title>` — wiki page (underscores normalized to spaces, matching MediaWiki convention)
- `/api/search?q=term` — JSON search endpoint

## MCP server

`mcp_serve.py` exposes the hive to AI agents over [MCP](https://modelcontextprotocol.io/) (streamable HTTP), read-only:

| Tool | What it does |
|---|---|
| `list_hives` | The scraped wikis and what each contains (HTML pages, wikitext pages, media files, interrupted scrapes) |
| `search` | Full-text search of one wiki; prefix matching, `"exact phrases"`. Uses the index chosen with `--fts`; falls back to scanning wikitext for wikis with neither index. |
| `get_page` | One page by title: its wikitext if saved, otherwise its HTML. Follows redirects; long pages are cut at `max_chars`. |
| `get_pages` | Up to 50 pages at once; missing ones are listed instead of failing the call |
| `get_media` | A downloaded image, audio clip or other file (up to 20 MB) |

It needs Python 3.10+ (for the `mcp` SDK). Every request must carry a static API key:

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"   # paste into FANDOM_MCP_API_KEY in .env
python mcp_serve.py                                             # http://127.0.0.1:8765/mcp
```

`.env` also sets the bind address, port and hive (`FANDOM_MCP_HOST`, `FANDOM_MCP_PORT`, `FANDOM_HIVE`); `--host`, `--port`, `--hive` and `--env-file` override it, and real environment variables override the file. The default only accepts local connections. To serve other machines set `FANDOM_MCP_HOST=0.0.0.0`, and put TLS in front of it beyond a trusted network, since the key travels in a header.

Clients send the key as `Authorization: Bearer <key>` (or `X-API-Key: <key>`). For Claude Code:

```bash
claude mcp add --transport http fandom-hive http://127.0.0.1:8765/mcp --header "Authorization: Bearer <key>"
```

## Cloudflare Gotchas

| Resource | Accessible? |
|----------|-------------|
| `api.php` (MediaWiki API) | ✅ Yes |
| `wikia.php` (theme variables) | ✅ Yes |
| `static.wikia.nocookie.net` (images) | ✅ Yes |
| `load.php` (CSS bundles) | ❌ Cloudflare blocked |
| Wiki HTML pages | ❌ Cloudflare blocked for non-browsers |

This is why `fandom-all.css` requires manual browser extraction.

## robots.txt

Fandom explicitly allows `/api.php?` for all bots. We're compliant.

## Dependencies

- Python 3, `requests`, `flask`
- For `mcp_serve.py`: Python 3.10+, `mcp`, `python-dotenv`
- SQLite with FTS5 (included in Python's `sqlite3`)

## Development

```bash
pip install -r requirements.txt
./scripts/install-hooks   # one-time: installs formatting pre-commit hook
tox                       # run tests
tox -e format             # auto-format
tox -e typecheck          # mypy + pyright
```

## License

Personal tool for offline wiki browsing. All wiki content belongs to its respective authors under [CC-BY-SA](https://creativecommons.org/licenses/by-sa/3.0/).
