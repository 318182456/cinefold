from types import SimpleNamespace

import pytest

from app.core import config
from app.core.config import Settings
from app.modules import ptsite
from app.modules.ptsite.mteam import MTeam
from app.modules.ptsite.rousi import Rousi
from app.modules.ptsite.ptt import PTT
from app.modules.ptsite.nicept import NicePT
from app.schemas.torrent import Torrent


@pytest.fixture
def settings(monkeypatch):
    settings = Settings(mteam_api_key='m-secret', rousi_apikey='r-secret', ptt_cookie='ptt-secret', nicept_cookie='nice-secret')
    monkeypatch.setattr(config, '_settings', settings)
    return settings


def test_disabled_by_default_but_credentials_kept(settings):
    assert [site.name for site in ptsite.get_sites()] == ['MTeam', 'Rousi']
    assert not PTT().enabled and not NicePT().enabled
    assert settings.ptt_cookie == 'ptt-secret' and settings.nicept_cookie == 'nice-secret'
    settings.ptt_enabled = settings.nicept_enabled = True
    assert [site.name for site in ptsite.get_sites()] == ['MTeam', 'Rousi', 'PTT', 'NicePT']


@pytest.mark.parametrize('cls,key', [(MTeam,'mteam_enabled'), (Rousi,'rousi_enabled'), (PTT,'ptt_enabled'), (NicePT,'nicept_enabled')])
def test_existing_instances_stop_search_check_and_seed_fetch(settings, monkeypatch, cls, key):
    setattr(settings, key, True)
    site = cls()
    assert site.enabled
    setattr(settings, key, False)
    def no_request(*args, **kwargs):
        pytest.fail('disabled site must not issue requests')
    monkeypatch.setattr('httpx.Client', no_request)
    assert not site.enabled
    assert site.search('SONE-725') == []
    assert '停用' in site.check_status()[1]
    assert site.download_seed(Torrent(site=site.name, download_url='https://site.test/seed')) is None


def test_disabling_filters_cached_results_and_blocks_url_fallback(settings, monkeypatch):
    from app import services
    monkeypatch.setattr(services, '_search_pt_cached', lambda *a, **kw: [Torrent(site='PTT'), Torrent(site='NicePT'), Torrent(site='MTeam')])
    assert [t.site for t in services.search_torrents('SONE-725', use_filter=False)] == ['MTeam']
    def fail(*a):
        pytest.fail('must not push disabled source to downloader')
    client = SimpleNamespace(add_torrent_by_magnet=fail, add_torrent=fail)
    for url in ('magnet:?xt=urn:btih:abc', 'https://site.test/download.php?id=1'):
        assert services._push_to_client(client, Torrent(site='PTT', download_url=url), 'SONE-725') is None


def test_reenabling_uses_new_cache_partition(settings, monkeypatch):
    from app import services
    keys = []
    monkeypatch.setattr(services, 'get_rank_cache', lambda ns, key, **kw: keys.append(key) or '[]')
    assert services._search_pt_cached('SONE-725') == []
    settings.ptt_enabled = True
    assert services._search_pt_cached('SONE-725') == []
    assert keys[0] != keys[1]


def test_enabled_flags_survive_settings_save_reload(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ENV_FILE', tmp_path / 'app.env')
    monkeypatch.setenv('PTT_COOKIE', 'preserved')
    monkeypatch.setenv('PTT_ENABLED', 'true')
    monkeypatch.setenv('NICEPT_ENABLED', 'false')
    config.save_settings({'PTT_ENABLED': False, 'NICEPT_ENABLED': True})
    loaded = config.load_settings()
    assert loaded.ptt_enabled is False and loaded.nicept_enabled is True
    assert loaded.ptt_cookie == 'preserved'
