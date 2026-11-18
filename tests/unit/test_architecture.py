import ast
import importlib
import json
import pkgutil
from pathlib import Path
import re
import telegram_bot

ROOT=Path(__file__).resolve().parents[2]


def test_all_modules_import_and_no_cycles():
    graph={}
    for mod in pkgutil.walk_packages(telegram_bot.__path__,telegram_bot.__name__+'.'):
        importlib.import_module(mod.name)
    for path in (ROOT/'telegram_bot').rglob('*.py'):
        name='.'.join(path.relative_to(ROOT).with_suffix('').parts)
        if name.endswith('.__init__'): name=name[:-9]
        deps=set()
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node,ast.ImportFrom):
                if node.level:
                    base=name if path.name=='__init__.py' else name.rpartition('.')[0]
                    target=importlib.util.resolve_name('.'*node.level+(node.module or ''),base)
                else: target=node.module or ''
                deps.add(target)
        graph[name]=deps
    visiting,done=set(),set()
    def visit(name):
        assert name not in visiting, f'circular import: {name}'
        if name in done:return
        visiting.add(name)
        for dep in graph.get(name,()):
            if dep in graph:visit(dep)
        visiting.remove(name);done.add(name)
    for name in graph:visit(name)


def test_no_removed_runtime_features_or_scattered_sql():
    for path in (ROOT/'telegram_bot').rglob('*.py'):
        text=path.read_text(encoding='utf-8')
        assert not re.search(r'\b(?:ad_loop|ad_running|ad_task|AD_GROUPS|dg_command|tz_command)\b',text),path
        assert '/dg' not in text and '/tz' not in text
        assert 'import *' not in text
        if 'storage' not in path.parts:
            assert not re.search(r'\b(?:SELECT .+ FROM|INSERT INTO|CREATE TABLE|DELETE FROM|UPDATE .+ SET)\b',text),path


def test_module_boundaries():
    for folder in ('models','utils','storage'):
        for path in (ROOT/'telegram_bot'/folder).rglob('*.py'):
            text=path.read_text(encoding='utf-8')
            assert 'from telegram_bot.services' not in text and 'from ..services' not in text
            assert 'from telegram_bot.handlers' not in text


def test_old_function_mapping_complete():
    inventory=json.loads((ROOT/'docs/legacy_inventory.json').read_text(encoding='utf-8'))
    mapping=json.loads((ROOT/'docs/migration_map.json').read_text(encoding='utf-8'))
    assert {d['name'] for d in inventory['definitions']}==set(mapping)
    for value in mapping.values():
        if value.startswith('removed:'):continue
        path=value.split(':')[0]
        assert (ROOT/path).is_file(),path
