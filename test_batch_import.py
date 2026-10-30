"""Synthetic-only safety, incremental import and CLI contract tests."""
import concurrent.futures
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import CaptureError, MAX_TEXT_BYTES, Vault
from batch_import import (JSON_SCHEMA, MAX_INPUT_BYTES, apply_plan, canonical,
                          inspect_file, main, make_plan, read_file)


class BatchImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'vault'
        for name in ('.obsidian', '00 Inbox/raw', '04 Content/X', 'Projects'):
            (self.root / name).mkdir(parents=True)
        self.old = self.root / 'Projects/old.md'
        self.old.write_text('# Original synthetic note\nNever overwrite.\n', encoding='utf-8')
        self.original = self.old.read_bytes()
        self.vault = Vault(self.root)
        self.sources = self.base / 'exports'
        self.sources.mkdir()
        self.serial = 0

    def tearDown(self):
        self.assertEqual(self.old.read_bytes(), self.original)
        self.assertEqual(list((self.root / '.obsidian').iterdir()), [])
        self.temp.cleanup()

    def source(self, name='note.md', text='# Synthetic\nOriginal text.\n'):
        path = self.sources / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode('utf-8'))
        return path

    def json_source(self, records, name='records.json'):
        return self.source(name, json.dumps({'schema': JSON_SCHEMA, 'records': records}, ensure_ascii=False))

    def plan(self, inputs=None, **kwargs):
        return make_plan(self.vault, inputs or [self.sources], 'chatgpt', **kwargs)

    def apply(self, plan, vault=None):
        self.serial += 1
        report = self.base / f'report-{self.serial}.jsonl'
        summary = apply_plan(vault or self.vault, plan, report)
        rows = [json.loads(line) for line in report.read_text().splitlines()]
        self.assertEqual(rows[0]['event'], 'start')
        self.assertEqual(rows[-1], summary)
        return summary['counts'], rows

    def notes(self):
        return list(self.vault.raw.glob('*.md'))

    def test_preview_has_no_vault_writes_or_body(self):
        self.source(text='PRIVATE SYNTHETIC BODY')
        before = sorted(p.relative_to(self.root) for p in self.root.rglob('*'))
        plan = self.plan()
        self.assertNotIn('PRIVATE SYNTHETIC BODY', canonical(plan).decode())
        self.assertEqual(before, sorted(p.relative_to(self.root) for p in self.root.rglob('*')))
        self.assertEqual(plan['files'][0]['records'][0]['preview'], 'new')

    def test_markdown_text_and_metadata_preserved(self):
        text = '---\ncreated: source-opaque-value\n---\n# 中文🙂\r\n  exact spaces  \n'
        path = self.source(text=text)
        os.utime(path, ns=(1700000000123456789, 1700000000123456789))
        plan = self.plan()
        counts, rows = self.apply(plan)
        self.assertEqual(counts, {'imported': 1})
        body = self.notes()[0].read_bytes().decode()
        self.assertIn(text, body)
        self.assertTrue(Vault.complete_note(body))
        metadata = json.loads(next(l.split(': ', 1)[1] for l in body.splitlines() if l.startswith('import_metadata: ')))
        self.assertEqual(metadata['path'], str(path))
        self.assertEqual(metadata['mtime_ns'], 1700000000123456789)
        self.assertEqual(metadata['size_bytes'], path.stat().st_size)
        self.assertEqual(metadata['source_type'], 'chatgpt')
        self.assertEqual(metadata['file_sha256'], plan['files'][0]['file_sha256'])
        self.assertNotIn('exact spaces', canonical(rows).decode())

    def test_more_than_twelve_files_and_repeat_is_incremental(self):
        for i in range(25):
            self.source(f'{i}.txt', f'Synthetic record {i}')
        self.assertEqual(self.apply(self.plan())[0], {'imported': 25})
        self.assertEqual(self.apply(self.plan())[0], {'duplicate': 25})
        self.source('new.txt', 'New synthetic record')
        self.assertEqual(self.apply(self.plan())[0], {'duplicate': 25, 'imported': 1})
        self.assertEqual(len(self.notes()), 26)

    def test_duplicate_content_across_paths_and_mtime(self):
        a = self.source('a.md', 'same original text')
        b = self.source('b.txt', 'same original text')
        os.utime(b, (1700000000, 1700000000))
        plan = self.plan()
        self.assertEqual([r['preview'] for e in plan['files'] for r in e['records']], ['new', 'duplicate_in_batch'])
        counts, rows = self.apply(plan)
        self.assertEqual(counts, {'imported': 1, 'duplicate': 1})
        self.assertEqual(rows[1]['note'], rows[2]['note'])
        a.rename(self.sources / 'renamed.md')
        self.assertEqual(self.apply(self.plan())[0], {'duplicate': 2})

    def test_source_type_is_part_of_identity(self):
        self.source()
        self.apply(self.plan())
        plan = make_plan(self.vault, [self.sources], 'codex')
        self.assertEqual(self.apply(plan)[0], {'imported': 1})

    def test_changed_content_creates_new_note_without_overwrite(self):
        path = self.source()
        self.apply(self.plan())
        first = self.notes()[0]
        before = first.read_bytes()
        path.write_text('A revised synthetic source', encoding='utf-8')
        self.assertEqual(self.apply(self.plan())[0], {'imported': 1})
        self.assertEqual(first.read_bytes(), before)

    def test_edited_or_incomplete_saved_note_not_deduped(self):
        self.source()
        self.apply(self.plan())
        first = self.notes()[0]
        first.write_text('edited by user', encoding='utf-8')
        self.assertEqual(self.apply(self.plan())[0], {'imported': 1})
        self.assertEqual(first.read_text(), 'edited by user')

    def test_source_changed_after_preview_isolated(self):
        a = self.source('a.md', 'before')
        self.source('b.md', 'unchanged')
        plan = self.plan()
        a.write_text('after', encoding='utf-8')
        counts, rows = self.apply(plan)
        self.assertEqual(counts, {'changed': 1, 'imported': 1})
        self.assertNotEqual(rows[1]['expected_file_sha256'], rows[1]['actual_file_sha256'])

    def test_mtime_only_change_requires_preview_again(self):
        path = self.source()
        plan = self.plan()
        os.utime(path, (1000, 1000))
        self.assertEqual(self.apply(plan)[0], {'changed': 1})

    def test_deleted_source_isolated(self):
        path = self.source()
        plan = self.plan()
        path.unlink()
        self.assertEqual(self.apply(plan)[0], {'changed': 1})

    def test_json_mixed_sources_and_exact_source_times(self):
        self.json_source([{'id': 'c1', 'text': 'Synthetic conversation', 'source_type': 'chatgpt',
                           'created_at': '2026-10-27T12:34:56+08:00', 'source': 'https://example.invalid/chat'},
                          {'id': 'p1', 'text': 'Synthetic project', 'source_type': 'codex'},
                          {'text': 'Unverified transcript', 'source_type': 'meeting',
                           'updated_at': '2026-10-28T02:00:00Z'}])
        self.assertEqual(self.apply(self.plan())[0], {'imported': 3})
        bodies = '\n'.join(p.read_text() for p in self.notes())
        self.assertIn('2026-10-27T12:34:56+08:00', bodies)
        self.assertIn('2026-10-28T02:00:00Z', bodies)
        self.assertIn('不会自动生成或核对纪要', bodies)

    def test_bad_json_records_do_not_block_valid_siblings(self):
        self.json_source([{'text': 'good'}, {'text': 12}, {'text': 'bad', 'messages': []},
                          {'text': 'bad date', 'created_at': '2026-10-27'},
                          {'text': ''}, 'unknown'])
        counts, rows = self.apply(self.plan())
        self.assertEqual(counts, {'imported': 1, 'error': 5})
        self.assertEqual(len(self.notes()), 1)

    def test_duplicate_ids_not_guessed(self):
        self.json_source([{'id': 'same', 'text': 'first'}, {'id': 'same', 'text': 'second'}, {'text': 'ok'}])
        self.assertEqual(self.apply(self.plan())[0], {'error': 2, 'imported': 1})

    def test_unknown_export_format_and_corrupt_json_are_errors(self):
        self.source('chatgpt.json', '[{"mapping": {"unknown": "shape"}}]')
        self.source('corrupt.json', '{broken')
        self.source('good.txt', 'valid synthetic text')
        self.assertEqual(self.apply(self.plan())[0], {'error': 2, 'imported': 1})

    def test_duplicate_keys_nonfinite_and_unknown_envelope_rejected(self):
        for name, text in [('keys.json', '{"schema":"a","schema":"b","records":[]}'),
                           ('nan.json', '{"schema":"obsidian-ai-inbox/v1","records":[NaN]}'),
                           ('extra.json', '{"schema":"obsidian-ai-inbox/v1","records":[],"extra":1}')]:
            self.source(name, text)
        self.assertEqual(self.apply(self.plan())[0], {'error': 3})

    def test_utf8_bom_utf16_bom_and_explicit_legacy_encoding(self):
        for name, encoding in [('a.txt', 'utf-8-sig'), ('b.txt', 'utf-16')]:
            path = self.sources / name
            path.write_bytes(('Synthetic ' + name + ' 中文').encode(encoding))
        self.assertEqual(self.apply(self.plan())[0], {'imported': 2})
        legacy = self.sources / 'legacy.txt'
        legacy.write_bytes('中文会议资料'.encode('gb18030'))
        self.assertEqual(inspect_file(legacy, 'meeting', 'auto')[0]['status'], 'error')
        plan = self.plan([legacy], encoding='gb18030')
        self.assertEqual(self.apply(plan)[0], {'imported': 1})

    def test_unsupported_binary_and_over_limit_are_isolated(self):
        self.source('photo.png', 'Not parsed as text')
        self.source('nul.txt', 'binary\x00text')
        self.source('large.md', 'x' * (MAX_TEXT_BYTES + 1))
        too_large = self.sources / 'huge.json'
        with too_large.open('wb') as f:
            f.truncate(MAX_INPUT_BYTES + 1)
        self.source('ok.txt', 'ok')
        self.assertEqual(self.apply(self.plan())[0], {'error': 4, 'imported': 1})

    def test_directories_shallow_default_recursive_explicit(self):
        self.source('root.md', 'root')
        self.source('nested/a.md', 'nested')
        self.source('.hidden/secret.md', 'hidden')
        self.source('node_modules/no.md', 'excluded')
        self.assertEqual(len(self.plan()['files']), 1)
        self.assertEqual(len(self.plan(recursive=True)['files']), 2)

    def test_symlink_file_and_directory_not_followed(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'secret.md').write_text('PRIVATE SYNTHETIC', encoding='utf-8')
        (self.sources / 'linked.md').symlink_to(outside / 'secret.md')
        (self.sources / 'linked-dir').symlink_to(outside, target_is_directory=True)
        plan = self.plan(recursive=True)
        self.assertEqual(len(plan['files']), 1)
        self.assertEqual(plan['files'][0]['error'], 'symlink_path')
        self.assertNotIn('PRIVATE SYNTHETIC', canonical(plan).decode())

    def test_explicit_symlink_parent_rejected(self):
        link = self.base / 'link'
        link.symlink_to(self.sources, target_is_directory=True)
        self.source()
        with self.assertRaises(CaptureError):
            self.plan([link / 'note.md'])

    def test_hidden_explicit_source_not_read(self):
        path = self.source('.hidden.md', 'PRIVATE SYNTHETIC')
        entry, records = inspect_file(path, 'chatgpt', 'auto')
        self.assertEqual(entry['error'], 'hidden_path')
        self.assertFalse(records)

    def test_source_overlapping_vault_rejected(self):
        for path in (self.root, self.old, self.base):
            with self.subTest(path=path), self.assertRaises(CaptureError):
                self.plan([path])

    def test_output_in_vault_or_existing_rejected_before_import(self):
        self.source()
        plan = self.plan()
        for report in (self.vault.raw / 'report.jsonl', self.old):
            with self.subTest(report=report), self.assertRaises(CaptureError):
                apply_plan(self.vault, plan, report)
        existing = self.base / 'exists.jsonl'
        existing.write_text('existing report')
        with self.assertRaises(CaptureError):
            apply_plan(self.vault, plan, existing)
        self.assertEqual(existing.read_text(), 'existing report')
        self.assertEqual(self.notes(), [])

    def test_symlink_write_destination_rejected(self):
        self.source()
        plan = self.plan()
        self.vault.raw.rmdir()
        self.vault.raw.symlink_to(self.sources, target_is_directory=True)
        self.assertEqual(self.apply(plan)[0], {'error': 1})
        self.assertEqual(len(list(self.sources.iterdir())), 1)

    def test_plan_wrong_vault_and_tampered_descriptor(self):
        self.source()
        plan = self.plan()
        plan['vault'] = str(self.base)
        with self.assertRaises(CaptureError):
            self.apply(plan)
        plan['vault'] = str(self.root)
        plan['files'][0]['records'][0]['title'] = 'Tampered'
        self.assertEqual(self.apply(plan)[0], {'changed': 1})

    def test_concurrent_imports_share_capture_lock(self):
        self.source()
        plan = self.plan()
        def run(i):
            return apply_plan(Vault(self.root), plan, self.base / f'concurrent-{i}.jsonl')['counts']
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            counts = list(pool.map(run, (1, 2)))
        self.assertEqual(sum(c.get('imported', 0) for c in counts), 1)
        self.assertEqual(sum(c.get('duplicate', 0) for c in counts), 1)
        self.assertEqual(len(self.notes()), 1)

    def test_save_failure_isolated_and_retry_safe(self):
        self.source('a.md', 'a')
        self.source('b.md', 'b')
        plan = self.plan()
        original = self.vault.capture
        def fail_one(data, **kwargs):
            if data['text'] == 'a':
                raise OSError('simulated disk issue')
            return original(data, **kwargs)
        with patch.object(self.vault, 'capture', side_effect=fail_one):
            self.assertEqual(self.apply(plan)[0], {'error': 1, 'imported': 1})
        self.assertEqual(self.apply(plan)[0], {'imported': 1, 'duplicate': 1})

    def test_report_failure_stops_before_notes(self):
        self.source()
        plan = self.plan()
        with patch('batch_import.os.fsync', side_effect=OSError('simulated report failure')):
            with self.assertRaises(OSError):
                apply_plan(self.vault, plan, self.base / 'interrupted.jsonl')
        self.assertEqual(self.notes(), [])

    def test_partial_note_write_preserved_and_retry_creates_complete_copy(self):
        self.source()
        plan = self.plan()
        def interrupted(path, body):
            with path.open('xb') as handle:
                handle.write(body[:100])
            raise OSError('simulated partial note write')
        with patch.object(self.vault, 'write_new', side_effect=interrupted):
            self.assertEqual(self.apply(plan)[0], {'error': 1})
        partial = self.notes()[0]
        original = partial.read_bytes()
        self.assertFalse(Vault.complete_note(original.decode()))
        self.assertEqual(self.apply(plan)[0], {'imported': 1})
        self.assertEqual(partial.read_bytes(), original)
        self.assertEqual(len(self.notes()), 2)

    def test_malformed_plan_stops_before_report_or_notes(self):
        self.source()
        plan = self.plan()
        plan['files'][0]['records'] = [None]
        with self.assertRaises(CaptureError):
            apply_plan(self.vault, plan, self.base / 'bad-plan-report.jsonl')
        self.assertFalse((self.base / 'bad-plan-report.jsonl').exists())
        self.assertEqual(self.notes(), [])

    def test_empty_json_records_rejected(self):
        self.json_source([])
        self.assertEqual(self.plan()['files'][0]['error'], 'empty_records')

    def test_report_failure_after_save_can_retry_without_duplicate(self):
        self.source()
        plan = self.plan()
        original = os.fsync
        calls = []
        def fail_after_save(fd):
            calls.append(fd)
            if len(calls) == 3:  # report header, note, report result
                raise OSError('simulated interrupted audit write')
            return original(fd)
        with patch('batch_import.os.fsync', side_effect=fail_after_save):
            with self.assertRaises(OSError):
                apply_plan(self.vault, plan, self.base / 'partial.jsonl')
        self.assertEqual(len(self.notes()), 1)
        self.assertEqual(self.apply(plan)[0], {'duplicate': 1})

    def test_limits_and_nonregular_file(self):
        self.source()
        with patch('batch_import.MAX_FILES', 0), self.assertRaises(CaptureError):
            self.plan()
        self.json_source([{'text': 'one'}, {'text': 'two'}])
        with patch('batch_import.MAX_RECORDS', 1):
            self.assertEqual(inspect_file(self.sources / 'records.json', 'chatgpt', 'auto')[0]['status'], 'error')
        fifo = self.sources / 'pipe.txt'
        os.mkfifo(fifo)
        with self.assertRaises(CaptureError):
            read_file(fifo, 100)

    def test_cli_preview_apply_and_partial_exit_codes(self):
        self.source()
        self.source('bad.json', 'unknown JSON')
        plan = self.base / 'preview.json'
        report = self.base / 'audit.jsonl'
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['preview', '--vault', str(self.root), '--source-type', 'codex',
                                   '--plan', str(plan), str(self.sources)]), 0)
        self.assertEqual(json.loads(out.getvalue())['vault_writes'], 0)
        self.assertEqual(self.notes(), [])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(['apply', '--vault', str(self.root), '--plan', str(plan), '--report', str(report)]), 2)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(['apply', '--vault', str(self.root), '--plan', str(plan), '--report', str(report)]), 1)


if __name__ == '__main__':
    unittest.main()
