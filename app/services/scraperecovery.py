"""Resume an existing rebuild from its journal; never collect a new full plan."""
from __future__ import annotations

import json
from pathlib import Path
import time
from threading import Thread


def reference_path(rebuild):
    return rebuild._STATE_FILE.with_name('scrape-rebuild-task.json')


def reference(rebuild):
    path = reference_path(rebuild)
    if not path.is_file():
        path = rebuild._STATE_FILE  # Older versions only wrote the state file.
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding='utf-8'))
    return value if isinstance(value, dict) else {}


def info(rebuild, running):
    try:
        ref = reference(rebuild)
        directory = ref.get('quarantine_dir')
        available = bool(directory and not rebuild._unsafe_links(Path(directory)) and (Path(directory) / 'manifest.json').is_file())
        resumable = available and not running and (ref.get('phase') != 'completed' or ref.get('running'))
        return {'can_resume': bool(resumable), 'can_retry_failed': bool(resumable and ref.get('done', 0) > ref.get('ok', 0)),
                'job_id': ref.get('job_id', ''), 'done': ref.get('done', 0), 'total': ref.get('total', 0),
                'ok': ref.get('ok', 0), 'quarantine_dir': directory or ''}
    except (OSError, ValueError, TypeError):
        return {'can_resume': False, 'can_retry_failed': False, 'error': '无法读取任务恢复记录'}


def naming_config(settings):
    return {key: getattr(settings, key, None) for key in
            ('medialink_scrape_dir', 'medialink_library_path', 'scrape_dir_template', 'scrape_file_template', 'scrape_category')}


def start(job_id, mode='unfinished'):
    from app.services import scraperebuild as rebuild
    with rebuild._LOCK:
        rebuild.state()  # Recover an interrupted process state on first access.
        candidate = info(rebuild, rebuild._STATE['running'])
        if mode not in ('unfinished', 'failed') or not job_id or job_id != candidate.get('job_id'):
            raise ValueError('恢复任务已变化，请刷新状态后重试')
        if not candidate.get('can_resume') or mode == 'failed' and not candidate.get('can_retry_failed'):
            raise ValueError('当前没有可继续的任务或失败项')
        rebuild._require_idle()
        rebuild._maintenance().reserve()
        rebuild._STATE.update(running=True, phase='checking', error='', token='', plan=None,
                              job_id=job_id, quarantine_dir=candidate['quarantine_dir'],
                              done=candidate['done'], total=candidate['total'], ok=candidate['ok'],
                              started_at=time.time(), finished_at=None, progress=None, progress_steps={}, active_codes=[])
    try:
        Thread(target=_run, args=(candidate, mode), daemon=True, name='rebuild-resume').start()
    except Exception:
        rebuild._maintenance().release()
        rebuild._update(running=False, phase='failed', error='无法启动恢复任务')
        raise
    return rebuild.state()


def _load(rebuild, candidate):
    quarantine = rebuild._safe_path(candidate['quarantine_dir'])
    manifest_path = quarantine / 'manifest.json'
    if rebuild._unsafe_links(manifest_path):
        raise ValueError('恢复清单路径不安全')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    output = rebuild._safe_path(manifest['output_dir'], output=True)
    settings = rebuild.get_settings()
    configured = rebuild._safe_path(settings.medialink_scrape_dir or settings.medialink_library_path, output=True)
    if output != configured or quarantine == output or quarantine.is_relative_to(output) or output.is_relative_to(quarantine):
        raise ValueError('原任务输出目录与当前设置不同，不能继续')
    if manifest.get('job_id') != candidate['job_id']:
        raise ValueError('任务标识与恢复清单不一致')
    if manifest.get('quarantine_mode') not in ('copy_sidecars', 'rename'):
        raise ValueError('未知的原任务隔离方式')
    if manifest.get('naming_config') and manifest['naming_config'] != naming_config(settings):
        raise ValueError('目录或命名规则已改变，请恢复原任务设置后继续')
    for raw, _, kind in manifest['original_manifest']:
        relative = Path(raw)
        if not relative.parts or relative.is_absolute() or '..' in relative.parts or kind not in ('file', 'dir'):
            raise ValueError('恢复清单包含不安全的文件路径')
    sources = manifest['sources']
    for source in sources:
        path = Path(source['path'])
        if not path.is_absolute() or path == output or path.is_relative_to(output):
            raise ValueError('恢复清单的下载源路径不合法')
    source_paths = {row['path'] for row in sources}
    if any(not rebuild._under(row['link_path'], output) for row in manifest.get('media_links', [])):
        raise ValueError('恢复清单的媒体关联路径越界')
    if any(not rebuild._under(row['video'], output) or row['source'] not in source_paths for row in manifest.get('evidence', [])):
        raise ValueError('视频恢复证据不属于原任务')
    if any(not rebuild._under(raw, output) for raw in manifest.get('retained', [])):
        raise ValueError('原任务保留路径越界')
    latest = {row['code']: row for row in manifest.get('results', [])}
    journal = quarantine / 'results.jsonl'
    if rebuild._unsafe_links(journal):
        raise ValueError('结果日志路径不安全')
    if journal.is_file():
        lines = journal.read_bytes().splitlines(keepends=True)
        for index, line in enumerate(lines):
            try:
                row = json.loads(line)
                latest[row['code']] = row
            except (ValueError, KeyError, TypeError):
                if index == len(lines) - 1 and not line.endswith(b'\n'):
                    with journal.open('r+b') as stream:
                        stream.truncate(sum(len(part) for part in lines[:index]))
                        stream.flush()
                        rebuild.os.fsync(stream.fileno())
                    break  # Process termination during the final append.
                raise ValueError('结果日志中间记录损坏，不能推断完成状态')
        if lines and not lines[-1].endswith(b'\n'):
            # Complete JSON without its final newline is also a valid commit.
            tail = journal.read_bytes()
            if tail and not tail.endswith(b'\n'):
                with journal.open('ab') as stream:
                    stream.write(b'\n')
                    stream.flush()
                    rebuild.os.fsync(stream.fileno())
    for row in latest.values():
        if any(not rebuild._under(raw, output) for raw in row.get('links', [])):
            raise ValueError('影片结果路径越界')
    manifest['results'] = list(latest.values())
    if 'options' not in manifest:
        manifest['options'] = {'refresh_metadata': False, 'force': False}
        manifest.setdefault('cleanup_notes', []).append({'status': 'legacy', 'reason': '旧版本任务未记录联网选项，继续时默认复用已有影片资料'})
    return quarantine, output, manifest


