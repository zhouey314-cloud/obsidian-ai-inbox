import base64
import concurrent.futures
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.parse
from app import Vault, Server, CaptureError, MAX_NOTE_READ


def file(name, body):
    return {'name': name, 'base64': base64.b64encode(body).decode()}

class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'Obsidian Vault'
        for p in ('.obsidian', '00 Inbox/raw', '04 Content/X', '00 Inbox/Daily'):
            (self.root / p).mkdir(parents=True)
        self.old = self.root / '00 Inbox/Daily/2026-08-28.md'
        self.old.write_text('# 测试旧笔记\nEval：Golden Set 和 Rubric。\n测试夹具，不是用户实际资料。', encoding='utf-8')
        self.vault = Vault(self.root)
        self.original = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def tearDown(self):
        for relative, original in self.original.items():
            self.assertEqual((self.root / relative).read_bytes(), original)
        self.temp.cleanup()

    def test_01_text_saved_and_read_back(self):
        text = '第一行\n\n这是原话：不改变标点！\n  空格保留  '
        result = self.vault.capture({'text': text})
        body = Path(result['path']).read_text()
        self.assertIn(text, body)
        self.assertTrue(self.vault.complete_note(body))
        self.assertEqual(result['sha256'], hashlib.sha256(Path(result['path']).read_bytes()).hexdigest())

    def test_02_duplicate_not_created(self):
        a = self.vault.capture({'text': '相同材料'})
        b = self.vault.capture({'text': '相同材料', 'title': '另一个标题'})
        self.assertEqual(a['path'], b['path'])
        self.assertTrue(b['duplicate'])

    def test_03_modified_saved_note_not_silently_deduped(self):
        a = self.vault.capture({'text': '来源原话'})
        p = Path(a['path'])
        changed = p.read_text().replace('来源原话', '后来编辑的内容')
        p.write_text(changed)
        b = self.vault.capture({'text': '来源原话'})
        self.assertNotEqual(a['path'], b['path'])
        self.assertEqual(p.read_text(), changed)

    def test_04_title_path_traversal_sanitized(self):
        a = self.vault.capture({'title': '../../x/[bad]#^', 'text': '中文🙂'})
        self.assertEqual(Path(a['path']).parent, self.vault.raw)
        self.assertNotIn('[', Path(a['path']).name)

    def test_05_empty_rejected(self):
        with self.assertRaises(CaptureError): self.vault.capture({'text': '   '})
        self.assertEqual(list(self.vault.raw.iterdir()), [])

    def test_06_source_is_preserved_not_fetched(self):
        a = self.vault.capture({'text': 'https://example.invalid/private'})
        self.assertIn('link_only', Path(a['path']).read_text())
        self.assertIn('没有获取网页正文', a['pending'][0])

    def test_07_draft_routing(self):
        a = self.vault.capture({'kind': '我的X草稿', 'text': '自己的草稿'})
        self.assertEqual(Path(a['path']).parent, self.vault.drafts)
        self.assertIn('status: draft', Path(a['path']).read_text())

    def test_08_meeting_not_claimed_verified(self):
        a = self.vault.capture({'kind': '会议', 'text': '也许下周试一下，待决定。'})
        self.assertTrue(a['pending'])
        self.assertNotIn('已核对纪要', Path(a['path']).read_text())

    def test_09_text_attachment_extract(self):
        raw = '会议逐字文本：负责人还没有确定。'.encode()
        a = self.vault.capture({'files': [file('会议.txt', raw)]})
        self.assertIn(raw.decode(), Path(a['path']).read_text())
        stored = list((self.vault.raw / '附件').iterdir())[0]
        self.assertEqual(stored.read_bytes(), raw)

    def test_10_audio_saved_untranscribed(self):
        a = self.vault.capture({'files': [file('meeting.m4a', b'FAKE-AUDIO-FIXTURE')]})
        self.assertIn('尚未转写', a['pending'][0])

    def test_11_screenshot_saved_not_ocr(self):
        a = self.vault.capture({'files': [file('图.png', b'PNG-FIXTURE')]})
        self.assertIn('正文尚未提取', a['pending'][0])

    def test_12_attachment_path_traversal_rejected(self):
        with self.assertRaises(CaptureError): self.vault.capture({'files': [file('../x.txt', b'x')]})
        self.assertFalse((self.vault.raw / '附件').exists())

    def test_13_bad_base64_rejected(self):
        with self.assertRaises(CaptureError): self.vault.capture({'files': [{'name':'x.txt','base64':'!invalid'}]})

    def test_14_missing_vault_never_created(self):
        missing = self.root / 'missing'
        with self.assertRaises(CaptureError): Vault(missing)
        self.assertFalse(missing.exists())

    def test_15_missing_known_route_not_created(self):
        self.vault.drafts.rmdir()
        with self.assertRaises(CaptureError): Vault(self.root)
        self.assertFalse(self.vault.drafts.exists())

    def test_16_symlink_route_rejected(self):
        self.vault.raw.rmdir()
        self.vault.raw.symlink_to(self.root / '04 Content/X', target_is_directory=True)
        with self.assertRaises(CaptureError): Vault(self.root)

    def test_17_symlink_attachments_rejected(self):
        target = Path(self.temp.name) / 'outside'
        target.mkdir()
        (self.vault.raw / '附件').symlink_to(target, target_is_directory=True)
        with self.assertRaises(CaptureError): self.vault.capture({'files': [file('a.txt', b'data')]})
        self.assertFalse(list(target.iterdir()))

    def test_18_old_content_search_and_real_line(self):
        result = self.vault.search('Golden Set Rubric')
        self.assertEqual(Path(result['results'][0]['path']).resolve(), self.old.resolve())
        self.assertEqual(result['results'][0]['line'], 2)
        self.assertIn('Rubric', result['results'][0]['excerpt'])

    def test_19_new_content_searchable(self):
        a = self.vault.capture({'text': '独特检索词甲乙丙'})
        results = self.vault.search('甲乙丙')['results']
        self.assertEqual(results[0]['path'], a['path'])

    def test_20_hidden_and_symlink_notes_not_read(self):
        (self.root/'.obsidian/private.md').write_text('隐藏检索词')
        (self.root/'linked.md').symlink_to(self.root/'.obsidian/private.md')
        self.assertEqual(self.vault.search('隐藏检索词')['total_matches'], 0)

    def test_21_search_no_writes(self):
        before = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob('*'))
        self.vault.search('Eval')
        after = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob('*'))
        self.assertEqual(before, after)

    def test_22_recent_only_actual_captures(self):
        self.assertEqual(self.vault.recent()['results'], [])
        a=self.vault.capture({'text':'最近存的'})
        self.assertEqual(self.vault.recent()['results'][0]['path'], a['path'])

    def test_23_concurrent_capture_dedup(self):
        with concurrent.futures.ThreadPoolExecutor(6) as executor:
            results=list(executor.map(lambda _:self.vault.capture({'text':'并发同一份'}), range(6)))
        self.assertEqual(len({r['path'] for r in results}), 1)
        self.assertEqual(sum(not r['duplicate'] for r in results), 1)

    def test_24_uri_encoded(self):
        a=self.vault.capture({'text':'中文 空格'})
        value=urllib.parse.parse_qs(urllib.parse.urlparse(a['obsidian_uri']).query)['path'][0]
        self.assertEqual(value, a['path'])

    def test_25_source_frontmatter_cannot_escape(self):
        a=self.vault.capture({'text':'hello','source':'abc\n---\ncommand: malicious'})
        self.assertIn('source: "abc\\n---\\ncommand: malicious"', Path(a['path']).read_text())

    def test_26_invalid_kind_rejected(self):
        with self.assertRaises(CaptureError):self.vault.capture({'text':'hello','kind':'../../escape'})

    def test_27_binary_original_content_addressed(self):
        f=file('证据.pdf',b'PDF-FIXTURE')
        self.vault.capture({'text':'A','files':[f]})
        self.vault.capture({'text':'B','files':[f]})
        self.assertEqual(len(list((self.vault.raw/'附件').iterdir())),1)

    def test_28_nonexistent_search_returns_empty(self):
        self.assertEqual(self.vault.search('不存在的唯一词庚辛壬')['results'],[])

