import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

source = Path(os.environ["AB_SOURCE"]).resolve() / "backend" / "src"
sys.path.insert(0, str(source))
# Production gets this generated module from the official image build.
sys.modules["module.__version__"] = SimpleNamespace(VERSION="4.0.0")


@pytest.fixture(autouse=True)
def isolated_host(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    from module import conf
    from module.database import combine
    from module.downloader.download_client import _reset_client_cache
    from module.models.config import Config
    from module.plugin import host
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from sqlmodel import SQLModel, create_engine

    # Imports register AB's real schema; each test gets an isolated file DB.
    engine = create_engine(f"sqlite:///{tmp_path / 'host.db'}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()
    async_engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'host.db'}", poolclass=NullPool
    )
    monkeypatch.setattr(
        combine, "async_session_factory", async_sessionmaker(async_engine, expire_on_commit=False)
    )
    defaults = Config()
    for key in Config.model_fields:
        monkeypatch.setattr(conf.settings, key, getattr(defaults, key))
    conf.settings.rss_parser.engine = "tokenizer"
    monkeypatch.setattr(host, "_registry", None)
    monkeypatch.setattr(host, "_runner", None)
    _reset_client_cache()
    yield
    _reset_client_cache()


@pytest_asyncio.fixture
async def plugin(tmp_path, monkeypatch):
    from ab_sdk import points
    from ab_sdk.downloader import AddResult
    from ab_sdk.testing import create_plugin
    from module.conf import settings
    from module.downloader.client.qb_downloader import QbDownloader
    from module.plugin import host
    from module.plugin.registry import ProviderEntry

    from review_gate.bridge40 import PROVIDER
    from review_gate.plugin import SubscriptionReview

    settings.plugins.instances[0].provider = PROVIDER
    settings.plugins.instances[0].options.update(host="127.0.0.1:1", path="/downloads")
    send = AsyncMock(return_value=AddResult.ADDED)
    monkeypatch.setattr(QbDownloader, "auth", AsyncMock(return_value=True))
    monkeypatch.setattr(QbDownloader, "logout", AsyncMock())
    monkeypatch.setattr(QbDownloader, "add_torrents", send)
    obj, _ = create_plugin(
        SubscriptionReview, plugin_id="subscription-review", data_dir=tmp_path / "plugin"
    )
    await obj.setup()
    host.get_registry().add_provider(
        points.DOWNLOADER, ProviderEntry("subscription-review", PROVIDER, obj.downloader)
    )
    obj.test_send = send
    yield obj
    await obj.teardown()


@pytest_asyncio.fixture
async def rule():
    from module.database import Database
    from module.models import Bangumi

    r = Bangumi(
        official_title="示例番剧",
        title_raw="Example Show",
        season=1,
        group_name="Group",
        filter="",
        deleted=False,
    )
    async with Database() as db:
        db.add(r)
        await db.commit()
    return r


def release(ep=1, variant="HEVC", url=None):
    from module.models import Torrent

    return Torrent(
        name=f"[Group] Example Show - {ep:02} [WebRip 1080p {variant}][简繁内封]",
        url=url or f"magnet:?xt=urn:btih:{ep:040x}&dn={variant}",
    )


async def approve(plugin, rule, **kwargs):
    from review_gate.policy import Policy

    await plugin.store.register(plugin.bridge.rule_data(rule))
    entry = next(r for r in await plugin.store.rules() if r["id"] == rule.id)
    policy = Policy(include=["HEVC", "1080p", "内封"], mode="backlog", **kwargs)
    await plugin.store.set_policy(rule.id, policy, entry["revision"])
