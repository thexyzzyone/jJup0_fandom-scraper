"""Unit tests for mcp_serve.py."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("mcp")  # needs Python 3.10+

import anyio
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import AudioContent, EmbeddedResource, ImageContent, TextContent

import mcp_serve
import scrape

WIKI = "testwiki"


def _page(pageid: int, title: str, html: str, categories: list[str]) -> tuple[Any, ...]:
    return (
        pageid,
        title,
        html,
        scrape.strip_text(html),
        json.dumps(categories),
        "2024-01-01T00:00:00Z",
    )


@pytest.fixture
def hive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A hive with one wiki holding HTML pages, wikitext and media."""
    layout = scrape.hive_layout(str(tmp_path), WIKI)
    os.makedirs(layout.mediawiki)
    os.makedirs(layout.images)
    conn = scrape.init_db(layout.db)
    conn.executemany(
        "INSERT INTO pages (pageid,title,html,plaintext,categories,touched) "
        "VALUES (?,?,?,?,?,?)",
        [
            _page(1, "Dragon", "<p>A large bronze dragon lives here</p>", ["Beasts"]),
            _page(2, "Gold Coin", "<p>Currency used by dragon hoards</p>", []),
            _page(
                3,
                "Wyrm",
                '<div class="redirectMsg"><p>Redirect to:</p><ul class="redirectText">'
                '<li><a href="/wiki/Dragon" title="Dragon">Dragon</a></li></ul></div>',
                [],
            ),
            _page(4, "HTML Only", "<p>No wikitext saved for this one</p>", ["Misc"]),
            _page(
                5,
                "Broken Link",
                '<div class="redirectMsg"><a href="/wiki/Not_Scraped">x</a></div>',
                [],
            ),
        ],
    )
    conn.commit()
    conn.close()
    for pageid, title, text in [
        (1, "Dragon", "{{Creature|name = Dragon|size = huge}}\nA dragon."),
        (2, "Gold Coin", "Coins. [[Dragon]]s hoard them."),
        (3, "Wyrm", "#REDIRECT [[Dragon]]"),
        (6, "AC/DC", "Band page"),
    ]:
        path = os.path.join(layout.mediawiki, scrape.wikitext_filename(pageid, title))
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    for name, data in [
        ("Dragon.png", b"\x89PNG fake"),
        ("Roar.wav", b"RIFF fake"),
        ("Font.ttf", b"ttf fake"),
    ]:
        (Path(layout.images) / name).write_bytes(data)
    # Not wikis: a stray file, a hidden dir and a name the scraper can't produce
    (tmp_path / "README.txt").write_text("x")
    (tmp_path / ".cache").mkdir()
    (tmp_path / "Bad_Name").mkdir()
    monkeypatch.setattr(mcp_serve, "_hive", str(tmp_path))
    return tmp_path


def _wikitext_only(hive: Path) -> None:
    """Turn the fixture into a hive scraped with --no-html."""
    os.remove(hive / WIKI / f"{WIKI}.db")


def _mediawiki_fts(hive: Path) -> None:
    """Turn the fixture into a hive scraped with --fts=mediawiki."""
    layout = scrape.hive_layout(str(hive), WIKI)
    conn = scrape.init_db(layout.db)
    scrape.set_fts_mode(conn, scrape.FTS_MEDIAWIKI)
    scrape.index_wikitext(conn, layout.mediawiki)
    conn.close()


