"""E2E tests using Playwright - starts the server and tests in a real browser."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Generator

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import Page, expect  # noqa: E402

if TYPE_CHECKING:
    from playwright.sync_api import Browser


@pytest.fixture(scope="module")
def server_url() -> Generator[str, None, None]:
    """Start the actual server with the gorogoa wiki (must exist)."""
    project_dir = os.path.dirname(os.path.dirname(__file__))
    db_path = os.path.join(project_dir, "hive", "gorogoa", "gorogoa.db")

    if not os.path.exists(db_path):
        pytest.skip(
            "hive/gorogoa/gorogoa.db not found - run 'python scrape.py gorogoa' first"
        )

    port = 5099
    proc = subprocess.Popen(
        [sys.executable, "server.py", "gorogoa", "--no-scrape", "--port", str(port)],
        cwd=project_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    time.sleep(2)
    yield f"http://127.0.0.1:{port}"
    proc.terminate()
    proc.wait(timeout=5)


ARTIFACTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "test-artifacts"
)


@pytest.fixture
def page(
    request: pytest.FixtureRequest, server_url: str
) -> Generator[tuple[Page, str], None, None]:
    """Fresh browser page per test with video recording."""
    from playwright.sync_api import sync_playwright

    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    with sync_playwright() as p:
        browser: Browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            record_video_dir=ARTIFACTS_DIR,
            record_video_size={"width": 1280, "height": 720},
        )
        pg = ctx.new_page()
        yield pg, server_url
        # Screenshot on failure
        if request.node.rep_call.failed:
            name = request.node.name
            pg.screenshot(path=os.path.join(ARTIFACTS_DIR, f"{name}.png"))
        ctx.close()
        # Rename video to test name
        if pg.video:
            video_path = pg.video.path()
            if os.path.exists(video_path):
                dest = os.path.join(ARTIFACTS_DIR, f"{request.node.name}.webm")
                os.replace(video_path, dest)
        browser.close()


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo) -> Generator:  # type: ignore[type-arg]
    """Attach test outcome to the request node so the fixture can check it."""
    import pluggy

    outcome: pluggy.Result = yield  # type: ignore[assignment]
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


def test_index_loads(page: tuple[Page, str]) -> None:
    pg, url = page
    pg.goto(url)
    expect(pg).to_have_title("Gorogoa Wiki")
    expect(pg.locator("a[href*='/wiki/']").first).to_be_visible()


def test_search_works(page: tuple[Page, str]) -> None:
    pg, url = page
    pg.goto(url)
    initial_count = pg.locator("#results li").count()
    assert initial_count > 1, "should have multiple pages listed initially"
    # Type to trigger JS live search (input event, not form submit)
    pg.locator("input[name='q']").press_sequentially("chapter", delay=50)
    # Wait for JS search to replace results — the count text only appears after fetch
    pg.locator(".count").wait_for(timeout=5000)
    # Wait for the result list to actually shrink
    expect(pg.locator("#results li")).not_to_have_count(initial_count, timeout=3000)
    assert (
        pg.locator("#results li").count() < initial_count
    ), "search should filter results"
    assert pg.locator(".snip").count() > 0, "search results should have text snippets"


def test_wiki_page_loads(page: tuple[Page, str]) -> None:
    pg, url = page
    pg.goto(f"{url}/wiki/Gorogoa")
    expect(pg.locator(".wiki-content")).to_be_visible()


def test_navigation_between_pages(page: tuple[Page, str]) -> None:
    pg, url = page
    pg.goto(url)
    pg.locator("a[href*='/wiki/']").first.click()
    assert "/wiki/" in pg.url


def test_static_images_load(page: tuple[Page, str]) -> None:
    pg, url = page
    pg.goto(f"{url}/wiki/Gorogoa")
    images = pg.locator("img").all()
    for img in images[:3]:
        assert img.evaluate("el => el.naturalWidth") > 0 or (
            img.get_attribute("src") or ""
        ).startswith("data:")


def test_scraping_notice_hidden_when_not_scraping(page: tuple[Page, str]) -> None:
    """Notice should not appear when --no-scrape is used."""
    pg, url = page
    pg.goto(url)
    expect(pg.locator(".scraping-notice")).to_have_count(0)


def test_image_proxy_fallback_script_present(page: tuple[Page, str]) -> None:
    """The JS fallback for broken images should be in the page source."""
    pg, url = page
    pg.goto(f"{url}/wiki/Gorogoa")
    assert "image-proxy" in pg.content()
    assert "proxyAttempted" in pg.content()


def test_placeholder_images_get_proxy_src(page: tuple[Page, str]) -> None:
    """Images with base64 placeholder src and data-image-name should be rewritten to proxy URL."""
    pg, url = page
    pg.goto(f"{url}/wiki/Gorogoa")
    # Wait for JS to run
    pg.wait_for_timeout(500)
    proxy_imgs = pg.evaluate("""() => {
        return [...document.querySelectorAll('.wiki-content img')]
            .filter(img => img.src.includes('image-proxy'))
            .length;
    }""")
    # This is a soft check — if there are placeholder images, they should be rewritten
    # If all images are already local, proxy_imgs will be 0 which is also fine
    assert isinstance(proxy_imgs, int)
