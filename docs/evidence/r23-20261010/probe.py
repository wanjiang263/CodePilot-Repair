import argparse
import faulthandler
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'src'))

def environment():
    return dict(python=sys.version, executable=sys.executable, windows=platform.platform(),
                packages={n: importlib.metadata.version(n) for n in ['tree-sitter', 'tree-sitter-python']})

def child(path, mode):
    faulthandler.enable()
    raw = Path(path).read_bytes()
    print(json.dumps(environment()), flush=True)
    print(f'BEGIN {path} mode={mode} bytes={len(raw)} sha256={hashlib.sha256(raw).hexdigest()}', flush=True)
    if mode == 'extract':
        from codepilot.repo.parser import TreeSitterParser
        from codepilot.repo.symbols import extract_symbols
        parsed = TreeSitterParser().parse(raw, path=Path(path).name)
        print('PARSED', flush=True)
        syms = extract_symbols(parsed, path=Path(path).name, repository_id='r23')
        print(f'OK symbols={len(syms)}', flush=True)
    else:
        import tree_sitter_python
        from tree_sitter import Language, Parser
        parser = Parser(Language(tree_sitter_python.language()))
        tree = parser.parse(raw)
        stack = [tree.root_node]
        count = 0
        while stack:
            node = stack.pop()
            if mode == 'points':
                _ = node.start_point.row, node.end_point.row, node.end_point.column
            elif mode == 'tuple-points':
                _ = node.start_point[0], node.end_point[0], node.end_point[1]
            elif mode == 'fields':
                _ = node.child_by_field_name('name'), node.child_by_field_name('body'), node.text
            stack.extend(reversed(node.children))
            count += 1
        print(f'OK nodes={count}', flush=True)

def run(path, mode):
    p = subprocess.run([sys.executable, '-X', 'faulthandler', '-u', str(Path(__file__).resolve()),
                        '--child', str(path), '--mode', mode], capture_output=True, timeout=60)
    return dict(path=str(path), mode=mode, returncode=p.returncode,
                exit_hex=f'0x{p.returncode & 0xffffffff:08X}',
                stdout=p.stdout.decode('utf-8', 'replace'), stderr=p.stderr.decode('utf-8', 'replace'))

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--child')
    ap.add_argument('--mode', default='extract')
    ap.add_argument('--repo')
    ap.add_argument('--out')
    args = ap.parse_args()
    if args.child:
        child(args.child, args.mode)
    else:
        files = sorted(Path(args.repo).rglob('*.py'))
        with Path(args.out).open('x', encoding='utf-8') as f:
            for path in files:
                result = run(path, args.mode)
                f.write(json.dumps(result, ensure_ascii=False) + '\n')
                f.flush()
                print(result['returncode'], path, flush=True)
