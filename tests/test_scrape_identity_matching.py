"""页面编号必须提供独立身份，不能以请求号或推荐区替代。"""
import pytest

from app.modules.ladysite import avmoo, javdb
from app.modules.ladysite.bus import Bus
from app.modules.ladysite.identity import same_code, validate_page


@pytest.mark.parametrize('actual,expected,matched', [
    ('ＳＳＩＳ － ００１', 'ssis-001', True),
    ('SSIS\t-\n001', 'SSIS-001-C', True),
    ('SSIS-001', 'SSIS-001-U', True),
    ('SSIS-0011', 'SSIS-001', False),
    ('SSIS-00001', 'SSIS-001', False),
    ('259LUXU-1234', 'LUXU-1234', False),
    ('SSIS-001', 'SSIS-01', False),
    ('', '', False),
    ('not a code', 'invalid', False),
])
def test_identity_matches_characters_but_preserves_number(actual, expected, matched):
    assert same_code(actual, expected) is matched


@pytest.mark.parametrize('search', [javdb.html_to_detail_url, avmoo.html_to_detail_url])
def test_search_does_not_match_two_invalid_codes(search):
    page = '''<div class="movie-list"><div class="item"><a href="/wrong">
    <date>2026-10-16</date><div class="video-title"><strong>invalid</strong></div>
    </a></div></div>'''
    assert search(page, '') == ''
    assert search(page, 'invalid') == ''


@pytest.mark.parametrize('search', [javdb.html_to_detail_url, avmoo.html_to_detail_url])
def test_search_selects_exact_normalized_base_code(search):
    def card(code, href):
        return f'''<div class="item"><a href="{href}"><date>{code}</date>
        <div class="video-title"><strong>{code}</strong></div></a></div>'''
    page = '<div class="movie-list">' + ''.join([
        card('SSIS-0011', '/similar'), card('SSIS-00001', '/padded'),
        card('ＳＳＩＳ － ００１', '/correct'),
    ]) + '</div>'
    assert search(page, 'ssis-001-C') == '/correct'


@pytest.mark.parametrize('parser', [javdb.html_to_code, Bus(host="https://example.test").html_to_code])
def test_detail_cannot_fill_missing_identity_from_request(parser):
    assert parser('<h2 class="title is-4">Unknown</h2><h3>Unknown</h3>', 'SSIS-001') is None


@pytest.mark.parametrize('actual', ['SSIS-002', 'SSIS-0011', 'SSIS-00001', ''])
def test_javdb_rejects_different_or_missing_main_number(actual):
    page = f'''<h2 class="title is-4"><strong>{actual}</strong>
    <strong class="current-title">Test title</strong></h2>
    <div class="movie-list"><strong>SSIS-001</strong></div>'''
    assert javdb.html_to_code(page, 'SSIS-001') is None


@pytest.mark.parametrize('actual', ['SSIS-002', 'SSIS-0011', 'SSIS-00001', ''])
def test_bus_rejects_different_or_invalid_identifier_despite_matching_title(actual):
    page = f'''<h3>SSIS-001 Test title</h3>
    <div class="info"><p>識別碼: {actual}</p></div>'''
    assert Bus(host="https://example.test").html_to_code(page, 'SSIS-001') is None


@pytest.mark.parametrize('identifier', ['', 'SSIS-002', 'SSIS-00001'])
def test_invalid_explicit_identity_overrides_matching_title_and_url(identifier):
    page = f'''<h1>SSIS-001 Title</h1>
    <div class="info"><p>識別碼: {identifier}</p></div>
    <aside><h3>SSIS-001 Recommendation</h3></aside>'''
    assert not validate_page(page, 'https://example.test/SSIS-001', 'SSIS-001')


def test_identity_accepts_table_value_without_colon_and_full_width_header_value():
    page = '<table><tr><td>品番</td><td>ＳＳＩＳ － ００１</td></tr></table>'
    assert validate_page(page, 'https://example.test/v/hash', 'SSIS-001-C')


def test_number_in_generic_title_does_not_use_filename_padding_heuristics():
    assert not validate_page('<h1>作品 SSIS-00001</h1>', 'https://example.test/v/hash', 'SSIS-001')

