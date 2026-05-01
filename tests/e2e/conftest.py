"""Shared fixtures for the E2E browser test suite.

Session-scoped fixtures boot a real `compendium serve` subprocess against a
tmp_path SQLite file, seed it with librarian + patron + works, and yield a
base URL. Function-scoped helpers return authenticated Playwright pages.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import types
import urllib.error
import urllib.request
from pathlib import Path

import bcrypt
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from compendium.config.seed import seed_defaults
from compendium.domain.models import AppUser, Base, Patron, PatronCategory, Role
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.catalog import CatalogService

from tests.helpers import setup_sqlite_fts


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _hash_pw(pw: str) -> str:
    # Use rounds=4 for speed in tests; bcrypt embeds the cost in the hash.
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(4)).decode()


@pytest.fixture(scope="session")
def e2e_db_path(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("e2e_db") / "compendium.db"


@pytest.fixture(scope="session")
def e2e_seed(e2e_db_path) -> types.SimpleNamespace:
    """Seed the E2E SQLite database and return handles the tests need."""
    url = f"sqlite:///{e2e_db_path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA synchronous=NORMAL")
        finally:
            cur.close()

    Base.metadata.create_all(engine)
    setup_sqlite_fts(engine)

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = factory()

    seed_defaults(s)
    s.commit()

    admin_role = s.query(Role).filter_by(name="Administrator").one()
    patron_role = s.query(Role).filter_by(name="Patron").one()
    adult_cat = s.query(PatronCategory).filter_by(is_default=True).first()

    # Librarian user (Administrator role = wildcard permissions)
    librarian_user = AppUser(
        username="librarian",
        password_hash=_hash_pw("librarian-pw-1"),
        role_id=admin_role.id,
    )
    s.add(librarian_user)
    s.flush()

    # Patron user + linked patron record (card 00000001)
    patron_user = AppUser(
        username="patron1",
        password_hash=_hash_pw("patron-pw-1"),
        role_id=patron_role.id,
    )
    s.add(patron_user)
    s.flush()

    patron_record = Patron(
        library_card_number="00000001",
        full_name="Test Patron",
        user_id=patron_user.id,
        category_id=adult_cat.id if adult_cat else None,
    )
    s.add(patron_record)
    s.flush()

    # Card-only patron (no user account) for kiosk test
    kiosk_patron = Patron(
        library_card_number="00000002",
        full_name="Kiosk Test Patron",
        category_id=adult_cat.id if adult_cat else None,
    )
    s.add(kiosk_patron)
    s.flush()

    # Two works with items so the catalog and detail pages have content
    catalog_svc = CatalogService(
        work_repo=SqlWorkRepository(s),
        item_repo=SqlItemRepository(s),
        creator_repo=SqlCreatorRepository(s),
        branch_repo=SqlBranchRepository(s),
        media_type_repo=SqlMediaTypeRepository(s),
    )
    work_a, item_a = catalog_svc.add_manual("book", "Test Work Alpha")
    work_b, item_b = catalog_svc.add_manual("book", "Test Work Beta")

    s.commit()
    s.close()
    engine.dispose()

    return types.SimpleNamespace(
        librarian_username="librarian",
        librarian_password="librarian-pw-1",
        patron_username="patron1",
        patron_password="patron-pw-1",
        patron_card="00000001",
        kiosk_card="00000002",
        work_a_id=work_a.id,
        work_b_id=work_b.id,
        item_a_barcode=item_a.barcode,
    )


@pytest.fixture(scope="session")
def e2e_server(e2e_db_path, e2e_seed, tmp_path_factory):
    """Start `compendium serve` as a subprocess; yield the base URL."""
    port = _find_free_port()
    db_url = f"sqlite:///{e2e_db_path}"
    log_path = tmp_path_factory.mktemp("e2e_logs") / "server.log"

    env = {
        **os.environ,
        "COMPENDIUM_DATABASE_URL": db_url,
        "COMPENDIUM_JWT_SECRET_KEY": "e2e-test-secret-not-for-production-x7k2",
        "COMPENDIUM_ALLOW_INSECURE_JWT": "1",
        "COMPENDIUM_SECURE_COOKIES": "false",
    }

    with open(log_path, "w") as log_f:
        proc = subprocess.Popen(
            [sys.executable, "-m", "compendium", "serve", "--port", str(port)],
            env=env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
        )

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30.0

    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/ui/login", timeout=1) as resp:
                if resp.status == 200:
                    break
        except (urllib.error.URLError, OSError):
            pass
        if proc.poll() is not None:
            with open(log_path) as f:
                pytest.fail(f"Server exited early. Logs:\n{f.read()}")
        time.sleep(0.3)
    else:
        proc.kill()
        with open(log_path) as f:
            pytest.fail(f"Server didn't start in 30s. Logs:\n{f.read()}")

    yield base_url

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    """Add Chromium flags so getUserMedia works headlessly without camera hardware."""
    return {
        **browser_type_launch_args,
        "args": [
            *browser_type_launch_args.get("args", []),
            "--use-fake-device-for-media-stream",
            "--use-fake-ui-for-media-stream",
        ],
    }


@pytest.fixture
def librarian_page(page, e2e_server, e2e_seed):
    """Return a Playwright page already authenticated as the librarian."""
    page.goto(f"{e2e_server}/ui/login")
    page.fill("input[name=username]", e2e_seed.librarian_username)
    page.fill("input[name=password]", e2e_seed.librarian_password)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    return page


@pytest.fixture
def patron_page(page, e2e_server, e2e_seed):
    """Return a Playwright page already authenticated as the patron user."""
    page.goto(f"{e2e_server}/ui/login")
    page.fill("input[name=username]", e2e_seed.patron_username)
    page.fill("input[name=password]", e2e_seed.patron_password)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    return page