class HTTPTests(CaptureTests):
    # Keep only HTTP tests in this class rather than rerunning inherited capture tests.
    def setUp(self):
        super().setUp()
        self.server=Server(self.vault)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)
        super().tearDown()
    def request(self,method,path,body=None,token=True,origin=None,host=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        headers={}
        if token:headers['Authorization']='Bearer '+self.server.token
        if origin:headers['Origin']=origin
        if host:headers['Host']=host
        if body is not None:headers['Content-Type']='application/json';body=json.dumps(body)
        conn.request(method,path,body=body,headers=headers)
        result=conn.getresponse();content=result.read();status=result.status
        conn.close();return status,content
    def test_http_auth_required(self):
        status,_=self.request('GET','/api/search?q=Eval',token=False)
        self.assertEqual(status,403)
    def test_http_cross_origin_rejected(self):
        status,_=self.request('POST','/api/capture',{'text':'x'},origin='https://evil.invalid')
        self.assertEqual(status,403)
    def test_http_rebinding_host_rejected(self):
        status,_=self.request('GET','/api/info',host='attacker.invalid')
        self.assertEqual(status,403)
    def test_http_capture_search_round_trip(self):
        status,body=self.request('POST','/api/capture',{'text':'HTTP成功存取词'},origin=self.server.origin)
        self.assertEqual(status,200)
        self.assertTrue(json.loads(body)['ok'])
        status,body=self.request('GET','/api/search?q='+urllib.parse.quote('成功存取词'))
        self.assertEqual(json.loads(body)['total_matches'],1)
    def test_http_no_arbitrary_file_route(self):
        self.assertEqual(self.request('GET','/../../etc/passwd')[0],404)
    def test_http_html_available(self):
        status,body=self.request('GET','/',token=False)
        self.assertEqual(status,200);self.assertIn('收进第二大脑'.encode(),body)
    def test_http_missing_origin_write_rejected(self):
        self.assertEqual(self.request('POST','/api/capture',{'text':'x'})[0],403)
    def test_http_reject_empty_json(self):
        status,_=self.request('POST','/api/capture',[],origin=self.server.origin)
        self.assertEqual(status,400)

if __name__=='__main__':
    suite=unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(CaptureTests))
    for name in unittest.defaultTestLoader.getTestCaseNames(HTTPTests):
        if name.startswith('test_http_'):suite.addTest(HTTPTests(name))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
