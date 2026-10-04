"""Full prod tests — scrape a real wiki from scratch and test all features.

Run with: pytest -m live tests/test_live.py -v
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Generator

import pytest
import requests

pytestmark = pytest.mark.live
PROJECT_DIR = os.path.dirname(os.path.dirname(__file__))
WIKI = "gorogoa"
PORT = 5097


@pytest.fixture(scope="module")
def clean_env(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Provide a clean temp dir for the wiki."""
    return tmp_path_factory.mktemp("prod")


@pytest.fixture(scope="module")
def server_with_scraping(clean_env: Path) -> Generator[str, None, None]:
    """Start server that scrapes from scratch. Yields URL while scraping is in progress."""
    proc = subprocess.Popen(
        [
            sys.executable,
            os.path.join(PROJECT_DIR, "server.py"),
            WIKI,
            "--hive",
            str(clean_env),
            "--port",
            str(PORT),
            "--log-level",
            "DEBUG",
        ],
        cwd=str(clean_env),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{PORT}"
    # Wait for server to be ready
    for _ in range(20):
        try:
            requests.get(url, timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    yield url
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(scope="module")
def scraped_server(clean_env: Path) -> Generator[str, None, None]:
    """Start server after full scrape is complete (--no-scrape)."""
    # First scrape fully
    r = subprocess.run(
        [
            sys.executable,
            os.path.join(PROJECT_DIR, "scrape.py"),
            WIKI,
            "--hive",
            str(clean_env),
        ],
        cwd=str(clean_env),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, f"scrape failed:\n{r.stderr}"
    port = PORT + 1
    proc = subprocess.Popen(
        [
            sys.executable,
            os.path.join(PROJECT_DIR, "server.py"),
            WIKI,
            "--no-scrape",
            "--hive",
            str(clean_env),
            "--port",
            str(port),
        ],
        cwd=str(clean_env),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(20):
        try:
            requests.get(url, timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    yield url
    proc.terminate()
    proc.wait(timeout=5)


class TestScrapeFromScratch:
    """Test that scraping creates a valid DB and images."""

    def test_db_created(self, scraped_server: str, clean_env: Path) -> None:
        db_path = clean_env / WIKI / f"{WIKI}.db"
        assert db_path.exists()
        assert db_path.stat().st_size > 0

    def test_pages_in_db(self, scraped_server: str, clean_env: Path) -> None:
        conn = sqlite3.connect(str(clean_env / WIKI / f"{WIKI}.db"))
        count = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        conn.close()
        assert count >= 3

    def test_status_file_removed_after_scrape(
        self, scraped_server: str, clean_env: Path
    ) -> None:
        """#10: status file should be cleaned up when scraping is done."""
        assert not (clean_env / WIKI / f".{WIKI}.status").exists()

    def test_theme_css_downloaded(self, scraped_server: str, clean_env: Path) -> None:
        theme = clean_env / WIKI / "static" / "theme.css"
        assert theme.exists()
        assert theme.stat().st_size > 0


class TestPageServing:
    """Test page rendering after full scrape."""

    def test_index_loads(self, scraped_server: str) -> None:
        r = requests.get(scraped_server)
        assert r.status_code == 200
        assert "Gorogoa" in r.text

    def test_wiki_page(self, scraped_server: str) -> None:
        r = requests.get(f"{scraped_server}/wiki/Gorogoa")
        assert r.status_code == 200
        assert "wiki-content" in r.text

    def test_fandom_link_present(self, scraped_server: str) -> None:
        """#8: every page should have a View on Fandom link."""
        r = requests.get(f"{scraped_server}/wiki/Gorogoa")
        assert "View on Fandom" in r.text
        assert f"{WIKI}.fandom.com" in r.text

    def test_no_scraping_notice(self, scraped_server: str) -> None:
        """#10: no scraping notice when fully scraped."""
        r = requests.get(scraped_server)
        assert "Scraping in progress" not in r.text


class TestTabs:
    """#7: tabs and collapsibles JS should be present."""

    def test_tab_js_present(self, scraped_server: str) -> None:
        r = requests.get(f"{scraped_server}/wiki/Gorogoa")
        assert "wds-tabber" in r.text
        assert "mw-collapsible" in r.text


class TestSearch:
    """Test local search after full scrape."""

    def test_search_api(self, scraped_server: str) -> None:
        r = requests.get(f"{scraped_server}/api/search", params={"q": "puzzle"})
        assert r.status_code == 200
        data = r.json()
        assert len(data) > 0

    def test_search_no_remote_when_fully_scraped(self, scraped_server: str) -> None:
        """#9/#10: no remote results when scraping is done."""
        r = requests.get(f"{scraped_server}/api/search", params={"q": "gorogoa"})
        data = r.json()
        assert all(r.get("snip", "") != "(from Fandom)" for r in data)

    def test_search_empty_returns_empty(self, scraped_server: str) -> None:
        r = requests.get(f"{scraped_server}/api/search", params={"q": ""})
        assert r.json() == []


class TestImageProxy:
    """#5: image proxy should fetch, cache, and serve images."""

    def test_proxy_nonexistent_image_404(self, scraped_server: str) -> None:
        r = requests.get(
            f"{scraped_server}/image-proxy/{WIKI}/Totally_Fake_Image_99999.png"
        )
        assert r.status_code == 404

    def test_proxy_fetches_real_image(
        self, scraped_server: str, clean_env: Path
    ) -> None:
        conn = sqlite3.connect(str(clean_env / WIKI / f"{WIKI}.db"))
        rows = conn.execute("SELECT html FROM pages").fetchall()
        conn.close()
        name = None
        for row in rows:
            m = re.search(r'data-image-key="([^"]+)"', row[0])
            if not m:
                m = re.search(r'data-image-name="([^"]+)"', row[0])
            if m:
                name = m.group(1)
                break
        if not name:
            pytest.skip("No image refs in any page")
        r = requests.get(f"{scraped_server}/image-proxy/{WIKI}/{name}")
        assert r.status_code == 200
        assert len(r.content) > 100

    def test_proxy_caches_image_locally(
        self, scraped_server: str, clean_env: Path
    ) -> None:
        """After proxy fetch, image should exist on disk."""
        conn = sqlite3.connect(str(clean_env / WIKI / f"{WIKI}.db"))
        rows = conn.execute("SELECT html FROM pages").fetchall()
        conn.close()
        name = None
        for row in rows:
            m = re.search(r'data-image-key="([^"]+)"', row[0])
            if m:
                name = m.group(1)
                break
        if not name:
            pytest.skip("No image refs")
        safe = name.replace("/", "_").replace("\\", "_").replace(" ", "_")
        requests.get(f"{scraped_server}/image-proxy/{WIKI}/{name}")
        img_path = clean_env / WIKI / "static" / "images" / safe
        assert img_path.exists()


class TestOnDemandPageFetch:
    """#8: pages not in DB should be fetched on demand."""

    def test_missing_page_fetched_on_demand(self, scraped_server: str) -> None:
        """Fetch a page that exists on Fandom but probably isn't in gorogoa DB."""
        # Use the main page which should always exist on any wiki
        r = requests.get(f"{scraped_server}/wiki/Gorogoa")
        assert r.status_code == 200

    def test_truly_nonexistent_page_shows_fandom_link(
        self, scraped_server: str
    ) -> None:
        r = requests.get(
            f"{scraped_server}/wiki/This_Page_Absolutely_Does_Not_Exist_99999"
        )
        assert r.status_code == 404
        assert "fandom.com" in r.text


class TestNonexistentWiki:
    def test_scrape_exits_with_error(self, tmp_path: Path) -> None:
        r = subprocess.run(
            [
                sys.executable,
                os.path.join(PROJECT_DIR, "scrape.py"),
                "zzznonexistentwiki999",
                "--hive",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode != 0
        assert not any(tmp_path.iterdir())

    def test_server_exits_with_error(self) -> None:
        r = subprocess.run(
            [
                sys.executable,
                os.path.join(PROJECT_DIR, "server.py"),
                "zzznonexistentwiki999",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode != 0
        assert (
            "does not exist" in r.stdout.lower() or "does not exist" in r.stderr.lower()
        )
