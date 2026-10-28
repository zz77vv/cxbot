"""Audit a curated source-only release; never print sensitive literal values."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def release_files():
    top = ['main.py', 'requirements.txt', 'requirements-dev.txt', 'requirements-lock.txt',
           '.env.example', '.gitignore', 'README.md', 'pytest.ini']
    paths = [ROOT / name for name in top]
    for directory in ('telegram_bot', 'tests', 'scripts'):
        paths.extend(p for p in (ROOT / directory).rglob('*') if p.is_file()
                     and p.suffix in ('.py', '.md') and '__pycache__' not in p.parts)
    paths.extend(p for p in (ROOT / 'docs').iterdir() if p.is_file() and p.suffix in ('.md', '.json', '.xml'))
    return sorted(set(paths))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--legacy-source', type=Path)
    parser.add_argument('--zip', type=Path)
    args = parser.parse_args()
    inventory = json.loads((ROOT / 'docs/legacy_inventory.json').read_text(encoding='utf-8'))
    secrets = []
    if args.legacy_source:
        payload = args.legacy_source.read_bytes()
        if hashlib.sha256(payload).hexdigest() != inventory['sha256']:
            raise ValueError('旧源码 SHA256 与初始检查不一致')
        tree = ast.parse(payload.decode('utf-8-sig'))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(n, ast.Name) and n.id in {'BOT_TOKEN', 'API_HASH'} for n in node.targets):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) and node.value.value:
                    secrets.append(node.value.value)
        print('Legacy SHA256: unchanged; sensitive values loaded only for in-memory matching')
    files = release_files()
    for path in files:
        text = path.read_text(encoding='utf-8-sig')
        if any(secret in text for secret in secrets):
            raise ValueError('交付文件命中源凭据；停止打包（不回显值）')
        if path.suffix == '.py':
            ast.parse(text)
    report = {
        'legacy_sha256': inventory['sha256'],
        'legacy_unchanged_checked': bool(args.legacy_source),
        'sensitive_literal_scan': 'passed' if secrets else 'no_legacy_values_supplied',
        'source_files': len(set(files) | {ROOT / 'docs/release_audit.json', ROOT / 'docs/release_manifest.json'}),
        'packaging': 'allowlist; no runtime databases/sessions/logs/env/cache/venv',
        'github': 'not configured; no repository, remote or synchronization target',
        'server': 'not accessed',
    }
    (ROOT / 'docs/release_audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    files = release_files()
    excluded = {'docs/release_manifest.json'}
    manifest = {str(p.relative_to(ROOT)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in files if str(p.relative_to(ROOT)).replace('\\', '/') not in excluded}
    (ROOT / 'docs/release_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Release audit passed:', len(manifest), 'hashed source/test/document files')
    if args.zip:
        destination = args.zip.resolve()
        # Never overwrite an existing delivery archive.
        with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            for path in release_files():
                archive.write(path, 'telegram_bot_v2/' + str(path.relative_to(ROOT)).replace('\\', '/'))
        with zipfile.ZipFile(destination) as archive:
            if archive.testzip() is not None:
                raise ValueError('压缩包校验失败')
            forbidden = {'.db', '.session', '.log', '.pyc'}
            if any(Path(n).suffix in forbidden or '/.venv/' in n or n.endswith('/.env') for n in archive.namelist()):
                raise ValueError('压缩包包含运行数据，停止交付')
        print('ZIP validated:', destination.name)


if __name__ == '__main__':
    main()
