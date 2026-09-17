#!/usr/bin/env python3
"""Create a local macOS launcher without touching the vault or system settings."""
import argparse
import datetime
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--vault', required=True,
                        help='Path to an existing Obsidian vault; the installer never creates one.')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        raise SystemExit('此安装器只在 Mac 上执行；存取测试可在 Linux 运行。')
    if sys.version_info < (3, 9):
        raise SystemExit('需要 Python 3.9+。请让本机 Codex 使用已有可用版本，不要先重装环境。')
    from app import Vault
    vault = Vault(args.vault)  # read-only validation; never creates an empty vault
    desktop = Path.home() / 'Desktop'
    if not desktop.is_dir():
        raise SystemExit('桌面目录不存在，未安装。')
    target = desktop / '收进第二大脑.app'
    if target.exists() or target.is_symlink():
        raise SystemExit('桌面已有“收进第二大脑.app”；未覆盖。请先让 Codex 核查是否是同一工具。')
    compiler = Path('/usr/bin/osacompile')
    if not compiler.is_file():
        raise SystemExit('没有找到系统 osacompile，未安装。')
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    support = Path.home() / 'Library' / 'Application Support' / 'Obsidian Local Capture' / stamp
    support.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parent
    for name in ('app.py', 'index.html', 'README.md', '给Codex的安装任务.md'):
        shutil.copy2(source / name, support / name)
    python = str(Path(sys.executable).resolve())
    command = shlex.join([python, str(support / 'app.py'), '--vault', str(vault.root)])
    # JSON quoting is appropriate here for an AppleScript string literal.
    shell = '/usr/bin/nohup ' + command + ' > ' + shlex.quote(str(support / 'startup.log')) + ' 2>&1 < /dev/null &'
    script = 'on run\n do shell script ' + json.dumps(shell, ensure_ascii=False) + '\nend run\n'
    script_path = support / 'launcher.applescript'
    script_path.write_text(script, encoding='utf-8')
    subprocess.run([str(compiler), '-o', str(target), str(script_path)], check=True)
    record = {'app': str(target), 'support': str(support), 'python': python,
              'vault': str(vault.root), 'vault_modified_by_installer': False,
              'login_item_created': False, 'scheduled_tasks_created': False}
    (support / 'installation.json').write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(record, indent=2, ensure_ascii=False))
    print('桌面入口已生成，但启动、浏览器操作和 Obsidian 跳转仍需在本机验证。')

if __name__ == '__main__':
    main()