def _source_stamps(rebuild, manifest, strict):
    stamps = {}
    prior = manifest.get('recovery_source_stamps', {})
    for source in manifest['sources']:
        raw = source['path']
        try:
            path = rebuild._safe_path(raw)
            current = rebuild._stamp(path)
            expected = prior.get(raw, source['stamp'])
            # Own unlinks/new hard links legitimately change ctime on restart.
            if current[:4] != expected[:4]:
                raise ValueError(f'下载源身份或内容属性已变化: {raw}')
            stamps[raw] = current
        except (OSError, ValueError):
            if strict:
                raise ValueError(f'清理前无法验证下载源，原文件保留: {raw}')
            # Rebuilding handles missing/changed sources as per-film failures.
    return stamps


def _plan(rebuild, manifest, output, stamps):
    entries = []
    for raw, expected, kind in manifest['original_manifest']:
        path = output / raw
        if not path.exists():
            continue
        if rebuild._unsafe_links(path):
            raise ValueError(f'恢复路径出现符号链接: {path}')
        current = rebuild._stamp(path)
        if kind == 'dir' and current[:2] != expected[:2]:
            raise ValueError(f'原目录身份变化: {path}')
        if kind == 'file' and path.suffix.lower() in rebuild.local.VIDEO_SUFFIXES and current[:4] != expected[:4]:
            raise ValueError(f'尚未清理的原视频已变化: {path}')
        entries.append([raw, current, kind])
    sources = [{**row, 'stamp': stamps.get(row['path'], row['stamp'])} for row in manifest['sources']]
    return {'public': {'output_dir': str(output)}, 'manifest': entries, 'sources': sources,
            'metadata': manifest.get('metadata', []), 'evidence': manifest.get('evidence', []),
            'links': manifest.get('media_links', []), 'retained': manifest.get('retained', []),
            'quarantine_mode': manifest['quarantine_mode'], 'missing_policy': manifest.get('missing_policy', 'block'),
            'unrecoverable': manifest.get('unrecoverable', [])}