# ---------------------------------------------------------------------------
# list_hives
# ---------------------------------------------------------------------------
class TestListHives:
    def test_lists_contents(self, hive: Path) -> None:
        assert mcp_serve.list_hives() == [
            {
                "wiki": WIKI,
                "fandom_url": f"https://{WIKI}.fandom.com",
                "html_pages": 5,
                "wikitext_pages": 4,
                "media_files": 3,
                "has_theme": False,
                "search_index": "html",
                "scrape_status": None,
            }
        ]

    def test_reports_interrupted_scrape(self, hive: Path) -> None:
        (hive / WIKI / f".{WIKI}.status").write_text("images\n")
        assert mcp_serve.list_hives()[0]["scrape_status"] == "images"

    def test_wiki_without_database(self, hive: Path) -> None:
        _wikitext_only(hive)
        info = mcp_serve.list_hives()[0]
        assert info["html_pages"] == 0
        assert info["search_index"] == "wikitext-scan"

    def test_mediawiki_index(self, hive: Path) -> None:
        _mediawiki_fts(hive)
        assert mcp_serve.list_hives()[0]["search_index"] == "mediawiki"

    def test_missing_hive_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mcp_serve, "_hive", "/nonexistent/hive")
        assert mcp_serve.list_hives() == []


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------
class TestSearch:
    def test_fts_title_matches_first(self, hive: Path) -> None:
        result = mcp_serve.search(WIKI, "dragon")
        assert result["source"] == "fts-html"
        titles = [r["title"] for r in result["results"]]
        assert titles[0] == "Dragon"
        assert "Gold Coin" in titles
        assert "[" in result["results"][0]["snippet"]

    def test_prefix_and_phrase(self, hive: Path) -> None:
        assert [r["title"] for r in mcp_serve.search(WIKI, "curren")["results"]] == [
            "Gold Coin"
        ]
        phrase = mcp_serve.search(WIKI, '"bronze dragon"')["results"]
        assert [r["title"] for r in phrase] == ["Dragon"]
        assert mcp_serve.search(WIKI, '"dragon bronze"')["results"] == []

    def test_limit(self, hive: Path) -> None:
        assert len(mcp_serve.search(WIKI, "dragon", limit=1)["results"]) == 1

    @pytest.mark.parametrize(
        "query",
        [
            'dragon"',
            "(dragon",
            "dragon:",
            "NEAR(",
            "-dragon",
            "^dragon",
            "*",
            "dragon AND",
            "OR",
            "NOT dragon",
            "title:dragon",
            "{dragon}",
        ],
    )
    def test_fts_syntax_does_not_error(self, hive: Path, query: str) -> None:
        mcp_serve.search(WIKI, query)

    @pytest.mark.parametrize(
        "query, expected",
        [
            ("dragon gold", '"dragon"* "gold"*'),
            ('"red dragon" hoard', '"red dragon" "hoard"*'),
            ("dragon OR coin", '"dragon"* "OR"* "coin"*'),
            ('"  spaced   out "', '"spaced out"'),
            ('"" * -', ""),
        ],
    )
    def test_fts_query(self, query: str, expected: str) -> None:
        assert mcp_serve._fts_query(query) == expected

    def test_wikitext_scan_without_html(self, hive: Path) -> None:
        _wikitext_only(hive)
        result = mcp_serve.search(WIKI, "dragon")
        assert result["source"] == "wikitext-scan"
        # Title matches first, then pages that mention it, in filename order
        titles = [r["title"] for r in result["results"]]
        assert titles == ["Dragon", "Gold Coin", "Wyrm"]
        assert mcp_serve.search(WIKI, "hoard dragon")["results"][0]["title"] == (
            "Gold Coin"
        )
        assert mcp_serve.search(WIKI, "band")["results"][0]["title"] == "AC/DC"

    def test_mediawiki_index(self, hive: Path) -> None:
        _mediawiki_fts(hive)
        # "Creature" and "huge" only exist in Dragon's wikitext infobox;
        # "lives" only in its HTML, which isn't indexed in this mode
        result = mcp_serve.search(WIKI, "creature huge")
        assert result["source"] == "fts-mediawiki"
        assert [r["title"] for r in result["results"]] == ["Dragon"]
        assert mcp_serve.search(WIKI, "lives")["results"] == []
        assert [r["title"] for r in mcp_serve.search(WIKI, "band")["results"]] == [
            "AC/DC"
        ]

    def test_empty_mediawiki_index_falls_back_to_scan(self, hive: Path) -> None:
        layout = scrape.hive_layout(str(hive), WIKI)
        conn = scrape.init_db(layout.db)
        scrape.set_fts_mode(conn, scrape.FTS_MEDIAWIKI)
        conn.close()
        assert mcp_serve.search(WIKI, "dragon")["source"] == "wikitext-scan"

    def test_nothing_to_search(self, hive: Path) -> None:
        _wikitext_only(hive)
        for f in (hive / WIKI / "mediawiki").iterdir():
            f.unlink()
        with pytest.raises(ToolError, match="no pages"):
            mcp_serve.search(WIKI, "dragon")


