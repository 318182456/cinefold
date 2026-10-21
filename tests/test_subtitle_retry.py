"""Exercise backoff and rotation through the real automatic subtitle service."""
from datetime import datetime, timedelta
import json

import pytest
from sqlalchemy import select, update

from app.core.config import get_settings
from app.database.models import Cache, MediaLink
from app.modules import subtitle
from app.modules.ladysite.transport import report_response
from app.modules.subtitle.base import SubtitleItem
from app.services import subtitle as service, metadataretry
from tests.test_metadata_retry import db
from tests.test_subtitle import SRT_ZH


@pytest.fixture(autouse=True)
def enable(monkeypatch):
    monkeypatch.setattr(get_settings(), "subtitle_enabled", True)


def register(db, tmp_path, code, name=None):
    path = tmp_path / (name or code) / f"{code}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video")
    with db.begin() as session:
        session.add(MediaLink(code=code, link_path=str(path), source_path=str(path), inode=1, device=1, has_subtitle=False))
    return path


def retry_row(db, code):
    with db() as session:
        return session.scalar(select(Cache).where(Cache.namespace == service.RETRY_NAMESPACE, Cache.key == code))


def test_thirty_failures_do_not_starve_the_next_batch_and_expiry_allows_retry(db, tmp_path, monkeypatch):
    codes = [f"ABC-{number:03}" for number in range(40)]
    for code in codes:
        register(db, tmp_path, code)
    calls = []
    def missing(code):
        calls.append(code)
        report_response("successful search without subtitles")
        return None
    monkeypatch.setattr(subtitle, "search", missing)
    assert service.fill_lack_subtitles(30) == 0
    assert calls == codes[:30]
    assert service._codes_lacking_subtitle(30) == codes[30:]
    assert service.fill_lack_subtitles(30) == 0
    assert calls == codes
    assert service._codes_lacking_subtitle(30) == []
    with db.begin() as session:
        session.execute(update(Cache).where(Cache.namespace == service.RETRY_NAMESPACE, Cache.key == codes[0]).values(create_time=datetime.now() - timedelta(seconds=1)))
    assert service._codes_lacking_subtitle(30) == [codes[0]]
    assert service.fill_lack_subtitles(30) == 0
    assert calls == codes + [codes[0]]


@pytest.mark.parametrize("failed,reason,seconds", [(False, "empty", 21600), (True, "error", 600)])
def test_persistent_cooldown_distinguishes_network_failure(db, tmp_path, monkeypatch, failed, reason, seconds):
    register(db, tmp_path, "ABC-101")
    def missing(code):
        report_response("" if failed else "successful empty results")
        return None
    monkeypatch.setattr(subtitle, "search", missing)
    assert service.fetch_for_code("ABC-101") == 0
    row = retry_row(db, "ABC-101")
    assert json.loads(row.content)["reason"] == reason
    assert seconds - 5 < (row.create_time - datetime.now()).total_seconds() <= seconds
    monkeypatch.setattr(subtitle, "search", lambda code: pytest.fail("cooldown must avoid another query"))
    # All admission state is in the database; a fresh session sees the same skip.
    assert service.fetch_for_code("ABC-101") == 0
    assert service._codes_lacking_subtitle(30) == []


def test_new_entries_precede_expired_retries_then_older_retries_go_first(db, tmp_path):
    for code in ("ABC-101", "ABC-102", "ABC-103"):
        register(db, tmp_path, code)
    with db.begin() as session:
        session.add_all([Cache(namespace=service.RETRY_NAMESPACE, key="ABC-101", create_time=datetime.now() - timedelta(minutes=1)),
                         Cache(namespace=service.RETRY_NAMESPACE, key="ABC-102", create_time=datetime.now() - timedelta(minutes=5))])
    assert service._codes_lacking_subtitle(3) == ["ABC-103", "ABC-102", "ABC-101"]


def test_manual_request_bypasses_cooldown_and_global_switch_and_clears_backoff(db, tmp_path, monkeypatch):
    video = register(db, tmp_path, "ABC-101")
    metadataretry.defer("ABC-101", retryable=False, namespace=service.RETRY_NAMESPACE)
    monkeypatch.setattr(get_settings(), "subtitle_enabled", False)
    monkeypatch.setattr(subtitle, "search", lambda code: SubtitleItem(code=code, content=SRT_ZH))
    assert service.fetch_for_code("ABC-101", manual=True) == 1
    assert video.with_name("ABC-101.zh-CN.srt").read_text(encoding="utf-8") == SRT_ZH
    assert retry_row(db, "ABC-101") is None


def test_existing_sidecar_in_one_location_does_not_hide_another_missing_location(db, tmp_path):
    first = register(db, tmp_path, "ABC-101", "first")
    register(db, tmp_path, "ABC-101", "second")
    first.with_name("ABC-101.zh-CN.srt").write_text(SRT_ZH, encoding="utf-8")
    assert service._codes_lacking_subtitle(30) == ["ABC-101"]


def test_failed_file_write_uses_short_backoff_not_missing_subtitle_backoff(db, tmp_path, monkeypatch):
    register(db, tmp_path, "ABC-101")
    def found(code):
        report_response("successful subtitle download")
        return SubtitleItem(code=code, content=SRT_ZH)
    monkeypatch.setattr(subtitle, "search", found)
    monkeypatch.setattr(service, "_write_beside", lambda *args: False)
    assert service.fetch_for_code("ABC-101") == 0
    assert json.loads(retry_row(db, "ABC-101").content)["reason"] == "error"


@pytest.mark.parametrize("key", ["xunlei", "javsub", "subtitlecat", "subtitlegh"])
@pytest.mark.parametrize("suffix", ["-C", "-UC", "-U", "-UCH"])
def test_all_network_providers_receive_base_code_but_output_keeps_edition(monkeypatch, key, suffix):
    requests = []
    class Source:
        def search(self, code):
            requests.append(code)
            return SubtitleItem(code=code, content=SRT_ZH)
    monkeypatch.setattr(subtitle, "SUBTITLE_SITES", (key,))
    monkeypatch.setattr(subtitle, "_build", lambda name: Source())
    result = subtitle.search("ABC-101" + suffix)
    assert requests == ["ABC-101"]
    assert result.code == "ABC-101" + suffix


def test_local_provider_keeps_original_edition(monkeypatch):
    queries = []
    class Local:
        def search(self, code): queries.append(code)
    monkeypatch.setattr(subtitle, "SUBTITLE_SITES", ("subtitlelocal",))
    monkeypatch.setattr(subtitle, "_build", lambda name: Local())
    assert subtitle.search("ABC-101-UC") is None
    assert queries == ["ABC-101-UC"]
