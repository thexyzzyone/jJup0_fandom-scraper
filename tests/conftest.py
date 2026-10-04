from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Generator

import pytest
from flask.testing import FlaskClient

import scrape
import server

TEST_PAGES: list[tuple[int, str, str, str, str, str]] = [
    (1, "Main Page", "<p>Welcome</p>", "Welcome", "[]", ""),
    (
        2,
        "Gorogoa",
        "<p>puzzle game</p>",
        "puzzle game",
        json.dumps(["Games"]),
        "2024-01-01T00:00:00Z",
    ),
    (
        3,
        "Old Name",
        '<div class="redirectMsg"><a href="/wiki/Gorogoa">x</a></div>',
        "",
        "[]",
        "",
    ),
    (4, "Special & Characters", "<p>ampersand test</p>", "ampersand test", "[]", ""),
    (5, "Page With Spaces", "<p>spaced</p>", "spaced", "[]", ""),
    (6, "Über Page", "<p>unicode content</p>", "unicode content", "[]", ""),
    (
        7,
        "FTS Test",
        "<p>xylophone zebra</p>",
        "xylophone zebra",
        json.dumps(["Music", "Animals"]),
        "",
    ),
    (8, "Empty Content", "", "", "[]", ""),
]


@pytest.fixture
def db(tmp_path: Path) -> Generator[sqlite3.Connection, None, None]:
    conn = scrape.init_db(str(tmp_path / "test.db"))
    yield conn
    conn.close()


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "test.db")
    conn = scrape.init_db(path)
    conn.executemany(
        "INSERT INTO pages (pageid,title,html,plaintext,categories,touched) VALUES (?,?,?,?,?,?)",
        TEST_PAGES,
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def client(db_path: str) -> Generator[FlaskClient, None, None]:
    server._db_path = db_path
    server._wiki_name = "Test Wiki"
    server._wiki_static = None
    server.app.config["TESTING"] = True
    server.app.config["HAS_FULL_CSS"] = False
    server.app.config["WIKI_SLUG"] = "testwiki"
    with server.app.test_client() as c:
        yield c
