#!/usr/bin/env python3
"""Explicit, offline, two-phase text import. Standard library; no network access."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import sys

from app import CaptureError, EXCLUDED, MAX_TEXT_BYTES, Vault, sha

SCHEMA = 'obsidian-ai-inbox-import-plan/v1'
JSON_SCHEMA = 'obsidian-ai-inbox/v1'
SOURCE_TYPES = ('chatgpt', 'codex', 'meeting', 'other')
SUFFIXES = {'.md', '.markdown', '.txt', '.json'}
MAX_INPUT_BYTES = 25 * 1024 * 1024
MAX_FILES = 1000
MAX_RECORDS = 10000
MAX_PLAN_BYTES = 32 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def absolute(value):
    # Keep symlink components visible until checked; resolve() would conceal them.
    return Path(os.path.abspath(os.path.expanduser(str(value))))


def safe_path(path):
    for component in (path, *path.parents):
        if component.is_symlink():
            raise CaptureError('symlink_path')


def read_file(path, limit):
    safe_path(path)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise CaptureError('not_regular_file')
        if before.st_size > limit:
            raise CaptureError('file_too_large')
        raw = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
        if len(raw) > limit:
            raise CaptureError('file_too_large')
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise CaptureError('file_changed_during_read')
    return raw, after


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CaptureError('duplicate_json_key')
        result[key] = value
    return result


def invalid_constant(value):
    raise CaptureError('non_finite_json_number')


def load_json(raw):
    return json.loads(raw, object_pairs_hook=strict_object, parse_constant=invalid_constant)


def decode(raw, encoding):
    # Guessing legacy encodings can produce plausible nonsense. Require explicit choice.
    if encoding != 'auto':
        text = raw.decode(encoding)
    elif raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        text = raw.decode('utf-16')
    else:
        text = raw.decode('utf-8-sig')
    if any(ord(c) < 32 and c not in '\t\r\n' for c in text):
        raise CaptureError('binary_or_control_characters')
    return text


def string(value, limit):
    if not isinstance(value, str) or len(value.encode('utf-8')) > limit:
        raise CaptureError('invalid_text_field')
    return value


def record(value, default_source, fallback_title):
    if not isinstance(value, dict) or set(value) - {
            'id', 'title', 'text', 'source_type', 'source', 'created_at', 'updated_at'}:
        raise CaptureError('unsupported_record_fields')
    text = string(value.get('text'), MAX_TEXT_BYTES)
    if not text.strip():
        raise CaptureError('empty_text')
    source_type = value.get('source_type', default_source)
    if source_type not in SOURCE_TYPES:
        raise CaptureError('invalid_source_type')
    result = {'title': string(value.get('title', fallback_title), 500), 'text': text,
              'source_type': source_type, 'source': string(value.get('source', ''), 10000)}
    if 'id' in value:
        result['id'] = string(value['id'], 500)
        if not result['id'].strip():
            raise CaptureError('empty_record_id')
    for key in ('created_at', 'updated_at'):
        if key in value:
            stamp = string(value[key], 100)
            try:
                parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                if 'T' not in stamp or parsed.tzinfo is None:
                    raise ValueError()
            except ValueError:
                raise CaptureError('timestamp_requires_iso8601_timezone')
            result[key] = stamp  # Preserve the exact source representation.
    return result


def describe(item):
    return {k: v for k, v in item.items() if k != 'text'} | {
        'content_sha256': sha(item['text'].encode('utf-8')),
        'text_bytes': len(item['text'].encode('utf-8')),
        'capture_sha256': Vault.import_digest(item['source_type'], item['text'])}


def inspect_file(path, source_type, encoding):
    """Returns reviewable descriptors separately from transient private text."""
    entry = {'path': str(path), 'records': []}
    parsed_records = {}
    try:
        if any(p.startswith('.') for p in path.parts[1:]):
            raise CaptureError('hidden_path')
        if path.suffix.lower() not in SUFFIXES:
            raise CaptureError('unsupported_extension')
        raw, info = read_file(path, MAX_INPUT_BYTES)
        entry.update(file_sha256=sha(raw), size_bytes=len(raw), mtime_ns=info.st_mtime_ns,
                     file_modified_at=datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat())
        text = decode(raw, encoding)
        if path.suffix.lower() == '.json':
            data = load_json(text)
            if (not isinstance(data, dict) or set(data) != {'schema', 'records'}
                    or data['schema'] != JSON_SCHEMA or not isinstance(data['records'], list)):
                raise CaptureError('unsupported_json_schema')
            values = data['records']
        else:
            values = [{'text': text}]
        if not values:
            raise CaptureError('empty_records')
        if len(values) > MAX_RECORDS:
            raise CaptureError('too_many_records')
        ids = Counter(v.get('id') for v in values if isinstance(v, dict) and isinstance(v.get('id'), str))
        for index, value in enumerate(values):
            try:
                item = record(value, source_type, path.stem)
                if 'id' in item and ids[item['id']] > 1:
                    raise CaptureError('duplicate_record_id')
                entry['records'].append({'index': index, 'status': 'ready', **describe(item)})
                parsed_records[index] = item
            except (CaptureError, UnicodeError, TypeError) as exc:
                entry['records'].append({'index': index, 'status': 'error', 'error': error_code(exc)})
        entry['status'] = 'checked'
    except (CaptureError, OSError, ValueError, TypeError, RecursionError) as exc:
        entry.update(status='error', error=error_code(exc), records=[])
    return entry, parsed_records


def error_code(exc):
    if isinstance(exc, CaptureError):
        return str(exc)
    if isinstance(exc, UnicodeError):
        return 'decode_error'
    if isinstance(exc, OSError):
        return 'io_error'
    return 'invalid_format'


def inside(path, directory):
    return path == directory or directory in path.parents


def collect(inputs, recursive, vault):
    found = set()

    def add(path):
        if inside(path, vault.root) or inside(path.resolve(), vault.root):
            raise CaptureError('source_inside_vault')
        found.add(path)
        if len(found) > MAX_FILES:
            raise CaptureError('too_many_files: split into batches of at most 1000')

    for value in inputs:
        path = absolute(value)
        safe_path(path)
        if any(p.startswith('.') for p in path.parts[1:]):
            raise CaptureError('hidden_path')
        if inside(path, vault.root) or inside(vault.root, path):
            raise CaptureError('source_directory_overlaps_vault')
        if path.is_dir():
            def walk_error(exc):
                raise CaptureError('source_directory_unreadable')
            for parent, dirs, names in os.walk(path, followlinks=False, onerror=walk_error):
                dirs[:] = sorted(d for d in dirs if recursive and not d.startswith('.')
                                 and d not in EXCLUDED and not (Path(parent) / d).is_symlink())
                for name in sorted(names):
                    if not name.startswith('.'):
                        add(Path(parent) / name)
        else:
            add(path)
    return sorted(found)


def make_plan(vault, inputs, source_type, encoding='auto', recursive=False):
    if source_type not in SOURCE_TYPES:
        raise CaptureError('invalid_source_type')
    entries, seen = [], {}
    total = 0
    for path in collect(inputs, recursive, vault):
        entry, _ = inspect_file(path, source_type, encoding)
        total += len(entry['records'])
        if total > MAX_RECORDS:
            raise CaptureError('too_many_records: split batch')
        for item in entry['records']:
            if item['status'] != 'ready':
                continue
            digest = item['capture_sha256']
            existing = vault.find_capture(vault.raw, digest)
            item['preview'] = ('duplicate_existing' if existing else
                               'duplicate_in_batch' if digest in seen else 'new')
            if existing:
                item['existing_note'] = existing.relative_to(vault.root).as_posix()
            seen[digest] = True
        entries.append(entry)
    return {'schema': SCHEMA, 'created_at': now(), 'vault': str(vault.root),
            'source_type': source_type, 'encoding': encoding,
            'recursive': recursive, 'files': entries}


def output_path(value, vault, sources):
    path = absolute(value)
    safe_path(path)
    if inside(path, vault.root) or inside(path.resolve(), vault.root):
        raise CaptureError('report_or_plan_must_be_outside_vault')
    if path in sources:
        raise CaptureError('output_is_source')
    if path.exists():
        raise CaptureError('output_already_exists')
    if not path.parent.is_dir():
        raise CaptureError('output_parent_missing')
    return path


def validate_plan(plan, vault):
    if (not isinstance(plan, dict) or plan.get('schema') != SCHEMA
            or plan.get('vault') != str(vault.root)
            or plan.get('source_type') not in SOURCE_TYPES
            or plan.get('encoding') not in ('auto', 'utf-8-sig', 'utf-16', 'gb18030')
            or not isinstance(plan.get('files'), list) or len(plan['files']) > MAX_FILES):
        raise CaptureError('invalid_plan_or_wrong_vault')
    paths = []
    total = 0
    for entry in plan['files']:
        if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
            raise CaptureError('invalid_plan_file')
        path = absolute(entry['path'])
        if str(path) != entry['path'] or inside(path, vault.root) or inside(path.resolve(), vault.root):
            raise CaptureError('invalid_plan_source')
        if not isinstance(entry.get('records'), list):
            raise CaptureError('invalid_plan_records')
        if any(not isinstance(r, dict) for r in entry['records']):
            raise CaptureError('invalid_plan_record')
        total += len(entry['records'])
        if total > MAX_RECORDS or path in paths:
            raise CaptureError('invalid_plan_limits_or_duplicate_path')
        paths.append(path)
    return paths


def apply_plan(vault, plan, report):
    paths = validate_plan(plan, vault)
    report = output_path(report, vault, paths)
    # Create the report BEFORE notes. Retain the exclusive file handle, fsync each row.
    fd = os.open(report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    counts = Counter()
    with os.fdopen(fd, 'wb') as log:
        def emit(row):
            log.write(canonical(row) + b'\n')
            log.flush()
            os.fsync(log.fileno())

        emit({'event': 'start', 'schema': SCHEMA, 'started_at': now(),
              'vault': str(vault.root), 'plan_sha256': sha(canonical(plan))})
        for expected, path in zip(plan['files'], paths):
            actual, records = inspect_file(path, plan['source_type'], plan['encoding'])
            # Preview hints are advisory; source bytes and derived metadata must still match.
            normalized = dict(expected)
            normalized['records'] = [{k: v for k, v in r.items() if k not in ('preview', 'existing_note')}
                                     for r in expected['records']]
            if normalized != actual:
                counts['changed'] += 1
                emit({'event': 'file', 'path': str(path), 'status': 'changed',
                      'error': 'source_or_plan_changed: preview again',
                      'expected_file_sha256': expected.get('file_sha256'),
                      'actual_file_sha256': actual.get('file_sha256')})
                continue
            if actual['status'] == 'error':
                counts['error'] += 1
                emit({'event': 'file', **actual})
                continue
            for descriptor in actual['records']:
                row = {'event': 'record', 'path': str(path),
                       'file_sha256': actual['file_sha256'], 'size_bytes': actual['size_bytes'],
                       'mtime_ns': actual['mtime_ns'],
                       'file_modified_at': actual['file_modified_at'], **descriptor}
                if descriptor['status'] == 'ready':
                    item = records[descriptor['index']]
                    metadata = {k: v for k, v in row.items() if k not in ('event', 'status')}
                    metadata['import_tool'] = 'obsidian-offline-import-v1'
                    try:
                        saved = vault.capture({'kind': '会议' if item['source_type'] == 'meeting' else '资料',
                                               'title': item['title'], 'text': item['text'],
                                               'source': item['source'] or str(path)}, import_metadata=metadata)
                        row.update(status='duplicate' if saved['duplicate'] else 'imported',
                                   note=saved['relative_path'], note_sha256=sha(Path(saved['path']).read_bytes()))
                    except (CaptureError, OSError, ValueError):
                        row.update(status='error', error='save_unconfirmed: inspect vault and retry')
                counts[row['status']] += 1
                emit(row)  # If this fails, STOP; never proceed without an audit trail.
        summary = {'event': 'summary', 'finished_at': now(), 'counts': dict(counts)}
        emit(summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description='离线批量导入：先 preview，再 apply；仅使用明确指定的路径。')
    sub = parser.add_subparsers(dest='command', required=True)
    preview = sub.add_parser('preview', help='只读源文件和库；生成不含正文的预览清单')
    preview.add_argument('--vault', required=True)
    preview.add_argument('--source-type', required=True, choices=SOURCE_TYPES)
    preview.add_argument('--encoding', choices=('auto', 'utf-8-sig', 'utf-16', 'gb18030'), default='auto')
    preview.add_argument('--recursive', action='store_true', help='明确授权递归所选目录；跳过隐藏目录和软链接目录')
    preview.add_argument('--plan', required=True, help='库外新建的 JSON 文件；拒绝覆盖')
    preview.add_argument('inputs', nargs='+', help='明确选择的文件或目录；目录默认仅第一层')
    apply = sub.add_parser('apply', help='按预览清单导入；变化的文件须重新预览')
    apply.add_argument('--vault', required=True)
    apply.add_argument('--plan', required=True)
    apply.add_argument('--report', required=True, help='库外新建 JSONL 报告；拒绝覆盖')
    args = parser.parse_args(argv)
    try:
        vault = Vault(args.vault)
        if args.command == 'preview':
            plan = make_plan(vault, args.inputs, args.source_type, args.encoding, args.recursive)
            target = output_path(args.plan, vault, [Path(e['path']) for e in plan['files']])
            body = canonical(plan) + b'\n'
            if len(body) > MAX_PLAN_BYTES:
                raise CaptureError('plan_too_large: split batch')
            Vault.write_new(target, body)
            statuses = Counter(r.get('preview', r['status']) for e in plan['files'] for r in e['records'])
            statuses['file_errors'] = sum(e['status'] == 'error' for e in plan['files'])
            print(json.dumps({'plan': str(target), 'counts': dict(statuses), 'vault_writes': 0}, ensure_ascii=False))
            return 0
        raw, _ = read_file(absolute(args.plan), MAX_PLAN_BYTES)
        plan = load_json(raw.decode('utf-8'))
        summary = apply_plan(vault, plan, args.report)
        print(json.dumps(summary, ensure_ascii=False))
        return 2 if summary['counts'].get('error') or summary['counts'].get('changed') else 0
    except (CaptureError, OSError, ValueError, TypeError, RecursionError) as exc:
        print('导入停止：' + error_code(exc) + '；保留输入及已生成的报告，核对后重试。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
