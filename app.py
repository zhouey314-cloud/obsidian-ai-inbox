#!/usr/bin/env python3
"""Local-only, append-only capture UI for an existing Obsidian vault.
Python 3.9+; standard library only. No model calls, telemetry, or URL fetching.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
import unicodedata
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

MAX_BODY = 75 * 1024 * 1024
MAX_FILES = 12
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_NOTE_READ = 8 * 1024 * 1024
KINDS = ('资料', 'X素材', '会议', '群聊或对话', '我的想法', '我的X草稿')
EXCLUDED = {'.git', '.obsidian', '.agents', '.trash', '.venv', 'node_modules', '附件'}
TEXT_SUFFIXES = {'.txt', '.md', '.markdown', '.csv', '.srt', '.vtt', '.json'}
AUDIO_SUFFIXES = {'.mp3', '.m4a', '.wav', '.aac', '.ogg', '.flac', '.opus', '.mp4', '.mov'}

class CaptureError(Exception):
    pass

def clean_name(value: str, limit: int = 48) -> str:
    value = unicodedata.normalize('NFC', str(value))
    value = re.sub(r'[\\/:*?"<>|\[\]#^\x00-\x1f\x7f]', ' ', value)
    value = re.sub(r'\s+', ' ', value).strip(' .')
    return value[:limit].strip(' .') or '未命名资料'

def note_uri(path: Path) -> str:
    return 'obsidian://open?path=' + quote(str(path), safe='')

def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def text_field(data: dict, key: str, limit: int = MAX_TEXT_BYTES) -> str:
    value = data.get(key, '')
    if not isinstance(value, str):
        raise CaptureError(f'{key} 必须是文字。')
    if len(value.encode('utf-8')) > limit:
        raise CaptureError(f'{key} 内容太长；本窗口文字上限为 2MB。')
    return value

def decode_text(raw: bytes) -> str | None:
    for encoding in ('utf-8-sig', 'utf-16', 'gb18030'):
        try:
            text = raw.decode(encoding)
            if '\x00' not in text:
                return text
        except (UnicodeError, LookupError):
            pass
    return None

class Vault:
    def __init__(self, root: str | Path):
        original = Path(root).expanduser()
        if not original.is_dir():
            raise CaptureError('没有找到现有 Obsidian 库。不会创建空库，请让 Codex 核对路径。')
        self.root = original.resolve()
        if not (self.root / '.obsidian').is_dir():
            raise CaptureError('该目录没有 .obsidian，无法确认是目标库；已停止。')
        self.raw = self.checked_dir('00 Inbox/raw')
        self.drafts = self.checked_dir('04 Content/X')
        self.lock = threading.Lock()

    def checked_dir(self, relative: str) -> Path:
        path = self.root / relative
        current = self.root
        for part in Path(relative).parts:
            current = current / part
            if current.is_symlink():
                raise CaptureError('目标目录含软链接；为避免写错位置，已停止。')
        if not path.is_dir():
            raise CaptureError(f'既有目录不存在：{relative}。不会擅自重建目录。')
        return path

    def iter_notes(self):
        for parent, dirs, names in os.walk(self.root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d not in EXCLUDED
                             and not (Path(parent) / d).is_symlink())
            for name in sorted(names):
                path = Path(parent) / name
                if name.lower().endswith('.md') and not path.is_symlink() and path.is_file():
                    yield path

    def read_note(self, path: Path) -> str | None:
        try:
            if path.stat().st_size > MAX_NOTE_READ:
                return None
            return path.read_text(encoding='utf-8-sig')
        except (OSError, UnicodeError):
            return None

    def public_note(self, path: Path, **extra) -> dict:
        return {'name': path.stem, 'path': str(path),
                'relative_path': path.relative_to(self.root).as_posix(),
                'obsidian_uri': note_uri(path), **extra}

    def search(self, query: str) -> dict:
        query = query.strip()[:200]
        if not query:
            raise CaptureError('请输入关键词，例如 Eval、分佣、Dan Koe。')
        terms = list(dict.fromkeys(re.findall(r'\S+', query.casefold())))[:10]
        results, skipped, count = [], 0, 0
        for path in self.iter_notes():
            count += 1
            content = self.read_note(path)
            if content is None:
                skipped += 1
                continue
            lower = content.casefold()
            title = path.stem.casefold()
            matched = [term for term in terms if term in lower or term in title]
            if not matched:
                continue
            score = len(matched) * 20 + sum(30 for term in matched if term in title)
            if query.casefold() in lower:
                score += 15
            # Show an actual line, not a generated answer or semantic inference.
            lines = content.splitlines()
            target = next(((i, line) for i, line in enumerate(lines, 1)
                           if any(term in line.casefold() for term in matched)
                           and not line.startswith('capture_sha256:')), (1, path.stem))
            number, excerpt = target
            results.append(self.public_note(path, score=score, line=number,
                                           excerpt=excerpt[:380], matches=matched))
        results.sort(key=lambda r: (-r['score'], r['relative_path']))
        return {'results': results[:25], 'total_matches': len(results),
                'files_checked': count, 'files_skipped': skipped,
                'notice': '这是本地关键词匹配，不是 AI 语义搜索；结果显示真实文件和原文。'}

    def recent(self) -> dict:
        results = []
        for folder in (self.raw, self.drafts):
            for path in folder.glob('*.md'):
                if path.is_symlink() or not path.is_file():
                    continue
                content = self.read_note(path)
                if content and 'capture_tool: obsidian-local-capture-v1' in content[:4096]:
                    results.append((path.stat().st_mtime, self.public_note(path)))
        results.sort(key=lambda x: x[0], reverse=True)
        return {'results': [item for _, item in results[:20]]}

    def capture(self, data: dict) -> dict:
        if not isinstance(data, dict):
            raise CaptureError('保存内容必须是对象。')
        kind = text_field(data, 'kind', 200) or '资料'
        if kind not in KINDS:
            raise CaptureError('不支持的资料类型。')
        text = text_field(data, 'text')
        title_in = text_field(data, 'title', 500)
        source = text_field(data, 'source', 10000).strip()
        files_in = data.get('files', [])
        if not isinstance(files_in, list) or len(files_in) > MAX_FILES:
            raise CaptureError('每次最多选择 12 个文件。')
        attachments, total = [], 0
        for item in files_in:
            if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not isinstance(item.get('base64'), str):
                raise CaptureError('附件格式错误。')
            name = item['name']
            # Browser names are basenames. Never accept relative traversal or absolute paths.
            if not name or len(name) > 512 or '/' in name or '\\' in name or name in ('.', '..') or '\x00' in name:
                raise CaptureError('附件名称无效。')
            try:
                raw = base64.b64decode(item['base64'], validate=True)
            except (ValueError, TypeError):
                raise CaptureError('附件编码不完整；未保存。')
            total += len(raw)
            if len(raw) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise CaptureError('单文件上限 25MB，一次合计 50MB；大录音请交给本机 Codex 按路径处理。')
            attachments.append({'name': name, 'data': raw, 'hash': sha(raw)})
        if not text.strip() and not attachments:
            raise CaptureError('还没有内容：请粘贴文字或选择文件。')
        inferred = next((line.strip().lstrip('# ').strip() for line in text.splitlines() if line.strip()), '')
        title = clean_name(title_in.strip() or inferred or (attachments[0]['name'] if attachments else '资料'))
        digest_input = {'kind': kind, 'text': text, 'source': source,
                        'files': sorted((a['name'], a['hash']) for a in attachments)}
        digest = sha(json.dumps(digest_input, sort_keys=True, ensure_ascii=False).encode('utf-8'))
        folder = self.drafts if kind == '我的X草稿' else self.raw
        # Validate write destinations again immediately before writing.
        self.checked_dir(folder.relative_to(self.root).as_posix())
        with self.lock:
            # OS-level interprocess lock lives outside the vault.
            import tempfile
            import fcntl
            lockname = Path(tempfile.gettempdir()) / f'obsidian-capture-{os.getuid()}-{sha(str(self.root).encode())[:16]}.lock'
            flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
            fd = os.open(lockname, flags, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                return self._save(folder, kind, text, title, source, attachments, digest)
            finally:
                os.close(fd)

    @staticmethod
    def complete_note(body: str) -> bool:
        marker = '\n<!-- capture-complete-sha256:'
        if marker not in body:
            return False
        prefix, trailer = body.rsplit(marker, 1)
        expected = trailer.strip().removesuffix('-->').strip()
        return len(expected) == 64 and sha(prefix.encode('utf-8')) == expected

    def _save(self, folder, kind, text, title, source, attachments, digest):
        for existing in folder.glob('*.md'):
            if existing.is_symlink() or not existing.is_file():
                continue
            body = self.read_note(existing)
            if body and f'capture_sha256: {digest}\n' in body[:4096] and self.complete_note(body):
                return self.public_note(existing, ok=True, duplicate=True,
                                        status='已保存过；没有重复创建', pending=[])
        saved_attachments, pending = [], []
        extracted_total = 0
        for item in attachments:
            raw = item['data']
            ext = Path(item['name']).suffix.lower()
            stem = clean_name(Path(item['name']).stem, 40)
            safe_ext = ext if re.fullmatch(r'\.[a-z0-9]{1,12}', ext or '') else '.bin'
            parent = self.raw / '附件'
            if parent.is_symlink():
                raise CaptureError('附件目录是软链接，已停止写入。')
            parent.mkdir(exist_ok=True)
            # Shared originals are immutable, content-addressed, and verified before reuse.
            target = parent / f'{item["hash"]}-{stem}{safe_ext}'
            if target.is_symlink():
                raise CaptureError('附件目标为软链接，已停止。')
            if target.exists():
                if not target.is_file() or sha(target.read_bytes()) != item['hash']:
                    raise CaptureError('已有附件校验不一致；未覆盖，请交给 Codex 核对。')
            else:
                self.write_new(target, raw)
            extract = None
            if ext in TEXT_SUFFIXES and len(raw) <= MAX_TEXT_BYTES:
                extract = decode_text(raw)
                if extract is not None:
                    extracted_total += len(extract.encode('utf-8'))
                    if extracted_total > MAX_TEXT_BYTES:
                        extract = None
            if ext in AUDIO_SUFFIXES:
                pending.append(f'{item["name"]}：原文件已保存，尚未转写')
            elif extract is None:
                pending.append(f'{item["name"]}：原文件已保存，正文尚未提取')
            saved_attachments.append((item['name'], target, extract))
        created = datetime.now().astimezone().isoformat(timespec='seconds')
        only_url = bool(text.strip() and re.fullmatch(r'https?://\S+', text.strip()) and not attachments)
        if only_url:
            pending.append('仅保存了链接；没有获取网页正文')
        elif kind == '会议':
            pending.append('会议材料已收好；本工具不会自动生成或核对纪要')
        status = 'draft' if kind == '我的X草稿' else ('link_only' if only_url else 'captured')
        # JSON strings are valid YAML scalar strings; user data cannot escape frontmatter.
        front = ['---', 'capture_tool: obsidian-local-capture-v1',
                 f'capture_sha256: {digest}', 'title: ' + json.dumps(title, ensure_ascii=False),
                 'created: ' + json.dumps(created), 'source_kind: ' + json.dumps(kind, ensure_ascii=False),
                 'source: ' + json.dumps(source or '未提供', ensure_ascii=False),
                 f'status: {status}', 'ai_processed: false', '---', '', f'# {title}', '',
                 '> 由本地收集入口保存。正文为用户提供的材料，不代表已核实；未调用 AI 整理。', '']
        if source:
            front.extend(['## 来源说明', source, ''])
        if text:
            front.extend(['## 提供的文字（保留原文）', text, ''])
        for name, target, extract in saved_attachments:
            relative = os.path.relpath(target, folder).replace(os.sep, '/')
            front.extend(['## 附件：' + clean_name(name, 90),
                          '[打开已保存的原文件](' + quote(relative, safe='/') + ')', ''])
            if extract is not None:
                front.extend(['### 从文本附件提取的文字', extract, ''])
        if pending:
            front.extend(['## 处理状态'] + ['- ' + p for p in pending] + [''])
        body = '\n'.join(front).encode('utf-8')
        body += ('\n<!-- capture-complete-sha256:' + sha(body) + ' -->\n').encode('utf-8')
        name = datetime.now().strftime('%Y-%m-%d-%H%M%S') + '-' + title + '-' + digest[:10] + '.md'
        target = folder / name
        # Existing notes are NEVER overwritten, even if a previous write was interrupted.
        while target.exists() or target.is_symlink():
            target = folder / (Path(name).stem + '-' + secrets.token_hex(3) + '.md')
        self.write_new(target, body)
        return self.public_note(target, ok=True, duplicate=False, status='已保存并读回校验',
                                pending=pending, sha256=sha(body))

    @staticmethod
    def write_new(path: Path, body: bytes):
        # Exclusive create + fsync + full readback. On failure, report without touching old files.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        if path.read_bytes() != body:
            raise CaptureError('读回校验失败；未报告成功，请保留当前输入并让 Codex 检查。')

class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, vault: Vault):
        super().__init__(('127.0.0.1', 0), Handler)
        self.vault = vault
        self.token = secrets.token_urlsafe(32)
        self.origin = f'http://127.0.0.1:{self.server_port}'
        self.last_seen = time.monotonic()

class Handler(BaseHTTPRequestHandler):
    server: Server
    def log_message(self, *args):
        pass  # No note content, tokens, or request paths in logs.

    def send(self, status, body, content_type='application/json; charset=utf-8'):
        if isinstance(body, (dict, list)):
            raw = json.dumps(body, ensure_ascii=False).encode('utf-8')
        elif isinstance(body, str):
            raw = body.encode('utf-8')
        else:
            raw = body
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def check(self, require_token=True, post=False):
        expected = f'127.0.0.1:{self.server.server_port}'
        if self.headers.get('Host') != expected:
            self.send(403, {'error': 'Host 不匹配。'})
            return False
        origin = self.headers.get('Origin')
        if origin and origin != self.server.origin:
            self.send(403, {'error': '拒绝来自其他网页的请求。'})
            return False
        if post and origin != self.server.origin:
            self.send(403, {'error': '仅允许本地入口提交。'})
            return False
        if require_token and not secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + self.server.token):
            self.send(403, {'error': '入口会话已失效，请重新打开桌面入口。'})
            return False
        return True

    def do_GET(self):
        parsed = urlparse(self.path)
        if not self.check(require_token=parsed.path != '/'):
            return
        try:
            if parsed.path == '/':
                self.send(200, Path(__file__).with_name('index.html').read_bytes(), 'text/html; charset=utf-8')
            elif parsed.path == '/api/info':
                self.server.last_seen = time.monotonic()
                self.send(200, {'vault': str(self.server.vault.root), 'kinds': KINDS})
            elif parsed.path == '/api/recent':
                self.server.last_seen = time.monotonic()
                self.send(200, self.server.vault.recent())
            elif parsed.path == '/api/search':
                self.server.last_seen = time.monotonic()
                self.send(200, self.server.vault.search(parse_qs(parsed.query).get('q', [''])[0]))
            else:
                self.send(404, {'error': '未找到。'})
        except CaptureError as exc:
            self.send(400, {'error': str(exc)})
        except Exception:
            self.send(500, {'error': '读取失败；未修改资料，请让 Codex 检查文件权限或编码。'})

    def do_POST(self):
        if not self.check(post=True):
            return
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            self.send(415, {'error': '仅接受 JSON。'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_BODY:
                raise CaptureError('请求过大或为空；未保存。')
            self.connection.settimeout(30)
            data = json.loads(self.rfile.read(length))
            self.server.last_seen = time.monotonic()
            if self.path == '/api/capture':
                self.send(200, self.server.vault.capture(data))
            elif self.path == '/api/stop':
                self.send(200, {'ok': True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send(404, {'error': '未找到。'})
        except (CaptureError, ValueError, TypeError, UnicodeError) as exc:
            self.send(400, {'error': str(exc)})
        except Exception:
            self.send(500, {'error': '保存未完成或无法确认；请保留输入。不会覆盖或删除旧资料。'})


def main():
    parser = argparse.ArgumentParser(description='Obsidian 本地收集入口')
    parser.add_argument('--vault', required=True,
                        help='Path to an existing Obsidian vault; the tool never creates one.')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    try:
        vault = Vault(args.vault)
        if args.check:
            print(json.dumps({'ok': True, 'vault': str(vault.root), 'writes': 0}, ensure_ascii=False))
            return
        server = Server(vault)
        # No system scheduler or login item. An unused process stops after 30 minutes.
        def idle_stop():
            while True:
                time.sleep(30)
                if time.monotonic() - server.last_seen > 1800:
                    server.shutdown()
                    return
        threading.Thread(target=idle_stop, daemon=True).start()
        url = server.origin + '/#token=' + server.token
        if not args.no_browser:
            if not webbrowser.open(url):
                server.server_close()
                raise CaptureError('默认浏览器未能打开。请让 Codex 检查本机浏览器设置，不要更改系统安全策略。')
        else:
            print(url, flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
    except Exception as exc:
        message = '收集入口未启动：' + str(exc)
        print(message, file=sys.stderr)
        if sys.platform == 'darwin':
            import subprocess
            subprocess.run(['/usr/bin/osascript', '-e', 'on run argv\n display alert "Obsidian 收集入口" message (item 1 of argv)\nend run', message], check=False)
        raise SystemExit(1)

if __name__ == '__main__':
    main()
