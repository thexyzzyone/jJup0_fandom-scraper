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

Each wiki gets its own database (`<wiki>.db`) and asset directory (`static/<wiki>/`).

### Choosing what to scrape

By default the scraper stores rendered HTML (with its search index), downloads images, and downloads the wiki's theme CSS. These flags change that, and any combination is allowed:

| Flag | Effect |
|---|---|
| `--with-mediawiki` | Also save each page's raw wikitext to `<wiki>-mediawiki/<Title>.<pageid>.mediawiki` |
| `--no-html` | Don't store rendered HTML. `server.py` has nothing to serve from this database. |
| `--no-images` | Don't download images. Stored HTML keeps pointing at Fandom's servers, so images only load while you're online. |
| `--no-style` | Don't download theme CSS (`static/<wiki>/theme.css`) |

```bash
# Everything, plus wikitext files
python scrape.py spiritfarer --with-mediawiki

# Searchable text with no media
python scrape.py spiritfarer --no-images --no-style

# Wikitext files only
python scrape.py spiritfarer --with-mediawiki --no-html --no-images --no-style
```

Turning everything off is an error. A later run without `--no-images` downloads the missing images and switches stored pages to the local copies. With `--no-html`, images are still downloaded (unless `--no-images` is also given): the scraper asks the API for every image used on an article page, which is the same set a normal scrape downloads.

#### Wikitext files

Each `.mediawiki` file is the page's source exactly as stored on the wiki. Filenames end in the pageid so pages whose titles differ only by case (common with redirects) don't overwrite each other on case-insensitive filesystems. Characters that aren't safe in filenames (`/ \ : % * ? " < > |`) are percent-encoded, and spaces become underscores. When a page is renamed on the wiki, its old file is deleted.

`--mediawiki-tracking` sets where the scraper remembers which wikitext it has already saved, so reruns only fetch new or changed pages:

| `--mediawiki-tracking` | Bookkeeping | Reruns |
|---|---|---|
| `db` (default) | `wikitext_pages` table in `<wiki>.db` (pageid, title, timestamp; never the page text) | Fetch only new or changed pages |
| `manifest` | `<wiki>-mediawiki/_index.json` | Fetch only new or changed pages |
| `none` | Nothing | Fetch every page again |

With `--no-html`, a `manifest` or `none` run creates no database at all. Wikitext is fetched 50 pages per request, so even `none` is fast compared with an HTML scrape.

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

This only needs to be done once — the CSS is shared across all wikis. Per-wiki theming comes from `static/<wiki>/theme.css` which the scraper downloads automatically.

Without this step, the built-in fallback CSS handles infoboxes, tables, tabs, and galleries — just not pixel-perfect. The server will show a warning banner when the full CSS is missing.

## Architecture

```
scrape.py                  MediaWiki API scraper → SQLite + local images
server.py                  Flask web server with FTS5 search
static/
  fandom-all.css           Fandom's layout CSS (extracted from browser, shared across wikis)
  <wiki>/
    theme.css              Per-wiki theme variables (auto-downloaded by scraper)
    images/                Wiki images, named by original filename
<wiki>.db                  SQLite database per wiki (pages + FTS5 index)
<wiki>-mediawiki/          Raw wikitext files (--with-mediawiki)
templates/
  index.html               Search/browse page
  page.html                Wiki page viewer
```

### CSS Load Order

1. `static/<wiki>/theme.css` — per-wiki CSS variables (colors, fonts, background image)
2. `static/fandom-all.css` — shared Fandom layout CSS
3. Inline fallback CSS — covers infoboxes, tables, tabs, galleries when full CSS is missing

## How It Works

### Scraping

Uses the MediaWiki API exclusively — no HTML scraping or browser automation.

1. `action=query&list=allpages` — enumerate all content pages
2. `action=parse&prop=text|categories|images` — rendered HTML per page
3. `action=query&prop=imageinfo&iiprop=url` — batch-resolve image URLs (50 at a time)
4. Download images to `static/<wiki>/images/`
5. Rewrite HTML: remote image URLs → local paths, wiki links → local routes
6. Store in SQLite with FTS5 triggers for automatic search indexing
7. `wikia.php?controller=ThemeApi&method=themeVariables` — download theme CSS

Rate limited to 0.5s between requests. Resumable — skips already-scraped pages and existing images.

`--no-images` skips steps 3 and 4, `--no-style` skips step 7, and `--no-html` skips steps 2 and 5–6 (images then come from `action=query&generator=allpages&prop=images`). `--with-mediawiki` adds `action=query&prop=revisions&rvprop=content&rvslots=main`, 50 pageids per request.

### Search

SQLite FTS5 with prefix matching (`word*`). Live search via `/api/search` JSON endpoint with 200ms debounce. Title matches sorted first. URL updates via `replaceState`. Ctrl+Shift+F hotkey on wiki pages jumps to search.

### Serving

```
python server.py <wiki> [--no-scrape] [--host 0.0.0.0] [--port 5000]
```

- `/` — search/browse all pages
- `/wiki/<title>` — wiki page (underscores normalized to spaces, matching MediaWiki convention)
- `/api/search?q=term` — JSON search endpoint

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
