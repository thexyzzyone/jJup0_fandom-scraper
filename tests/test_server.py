"""Unit tests for server.py Flask routes."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask.testing import FlaskClient


class TestIndex:
    def test_lists_pages(self, client: FlaskClient) -> None:
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"Main Page" in resp.data

    def test_shows_page_count(self, client: FlaskClient) -> None:
        resp = client.get("/")
        assert b"8 pages" in resp.data

    def test_search(self, client: FlaskClient) -> None:
        resp = client.get("/?q=puzzle")
        assert resp.status_code == 200
        assert b"Gorogoa" in resp.data

    def test_search_no_results(self, client: FlaskClient) -> None:
        resp = client.get("/?q=zzzznonexistent")
        assert resp.status_code == 200
        assert b"0 results" in resp.data

    @pytest.mark.parametrize("q", ["", "   "])
    def test_empty_or_whitespace_shows_browse(
        self, client: FlaskClient, q: str
    ) -> None:
        resp = client.get(f"/?q={q}")
        assert resp.status_code == 200
        assert b"Main Page" in resp.data

    def test_browse_pages_sorted_alphabetically(self, client: FlaskClient) -> None:
        resp = client.get("/")
        data = resp.data.decode()
        titles = ["Empty Content", "FTS Test", "Gorogoa", "Main Page"]
        positions = [data.index(t) for t in titles]
        assert positions == sorted(positions)

    def test_wiki_name_displayed(self, client: FlaskClient) -> None:
        resp = client.get("/")
        assert b"Test Wiki" in resp.data

    def test_page_links_use_underscores(self, client: FlaskClient) -> None:
        resp = client.get("/")
        assert (
            b"/wiki/Page_With_Spaces" in resp.data
            or b"/wiki/Page+With+Spaces" in resp.data
        )

    def test_scraping_notice_hidden_when_not_scraping(
        self, client: FlaskClient
    ) -> None:
        import server

        server._status_path = "/nonexistent/.status"
        resp = client.get("/")
        assert b'<div class="scraping-notice">' not in resp.data

    def test_scraping_notice_visible_when_scraping_pages(
        self, client: FlaskClient, tmp_path: Path
    ) -> None:
        import server

        status = tmp_path / ".test.status"
        status.write_text("pages")
        server._status_path = str(status)
        resp = client.get("/")
        assert b'<div class="scraping-notice">' in resp.data
        assert b"Scraping in progress" in resp.data
        server._status_path = None

    def test_scraping_notice_hidden_during_image_phase(
        self, client: FlaskClient, tmp_path: Path
    ) -> None:
        import server

        status = tmp_path / ".test.status"
        status.write_text("images")
        server._status_path = str(status)
        resp = client.get("/")
        assert b'<div class="scraping-notice">' not in resp.data
        server._status_path = None

    def test_scraping_notice_visible_during_search(
        self, client: FlaskClient, tmp_path: Path
    ) -> None:
        import server

        status = tmp_path / ".test.status"
        status.write_text("pages")
        server._status_path = str(status)
        resp = client.get("/?q=puzzle")
        assert b'<div class="scraping-notice">' in resp.data
        server._status_path = None


class TestWikiPage:
    def test_found(self, client: FlaskClient) -> None:
        resp = client.get("/wiki/Gorogoa")
        assert resp.status_code == 200
        assert b"puzzle game" in resp.data

    def test_not_found_shows_fandom_link(self, client: FlaskClient) -> None:
        resp = client.get("/wiki/DoesNotExist")
        assert resp.status_code == 404
        assert b"fandom.com" in resp.data

    @patch("server.http_requests.get")
    def test_on_demand_fetch_from_fandom(
        self, mock_get: MagicMock, client: FlaskClient
    ) -> None:
        api_resp = MagicMock()
        api_resp.json.return_value = {
            "parse": {
                "title": "NewPage",
                "text": {"*": "<p>fetched content</p>"},
                "categories": [],
                "images": [],
            }
        }
        mock_get.return_value = api_resp
        resp = client.get("/wiki/NewPage")
        assert resp.status_code == 200
        assert b"fetched content" in resp.data

    @patch("server.http_requests.get")
    def test_on_demand_fetch_failure_shows_fandom_link(
        self, mock_get: MagicMock, client: FlaskClient
    ) -> None:
        mock_get.side_effect = Exception("timeout")
        resp = client.get("/wiki/FailPage")
        assert resp.status_code == 404
        assert b"fandom.com" in resp.data

    def test_fandom_link_on_existing_page(self, client: FlaskClient) -> None:
        resp = client.get("/wiki/Gorogoa")
        assert b"View on Fandom" in resp.data
        assert b"testwiki.fandom.com" in resp.data

    def test_underscore_to_space(self, client: FlaskClient) -> None:
        resp = client.get("/wiki/Main_Page")
        assert resp.status_code == 200
        assert b"Welcome" in resp.data

    def test_redirect(self, client: FlaskClient) -> None:
        resp = client.get("/wiki/Old Name")
        assert resp.status_code == 302
        assert "/wiki/Gorogoa" in resp.headers["Location"]

    def test_page_with_spaces_via_underscores(self, client: FlaskClient) -> None:
        resp = client.get("/wiki/Page_With_Spaces")
        assert resp.status_code == 200
        assert b"spaced" in resp.data

    def test_categories_rendered(self, client: FlaskClient) -> None:
        resp = client.get("/wiki/Gorogoa")
        assert b"Games" in resp.data

    def test_multiple_categories(self, client: FlaskClient) -> None:
        resp = client.get("/wiki/FTS_Test")
        assert b"Music" in resp.data
        assert b"Animals" in resp.data

    def test_css_warning_shown_without_full_css(self, client: FlaskClient) -> None:
        resp = client.get("/wiki/Gorogoa")
        assert b"fallback CSS" in resp.data

    def test_css_warning_hidden_with_full_css(self, client: FlaskClient) -> None:
        import server

        server.app.config["HAS_FULL_CSS"] = True
        resp = client.get("/wiki/Gorogoa")
        assert b"fallback CSS" not in resp.data

    def test_unicode_page(self, client: FlaskClient) -> None:
        resp = client.get("/wiki/Über_Page")
        assert resp.status_code == 200
        assert b"unicode content" in resp.data

    def test_special_chars_in_title(self, client: FlaskClient) -> None:
        resp = client.get("/wiki/Special_&_Characters")
        assert resp.status_code == 200
        assert b"ampersand test" in resp.data

    def test_page_no_categories_has_no_categories_div(
        self, client: FlaskClient
    ) -> None:
        resp = client.get("/wiki/Main_Page")
        assert b'class="categories"' not in resp.data

    @pytest.mark.parametrize("path", ["/wiki/", "/wiki/" + "a" * 500])
    def test_edge_case_paths_404(self, client: FlaskClient, path: str) -> None:
        assert client.get(path).status_code == 404

    def test_empty_content_page(self, client: FlaskClient) -> None:
        resp = client.get("/wiki/Empty_Content")
        assert resp.status_code == 200

    def test_page_title_in_html_title(self, client: FlaskClient) -> None:
        resp = client.get("/wiki/Gorogoa")
        assert b"<title>Gorogoa" in resp.data

    def test_back_link_present(self, client: FlaskClient) -> None:
        resp = client.get("/wiki/Gorogoa")
        assert b'href="/"' in resp.data

    def test_keyboard_shortcut_script_present(self, client: FlaskClient) -> None:
        resp = client.get("/wiki/Gorogoa")
        assert b"shiftKey" in resp.data and b"keydown" in resp.data

    def test_theme_css_link(self, client: FlaskClient) -> None:
        resp = client.get("/wiki/Gorogoa")
        assert b"/static/testwiki/theme.css" in resp.data


class TestImageProxy:
    def test_serves_existing_local_image(
        self, client: FlaskClient, tmp_path: Path
    ) -> None:
        import server

        img_dir = tmp_path / "testwiki" / "images"
        img_dir.mkdir(parents=True)
        (img_dir / "local.png").write_bytes(b"PNG_DATA")
        server._wiki_static = str(tmp_path / "testwiki")
        resp = client.get("/image-proxy/testwiki/local.png")
        assert resp.status_code == 200
        assert resp.data == b"PNG_DATA"

    @patch("server.http_requests.get")
    def test_fetches_remote_and_caches(
        self, mock_get: MagicMock, client: FlaskClient, tmp_path: Path
    ) -> None:
        import server

        img_dir = tmp_path / "testwiki" / "images"
        img_dir.mkdir(parents=True)
        server._wiki_static = str(tmp_path / "testwiki")

        api_resp = MagicMock()
        api_resp.json.return_value = {
            "query": {
                "pages": {
                    "1": {
                        "title": "File:remote.png",
                        "imageinfo": [{"url": "https://example.com/remote.png"}],
                    }
                }
            }
        }
        img_resp = MagicMock()
        img_resp.content = b"REMOTE_IMG"
        img_resp.raise_for_status = MagicMock()
        mock_get.side_effect = [api_resp, img_resp]

        resp = client.get("/image-proxy/testwiki/remote.png")
        assert resp.status_code == 200
        assert (img_dir / "remote.png").read_bytes() == b"REMOTE_IMG"

    @patch("server.http_requests.get")
    def test_returns_404_when_image_not_found(
        self, mock_get: MagicMock, client: FlaskClient, tmp_path: Path
    ) -> None:
        import server

        (tmp_path / "testwiki" / "images").mkdir(parents=True)
        server._wiki_static = str(tmp_path / "testwiki")
        api_resp = MagicMock()
        api_resp.json.return_value = {
            "query": {"pages": {"-1": {"title": "File:nope.png", "missing": ""}}}
        }
        mock_get.return_value = api_resp

        resp = client.get("/image-proxy/testwiki/nope.png")
        assert resp.status_code == 404

    @patch("server.http_requests.get")
    def test_returns_502_on_network_error(
        self, mock_get: MagicMock, client: FlaskClient, tmp_path: Path
    ) -> None:
        import server

        (tmp_path / "testwiki" / "images").mkdir(parents=True)
        server._wiki_static = str(tmp_path / "testwiki")
        mock_get.side_effect = Exception("connection refused")

        resp = client.get("/image-proxy/testwiki/fail.png")
        assert resp.status_code == 502

    def test_sanitizes_filename(self, client: FlaskClient, tmp_path: Path) -> None:
        import server

        img_dir = tmp_path / "testwiki" / "images"
        img_dir.mkdir(parents=True)
        (img_dir / "a_b.png").write_bytes(b"IMG")
        server._wiki_static = str(tmp_path / "testwiki")
        resp = client.get("/image-proxy/testwiki/a/b.png")
        assert resp.status_code == 200

    @patch("server.http_requests.get")
    def test_other_wiki_is_404(
        self, mock_get: MagicMock, client: FlaskClient, tmp_path: Path
    ) -> None:
        import server

        server._wiki_static = str(tmp_path / "testwiki")
        for wiki in ["otherwiki", ".."]:
            resp = client.get(f"/image-proxy/{wiki}/x.png")
            assert resp.status_code == 404
        mock_get.assert_not_called()
        assert sorted(p.name for p in tmp_path.iterdir()) == ["test.db"]


class TestStaticFiles:
    """/static/<wiki>/... comes from the hive; the rest is the shared static/."""

    def test_wiki_files_from_hive(self, client: FlaskClient, tmp_path: Path) -> None:
        import server

        wiki_static = tmp_path / "testwiki" / "static"
        (wiki_static / "images").mkdir(parents=True)
        (wiki_static / "theme.css").write_text(":root{}")
        (wiki_static / "images" / "a.png").write_bytes(b"PNG")
        server._wiki_static = str(wiki_static)

        assert client.get("/static/testwiki/theme.css").data == b":root{}"
        assert client.get("/static/testwiki/images/a.png").data == b"PNG"

    def test_shared_files_from_repo_static(
        self, client: FlaskClient, tmp_path: Path
    ) -> None:
        import server

        server._wiki_static = str(tmp_path)
        resp = client.get("/static/fandom-all.css")
        assert resp.status_code == 200
        with open(os.path.join(server.SHARED_STATIC, "fandom-all.css"), "rb") as f:
            assert resp.data == f.read()

    def test_missing_and_traversal_are_404(
        self, client: FlaskClient, tmp_path: Path
    ) -> None:
        import server

        (tmp_path / "secret.txt").write_text("nope")
        server._wiki_static = str(tmp_path / "static")
        (tmp_path / "static").mkdir()
        for url in [
            "/static/testwiki/missing.png",
            "/static/testwiki/../secret.txt",
            "/static/testwiki/%2e%2e/secret.txt",
        ]:
            assert client.get(url).status_code == 404


class TestApiSearch:
    def test_returns_json(self, client: FlaskClient) -> None:
        resp = client.get("/api/search?q=puzzle")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data[0]["title"] == "Gorogoa"

    @pytest.mark.parametrize("qs", ["/api/search?q=", "/api/search"])
    def test_empty_or_missing_q(self, client: FlaskClient, qs: str) -> None:
        assert json.loads(client.get(qs).data) == []

    def test_prefix_matching(self, client: FlaskClient) -> None:
        data = json.loads(client.get("/api/search?q=puzz").data)
        assert any(r["title"] == "Gorogoa" for r in data)

    def test_result_has_snip(self, client: FlaskClient) -> None:
        data = json.loads(client.get("/api/search?q=puzzle").data)
        assert "snip" in data[0]

    def test_title_match_sorted_first(self, client: FlaskClient) -> None:
        data = json.loads(client.get("/api/search?q=Gorogoa").data)
        assert data[0]["title"] == "Gorogoa"

    def test_response_content_type_is_json(self, client: FlaskClient) -> None:
        resp = client.get("/api/search?q=puzzle")
        assert resp.content_type == "application/json"

    def test_results_have_expected_keys(self, client: FlaskClient) -> None:
        data = json.loads(client.get("/api/search?q=Welcome").data)
        assert isinstance(data, list)
        for item in data:
            assert "title" in item
            assert "snip" in item

    @pytest.mark.parametrize(
        "q", ["test*", '"quoted"', "{}", "()", "a:b", "(hello", "he{llo"]
    )
    def test_search_special_chars_no_crash(self, client: FlaskClient, q: str) -> None:
        resp = client.get(f"/api/search?q={q}")
        assert resp.status_code == 200

    def test_case_insensitive(self, client: FlaskClient) -> None:
        data = json.loads(client.get("/api/search?q=PUZZLE").data)
        assert any(r["title"] == "Gorogoa" for r in data)

    def test_nonexistent_route_404(self, client: FlaskClient) -> None:
        assert client.get("/nonexistent").status_code == 404

    def test_api_limits_to_20_results(self, db_path: str) -> None:
        """API search uses limit=20 vs index's limit=100."""
        import sqlite3

        conn = sqlite3.connect(db_path)
        for i in range(25):
            conn.execute(
                "INSERT INTO pages (pageid,title,html,plaintext,categories,touched) VALUES (?,?,?,?,?,?)",
                (100 + i, f"Bulk{i}", "", f"commonword bulk{i}", "[]", ""),
            )
        conn.commit()
        conn.close()

        import server

        server._db_path = db_path
        with server.app.test_client() as c:
            data = json.loads(c.get("/api/search?q=commonword").data)
            assert len(data) <= 20

    @patch("server.http_requests.get")
    def test_proxy_search_during_page_scraping(
        self, mock_get: MagicMock, client: FlaskClient, tmp_path: Path
    ) -> None:
        import server

        status = tmp_path / ".test.status"
        status.write_text("pages")
        server._status_path = str(status)

        api_resp = MagicMock()
        api_resp.json.return_value = [
            "test",
            ["RemotePage", "RemoteOther"],
        ]
        mock_get.return_value = api_resp

        data = json.loads(client.get("/api/search?q=test").data)
        titles = [r["title"] for r in data]
        assert "RemotePage" in titles
        server._status_path = None

    def test_no_proxy_search_during_image_phase(
        self, client: FlaskClient, tmp_path: Path
    ) -> None:
        import server

        status = tmp_path / ".test.status"
        status.write_text("images")
        server._status_path = str(status)

        data = json.loads(client.get("/api/search?q=puzzle").data)
        # Should only have local results, no "(from Fandom)" snips
        assert all(r.get("snip", "") != "(from Fandom)" for r in data)
        server._status_path = None

    @patch("server.http_requests.get")
    def test_proxy_search_deduplicates(
        self, mock_get: MagicMock, client: FlaskClient, tmp_path: Path
    ) -> None:
        import server

        status = tmp_path / ".test.status"
        status.write_text("pages")
        server._status_path = str(status)

        api_resp = MagicMock()
        api_resp.json.return_value = ["gorogoa", ["Gorogoa"]]
        mock_get.return_value = api_resp

        data = json.loads(client.get("/api/search?q=gorogoa").data)
        titles = [r["title"] for r in data]
        assert titles.count("Gorogoa") == 1
        server._status_path = None


class TestSearchIndexMode:
    def test_uses_wikitext_index(self, client: FlaskClient, db_path: str) -> None:
        import scrape

        conn = scrape.init_db(db_path)
        conn.execute(
            "INSERT INTO wikitext_text (pageid, title, plaintext, filename, mtime_ns) "
            "VALUES (2, 'Gorogoa', 'infobox developer Jason Roberts', 'x', 0)"
        )
        scrape.set_fts_mode(conn, scrape.FTS_MEDIAWIKI)
        conn.close()

        data = json.loads(client.get("/api/search?q=roberts").data)
        assert [r["title"] for r in data] == ["Gorogoa"]
        assert "<mark>Roberts</mark>" in data[0]["snip"]
        # HTML text is no longer indexed in this mode
        assert json.loads(client.get("/api/search?q=puzzle").data) == []