def _finish_cleanup(rebuild, plan, manifest, output, storage, quarantine):
    if plan['quarantine_mode'] == 'copy_sidecars':
        if 'sidecar_backups' not in manifest:
            # Reuse verified completed copies, discard only incomplete backup files.
            existing, todo = [], []
            for entry in plan['manifest']:
                raw, expected, kind = entry
                if kind == 'dir' or Path(raw).suffix.lower() in rebuild.local.VIDEO_SUFFIXES:
                    todo.append(entry)
                    continue
                original, saved = output / raw, storage / raw
                if rebuild._unsafe_links(saved):
                    raise ValueError('资料备份路径不安全')
                if saved.is_file() and saved.stat().st_size == expected[2] and rebuild._progress_hash(saved) == rebuild._progress_hash(original):
                    existing.append({'path': raw, 'size': expected[2], 'sha256': rebuild._progress_hash(saved)})
                else:
                    if saved.is_file():
                        saved.unlink()
                    todo.append(entry)
            rebuild._update(phase='backup')
            manifest['sidecar_backups'] = existing + rebuild._backup_sidecars({**plan, 'manifest': todo}, storage)
            rebuild._write_manifest(quarantine / 'manifest.json', manifest)
        rebuild._update(phase='clearing')
        stamps = rebuild._clear_copied_output(plan, output, storage, quarantine, manifest)
    else:
        # A rename completed before its journal append is recognized by location.
        for name in sorted({Path(raw).parts[0] for raw, _, _ in manifest['original_manifest']}):
            source, saved = output / name, storage / name
            if source.exists() and saved.exists():
                raise ValueError(f'原位置与隔离位置同时存在，请核对: {name}')
            if source.exists():
                if rebuild._unsafe_links(source) or rebuild._unsafe_links(saved):
                    raise ValueError('继续隔离时路径不安全')
                rebuild.os.replace(source, saved)
                rebuild._append_operation(quarantine, {'operation': 'rename', 'name': name})
        stamps = _source_stamps(rebuild, manifest, strict=True)
    retained = set(plan['retained'])
    rebuild._map_cache_paths(output, storage, retained)
    from app.database.models import MediaLink, ScrapeSourceVideo
    from sqlalchemy import select
    with rebuild.session_scope() as session:
        for old in plan['links']:
            if old['link_path'] in retained:
                continue
            row = session.get(MediaLink, old['link_path'])
            if row and row.source_path == old['source_path'] and row.code == old['code']:
                session.delete(row)
        for cached in session.scalars(select(ScrapeSourceVideo)):
            cached.links = [raw for raw in (cached.links or []) if raw in retained or not rebuild._under(raw, output)]
            if not cached.links:
                cached.artifact_status = 'pending'
    manifest.update(checkpoint='rebuilding', recovery_source_stamps=stamps, retained=plan['retained'])
    rebuild._write_manifest(quarantine / 'manifest.json', manifest)
    return stamps


def _run(candidate, mode):
    from app.services import scraperebuild as rebuild, scrapesources
    manifest = None
    quarantine = None
    try:
        rebuild._progress_start('checking', '核对原任务恢复记录', unit='项')
        quarantine, output, manifest = _load(rebuild, candidate)
        storage = quarantine / 'library'
        if rebuild._unsafe_links(storage) or not storage.is_dir():
            raise ValueError('原任务隔离目录不可用')
        checkpoint = manifest.get('checkpoint')
        after_cleanup = checkpoint == 'rebuilding' or bool(manifest.get('recovery_source_stamps')) or bool(manifest.get('results'))
        if mode == 'failed' and not after_cleanup:
            raise ValueError('原任务尚未完成清理，请先继续未完成任务')
        stamps = _source_stamps(rebuild, manifest, strict=not after_cleanup)
        # Do not inspect the original tree after cleanup: it now contains NEW films.
        plan = _plan(rebuild, manifest, output, stamps) if not after_cleanup else {
            'public': {'output_dir': str(output)}, 'sources': manifest['sources'],
            'metadata': manifest.get('metadata', []), 'quarantine_mode': manifest['quarantine_mode'], 'retained': manifest.get('retained', [])}
        rebuild._maintenance().protect(str(output))
        rebuild._update(output_dir=str(output), quarantine_mode=manifest['quarantine_mode'])
        if not after_cleanup:
            rebuild._save_metadata(plan)
            stamps = _finish_cleanup(rebuild, plan, manifest, output, storage, quarantine)
        manifest.setdefault('recovery_source_stamps', stamps)
        groups = scrapesources.group_entries([{'path': row['path'], 'code': row['code']} for row in manifest['sources']])
        latest = {row['code']: row for row in manifest['results']}
        if mode == 'failed':
            selected = {code for code, row in latest.items() if not row['ok']}
        else:
            selected = {code for code in groups if code not in latest or not latest[code]['ok'] or
                        not latest[code].get('links') or any(not Path(raw).is_file() or rebuild._unsafe_links(Path(raw)) for raw in latest[code]['links'])}
        options = manifest.get('options', {})
        manifest['error'] = ''
        rebuild._update(phase='rebuilding')
        rebuild._run_films(plan, manifest, quarantine, storage, stamps, bool(options.get('refresh_metadata')), bool(options.get('force')), selected)
    except Exception as exc:
        rebuild.logger.exception('继续重建失败，已完成影片和原备份保留')
        if manifest is not None:
            manifest.update(phase='failed', error=str(exc))
            try:
                rebuild._write_manifest(quarantine / 'manifest.json', manifest)
            except OSError:
                rebuild.logger.exception('无法更新恢复任务清单')
        rebuild._update(phase='failed', error=str(exc))
    finally:
        try:
            rebuild._update(running=False, finished_at=time.time())
        finally:
            rebuild._maintenance().release()