# ---------------------------------------------------------------------------
# get_page / get_pages
# ---------------------------------------------------------------------------
class TestGetPage:
    def test_prefers_wikitext(self, hive: Path) -> None:
        page = mcp_serve.get_page(WIKI, "Dragon")
        assert page["format"] == "mediawiki"
        assert page["content"].startswith("{{Creature")
        assert page["pageid"] == 1
        assert page["categories"] == ["Beasts"]
        assert page["touched"] == "2024-01-01T00:00:00Z"
        assert page["fandom_url"] == f"https://{WIKI}.fandom.com/wiki/Dragon"

    def test_falls_back_to_html(self, hive: Path) -> None:
        page = mcp_serve.get_page(WIKI, "HTML Only")
        assert page["format"] == "html"
        assert "No wikitext" in page["content"]
        assert page["categories"] == ["Misc"]

    def test_wikitext_without_database(self, hive: Path) -> None:
        _wikitext_only(hive)
        page = mcp_serve.get_page(WIKI, "Gold Coin")
        assert page["format"] == "mediawiki"
        assert page["categories"] is None

    @pytest.mark.parametrize(
        "title", ["gold_coin", "Gold_Coin", " gold  coin ", "GOLD COIN"]
    )
    def test_title_matching(self, hive: Path, title: str) -> None:
        page = mcp_serve.get_page(WIKI, title)
        assert page["title"] == "Gold Coin"
        assert page["requested_title"] == title

    def test_percent_encoded_title(self, hive: Path) -> None:
        assert mcp_serve.get_page(WIKI, "AC/DC")["content"] == "Band page"

    def test_follows_wikitext_redirect(self, hive: Path) -> None:
        page = mcp_serve.get_page(WIKI, "Wyrm")
        assert page["title"] == "Dragon"
        assert page["redirected_from"] == ["Wyrm"]
        assert page["format"] == "mediawiki"

    def test_follows_html_redirect(self, hive: Path) -> None:
        os.remove(hive / WIKI / "mediawiki" / "Wyrm.3.mediawiki")
        page = mcp_serve.get_page(WIKI, "Wyrm")
        assert page["title"] == "Dragon"
        assert page["redirected_from"] == ["Wyrm"]

    def test_redirect_to_missing_page(self, hive: Path) -> None:
        with pytest.raises(ToolError, match="redirects to 'Not Scraped'"):
            mcp_serve.get_page(WIKI, "Broken Link")

    def test_redirect_loop_stops(self, hive: Path) -> None:
        mw = hive / WIKI / "mediawiki"
        (mw / "Loop_A.10.mediawiki").write_text("#REDIRECT [[Loop B]]")
        (mw / "Loop_B.11.mediawiki").write_text("#redirect: [[Loop A|label]]")
        page = mcp_serve.get_page(WIKI, "Loop A")
        assert len(page["redirected_from"]) == mcp_serve.MAX_REDIRECTS
        assert page["content"].lower().startswith("#redirect")

    def test_truncation(self, hive: Path) -> None:
        page = mcp_serve.get_page(WIKI, "Dragon", max_chars=5)
        assert page["content"] == "{{Cre"
        assert page["truncated"] is True
        assert mcp_serve.get_page(WIKI, "Dragon")["truncated"] is False

    def test_missing_page(self, hive: Path) -> None:
        with pytest.raises(ToolError, match="No page titled 'Unicorn'"):
            mcp_serve.get_page(WIKI, "Unicorn")

    @pytest.mark.parametrize("wiki", ["nope", "../etc", "Bad_Name", ".cache", ""])
    def test_unknown_wiki(self, hive: Path, wiki: str) -> None:
        with pytest.raises(ToolError, match="list_hives"):
            mcp_serve.get_page(wiki, "Dragon")

    def test_wiki_name_case_insensitive(self, hive: Path) -> None:
        assert mcp_serve.get_page("TestWiki", "Dragon")["wiki"] == WIKI

    def test_database_opened_read_only(self, hive: Path) -> None:
        layout = scrape.hive_layout(str(hive), WIKI)
        conn = mcp_serve._connect(layout)
        assert conn is not None
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM pages")
        conn.close()


