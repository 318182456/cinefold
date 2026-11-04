"""前端覆盖目录不能把新镜像钉在旧界面；验证真实文件的保留与回退。"""
from pathlib import Path

import pytest

from app.core import overlay


@pytest.fixture
def packages(tmp_path, monkeypatch):
    image, bundled = tmp_path / 'image', tmp_path / 'html'
    current = tmp_path / 'data' / 'updates' / 'web' / 'current'
    for path in (image, bundled, current):
        path.mkdir(parents=True)
    (image / 'VERSION').write_text('0.1.55')
    (bundled / 'assets').mkdir()
    (bundled / 'assets' / 'main.js').write_text('new frontend JS')
    (bundled / 'assets' / 'style.css').write_text('new frontend CSS')
    (bundled / 'index.html').write_text('new light frontend<script src="/assets/main.js"></script><link href="/assets/style.css">')
    (current / 'VERSION').write_text('0.1.54')
    (current / 'index.html').write_text('old dark frontend')
    backup = current.parent / 'backup'
    backup.mkdir()
    (backup / 'index.html').write_text('existing rollback')
    monkeypatch.setattr(overlay, 'web_current', lambda: current)
    return image, bundled, current, backup


@pytest.mark.parametrize('current_version', ['0.1.54', '0.1.55'])
def test_new_image_retires_stale_web_without_deleting_either_backup(packages, current_version):
    image, bundled, current, backup = packages
    (current / 'VERSION').write_text(current_version)
    retired = overlay.retire_stale_web(image, bundled)
    assert retired and retired.parent == current.parent
    assert not current.exists()
    assert (retired / 'index.html').read_text() == 'old dark frontend'
    assert (backup / 'index.html').read_text() == 'existing rollback'
    assert 'new light frontend' in (bundled / 'index.html').read_text()
    assert overlay.retire_stale_web(image, bundled) is None


def test_hot_updated_web_newer_than_image_keeps_serving(packages):
    image, bundled, current, _ = packages
    (current / 'VERSION').write_text('0.1.56')
    assert overlay.retire_stale_web(image, bundled) is None
    assert (current / 'index.html').read_text() == 'old dark frontend'


@pytest.mark.parametrize('broken', ['missing_index', 'bad_overlay_version', 'bad_image_version', 'bad_web_stamp', 'missing_js', 'missing_css', 'unsafe_asset', 'missing_assets'])
def test_uncertain_installation_is_left_intact(packages, broken):
    image, bundled, current, _ = packages
    if broken == 'missing_index': (bundled / 'index.html').unlink()
    elif broken == 'bad_overlay_version': (current / 'VERSION').write_text('../unsafe')
    elif broken == 'bad_image_version': (image / 'VERSION').write_text('unknown')
    elif broken == 'bad_web_stamp': (bundled / 'VERSION').write_text('broken')
    elif broken == 'missing_js': (bundled / 'assets' / 'main.js').unlink()
    elif broken == 'missing_css': (bundled / 'assets' / 'style.css').unlink()
    elif broken == 'unsafe_asset': (bundled / 'index.html').write_text('<script src="/assets/../../outside.js"></script>')
    else: (bundled / 'index.html').write_text('incomplete entrypoint')
    assert overlay.retire_stale_web(image, bundled) is None
    assert (current / 'index.html').exists()


def test_bundled_frontend_stamp_takes_precedence_over_backend_version(packages):
    image, bundled, current, _ = packages
    (bundled / 'VERSION').write_text('0.1.53')
    assert overlay.retire_stale_web(image, bundled) is None
    assert current.exists()


def test_failed_rename_preserves_live_web(packages, monkeypatch):
    image, bundled, current, _ = packages
    def fail(*args): raise PermissionError('locked')
    monkeypatch.setattr(Path, 'rename', fail)
    assert overlay.retire_stale_web(image, bundled) is None
    assert (current / 'index.html').exists()


def test_activate_reconciles_frontend_even_without_backend_overlay(packages, monkeypatch):
    image, bundled, current, _ = packages
    original = overlay.retire_stale_web
    monkeypatch.setattr(overlay, 'image_root', lambda: image)
    monkeypatch.setattr(overlay, 'backend_current', lambda: current.parent / 'absent-backend')
    monkeypatch.setattr(overlay, 'retire_stale_web', lambda root: original(root, bundled))
    assert overlay.activate() == '0.1.55'
    assert not current.exists()
    assert any(p.name.startswith('retired-') for p in current.parent.iterdir())