class TestGetPages:
    def test_mixed(self, hive: Path) -> None:
        result = mcp_serve.get_pages(WIKI, ["Dragon", "Unicorn", "HTML Only"], 10)
        assert [p["title"] for p in result["pages"]] == ["Dragon", "HTML Only"]
        assert all(len(p["content"]) <= 10 for p in result["pages"])
        assert [m["title"] for m in result["missing"]] == ["Unicorn"]
        assert "No page titled" in result["missing"][0]["error"]

    def test_too_many(self, hive: Path) -> None:
        with pytest.raises(ToolError, match="At most 50"):
            mcp_serve.get_pages(WIKI, ["Dragon"] * 51)

    def test_unknown_wiki(self, hive: Path) -> None:
        with pytest.raises(ToolError):
            mcp_serve.get_pages("nope", ["Dragon"])


# ---------------------------------------------------------------------------
# get_media
# ---------------------------------------------------------------------------
class TestGetMedia:
    def test_image(self, hive: Path) -> None:
        info, media = mcp_serve.get_media(WIKI, "Dragon.png")
        assert isinstance(info, TextContent)
        meta = json.loads(info.text)
        assert meta["mime_type"] == "image/png"
        assert meta["bytes"] == len(b"\x89PNG fake")
        assert meta["web_ui_path"] == f"/static/{WIKI}/images/Dragon.png"
        assert isinstance(media, ImageContent)
        assert base64.b64decode(media.data) == b"\x89PNG fake"

    def test_audio(self, hive: Path) -> None:
        media = mcp_serve.get_media(WIKI, "Roar.wav")[1]
        assert isinstance(media, AudioContent)
        assert media.mime_type.startswith("audio/")

    def test_other_file(self, hive: Path) -> None:
        media = mcp_serve.get_media(WIKI, "Font.ttf")[1]
        assert isinstance(media, EmbeddedResource)
        assert base64.b64decode(media.resource.blob) == b"ttf fake"  # type: ignore[union-attr]
        assert media.resource.uri == f"hive://{WIKI}/static/images/Font.ttf"

    @pytest.mark.parametrize(
        "name", ["File:Dragon.png", "file:dragon.PNG", "Image:Dragon.png", "dragon.png"]
    )
    def test_name_forms(self, hive: Path, name: str) -> None:
        info = mcp_serve.get_media(WIKI, name)[0]
        assert isinstance(info, TextContent)
        assert json.loads(info.text)["filename"] == "Dragon.png"

    def test_spaces_become_underscores(self, hive: Path) -> None:
        (hive / WIKI / "static" / "images" / "Big_Roar.wav").write_bytes(b"x")
        info = mcp_serve.get_media(WIKI, "File:Big Roar.wav")[0]
        assert isinstance(info, TextContent)
        assert json.loads(info.text)["filename"] == "Big_Roar.wav"

    @pytest.mark.parametrize(
        "name", ["Nope.png", "../testwiki.db", "../../README.txt", "/etc/passwd"]
    )
    def test_missing_or_outside(self, hive: Path, name: str) -> None:
        with pytest.raises(ToolError, match="isn't in the"):
            mcp_serve.get_media(WIKI, name)

    def test_size_limit(self, hive: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mcp_serve, "MAX_MEDIA_BYTES", 4)
        with pytest.raises(ToolError, match="at most"):
            mcp_serve.get_media(WIKI, "Dragon.png")


# ---------------------------------------------------------------------------
# Through the MCP tool layer
# ---------------------------------------------------------------------------
class TestToolLayer:
    def test_tools_registered_read_only(self) -> None:
        tools = anyio.run(mcp_serve.server.list_tools)
        assert sorted(t.name for t in tools) == [
            "get_media",
            "get_page",
            "get_pages",
            "list_hives",
            "search",
        ]
        for tool in tools:
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is True
            assert tool.description

    def test_call_returns_structured_content(self, hive: Path) -> None:
        async def call() -> Any:
            return await mcp_serve.server.call_tool(
                "get_page", {"wiki": WIKI, "title": "Dragon"}
            )

        result = anyio.run(call)
        assert result.structured_content["format"] == "mediawiki"

    def test_tool_error_raised_by_call_tool(self, hive: Path) -> None:
        async def call() -> Any:
            return await mcp_serve.server.call_tool(
                "get_page", {"wiki": "nope", "title": "x"}
            )

        with pytest.raises(ToolError, match="No hive named 'nope'"):
            anyio.run(call)


# ---------------------------------------------------------------------------
# API key
# ---------------------------------------------------------------------------
KEY = "k" * 32


def _asgi(
    scope_type: str, headers: list[tuple[bytes, bytes]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Run APIKeyMiddleware once; return what it sent and whether the app ran."""
    sent: list[dict[str, Any]] = []
    reached: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        reached.append(scope["type"])

    async def receive() -> dict[str, Any]:
        return {"type": "http.request"}

    async def send(message: Any) -> None:
        sent.append(message)

    middleware = mcp_serve.APIKeyMiddleware(app, KEY)
    anyio.run(middleware, {"type": scope_type, "headers": headers}, receive, send)
    return sent, reached


class TestAPIKeyMiddleware:
    @pytest.mark.parametrize(
        "headers",
        [
            [],
            [(b"authorization", b"Bearer wrong")],
            [(b"authorization", b"Bearer " + KEY[:-1].encode())],
            [(b"authorization", b"Basic " + KEY.encode())],
            [(b"authorization", b"Bearer ")],
            [(b"x-api-key", b"")],
        ],
    )
    def test_rejects(self, headers: list[tuple[bytes, bytes]]) -> None:
        sent, reached = _asgi("http", headers)
        assert reached == []
        assert sent[0]["status"] == 401
        assert (b"www-authenticate", b'Bearer realm="fandom-hive"') in sent[0][
            "headers"
        ]
        assert b"API key" in sent[1]["body"]

    @pytest.mark.parametrize(
        "headers",
        [
            [(b"authorization", b"Bearer " + KEY.encode())],
            [(b"authorization", b"bearer  " + KEY.encode())],
            [(b"x-api-key", KEY.encode())],
        ],
    )
    def test_accepts(self, headers: list[tuple[bytes, bytes]]) -> None:
        sent, reached = _asgi("http", headers)
        assert reached == ["http"]
        assert sent == []

    def test_websocket_without_key_closed(self) -> None:
        sent, reached = _asgi("websocket", [])
        assert reached == []
        assert sent == [{"type": "websocket.close", "code": 1008}]

    def test_lifespan_passes_through(self) -> None:
        assert _asgi("lifespan", [])[1] == ["lifespan"]


class TestMain:
    @pytest.mark.parametrize("key", [None, "", "short"])
    def test_requires_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str | None
    ) -> None:
        monkeypatch.delenv(mcp_serve.API_KEY_ENV, raising=False)
        env = tmp_path / ".env"
        env.write_text("" if key is None else f"{mcp_serve.API_KEY_ENV}={key}\n")
        argv = ["mcp_serve.py", "--env-file", str(env)]
        with patch("sys.argv", argv), patch("uvicorn.run") as run:
            with pytest.raises(SystemExit, match=mcp_serve.API_KEY_ENV):
                mcp_serve.main()
        run.assert_not_called()

    def test_settings_from_env_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for var in (mcp_serve.API_KEY_ENV, "FANDOM_HIVE", "FANDOM_MCP_HOST"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv("FANDOM_MCP_PORT", raising=False)
        monkeypatch.setattr(mcp_serve, "_hive", mcp_serve._hive)
        env = tmp_path / ".env"
        env.write_text(
            f"{mcp_serve.API_KEY_ENV}={KEY}\nFANDOM_MCP_PORT=9123\n"
            f"FANDOM_HIVE={tmp_path / 'h'}\n"
        )
        with patch("sys.argv", ["mcp_serve.py", "--env-file", str(env)]):
            with patch("uvicorn.run") as run:
                mcp_serve.main()
        app = run.call_args.args[0]
        assert isinstance(app, mcp_serve.APIKeyMiddleware)
        assert app.api_key == KEY.encode()
        assert run.call_args.kwargs == {"host": "127.0.0.1", "port": 9123}
        assert mcp_serve._hive == str(tmp_path / "h")

    def test_cli_overrides_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(mcp_serve.API_KEY_ENV, KEY)
        monkeypatch.setenv("FANDOM_MCP_PORT", "9123")
        monkeypatch.setattr(mcp_serve, "_hive", mcp_serve._hive)
        argv = [
            "mcp_serve.py",
            "--env-file",
            str(tmp_path / "missing.env"),
            "--port",
            "9200",
            "--host",
            "0.0.0.0",
        ]
        with patch("sys.argv", argv), patch("uvicorn.run") as run:
            mcp_serve.main()
        assert run.call_args.kwargs == {"host": "0.0.0.0", "port": 9200}
