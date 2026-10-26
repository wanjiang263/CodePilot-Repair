"""Non-paid, deterministic preparation; refuses an unadmitted Phase22 result."""
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / 'artifacts/phase22'
OUT = BASE / 'next-stage'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(name, value):
    path = OUT / name
    if path.exists():
        raise FileExistsError(path)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main():
    result = json.loads((BASE / 'results.json').read_text(encoding='utf-8'))
    if not result['next_stage_admitted']:
        raise RuntimeError('Phase22 gate not met')
    revision = '6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2'
    source = ROOT / f'cache/swebench/princeton-nlp__SWE-bench_Lite__{revision}/test.jsonl'
    fields = ('instance_id', 'repo', 'base_commit', 'version', 'environment_setup_commit')
    rows = [{k: row[k] for k in fields} for row in map(json.loads, source.read_text(encoding='utf-8').splitlines())]
    pattern = re.compile('|'.join(re.escape(row['instance_id']) for row in rows))
    used = {}
    skip = {'.git', 'workspace', 'repo-cache', 'env-cache', 'node_modules', '__pycache__', 'next-stage', 'base-copy-probe', 'index-seeds'}
    for current, directories, files in os.walk(ROOT / 'artifacts'):
        current = Path(current)
        relative = current.relative_to(ROOT).as_posix()
        for found in pattern.finditer(relative):
            used.setdefault(found.group(), relative)
        directories[:] = [d for d in directories if d not in skip and not (current / d / '.git').exists()]
        for name in files:
            for found in pattern.finditer(name):
                used.setdefault(found.group(), f'{relative}/{name}')
    for path in ROOT.glob('glm-*.json'):
        if 'numpy1264' in path.name:
            used.setdefault('numpy__numpy-1264', path.name)
    # Prior frozen catalogs are conservative exclusions, even if never dispatched.
    for path in (ROOT / 'configs').rglob('*catalog*.json'):
        for found in pattern.finditer(path.read_text(encoding='utf-8')):
            used.setdefault(found.group(), path.relative_to(ROOT).as_posix())
    candidates = [row for row in rows if row['instance_id'] not in used]
    seed = 20261026
    candidates.sort(key=lambda row: hashlib.sha256(f"phase23:test50:{seed}:{row['instance_id']}".encode()).hexdigest())
    selected = candidates[:50]
    if len(selected) != 50:
        raise RuntimeError('fewer than 50 unused candidates')
    OUT.mkdir(parents=True, exist_ok=True)
    write('exclusions.json', {'method': 'Historical artifact path names and frozen catalog IDs; conservative exclusions; no outcome fields', 'instances': [{'instance_id': k, 'evidence': v} for k, v in sorted(used.items())]})
    write('catalog.json', {'status': 'PREPARATION_ONLY', 'dataset': 'princeton-nlp/SWE-bench_Lite', 'split': 'test', 'revision': revision, 'snapshot_sha256': sha(source), 'seed': seed, 'algorithm': "sha256(f'phase23:test50:{seed}:{instance_id}') ascending; first 50; no repository balancing", 'metadata_fields_used': fields, 'forbidden_fields_not_used': ['patch', 'test_patch', 'FAIL_TO_PASS', 'PASS_TO_PASS', 'hints_text'], 'eligible_count': len(candidates), 'excluded_count': len(used), 'instances': selected})
    import docker
    client = docker.from_env()
    image_refs = {tag: image.id for image in client.images.list() for tag in image.tags}
    coverage = []
    for row in selected:
        iid = row['instance_id']
        ref = f"swebench/sweb.eval.x86_64.{iid.replace('__', '_1776_')}:latest"
        repo = row['repo']
        runner = 'django' if repo == 'django/django' else 'sympy' if repo == 'sympy/sympy' else 'pytest_requests_compat' if repo == 'psf/requests' else 'pytest'
        coverage.append({'instance_id': iid, 'repo': repo, 'base_commit': row['base_commit'], 'expected_official_image': ref, 'cached_image_id': image_refs.get(ref), 'image_status': 'CACHED_UNVERIFIED_FOR_THIS_RUN' if ref in image_refs else 'NOT_CACHED', 'runner_candidate': runner, 'runner_status': 'PER_INSTANCE_PREFLIGHT_REQUIRED', 'native_scope_and_dependencies': 'UNVERIFIED; derive from public repository and isolated environment; no hidden target data'})
    write('environment-audit.json', {'mode': 'read-only Docker inventory; no builds, no test50 execution', 'docker_version': client.version()['Version'], 'cached': sum(bool(x['cached_image_id']) for x in coverage), 'total': 50, 'repos': dict(Counter(x['repo'] for x in coverage)), 'instances': coverage})
    costs = [x['phase22_settled_cny'] for x in result['instances']]
    write('run-spec.json', {'status': 'PREPARATION_ONLY_NOT_AUTHORIZED_TO_DISPATCH', 'phase22_result_sha256': sha(BASE / 'results.json'), 'catalog_sha256': sha(OUT / 'catalog.json'), 'exclusions_sha256': sha(OUT / 'exclusions.json'), 'environment_audit_sha256': sha(OUT / 'environment-audit.json'), 'provider': 'bigmodel', 'alias': 'glm-5.3-flash-main', 'model_id': 'glm-5.3-flash', 'temperature': 0.0, 'http_retries': 0, 'timeout': {'connect': 10, 'read': 120, 'write': 30, 'pool': 10}, 'trust_env': False, 'graph_seconds': 1800, 'review_reserve_seconds': 240, 'max_test_runs': 18, 'max_iterations': 5, 'max_review_calls': 2, 'attempts_per_instance': 1, 'proposed_per_instance_cap_cny': 4, 'proposed_50_instance_cap_cny': 200, 'authorization': 'Future explicit funding and dispatch authorization required; original30 remainder is not allocated to test50', 'illustrative_seen_sample_mean_cny': sum(costs)/6, 'illustrative_50_mean_cny': sum(costs)/6*50, 'cost_limitation': 'Six seen recovery cases; two shortened system failures and incomplete closure bias this estimate downward. 200 CNY is a proposed hard ceiling, not a forecast or authorization.', 'requirements_before_dispatch': ['Freeze source HEAD, catalog, configs, prices, full opening and original authorization separately', 'Validate each base-only clean workspace, index identity, runner parsing, image ID, dependencies and scope with a real lightweight test', 'Precharge each HTTP; unknown usage freezes ledger and stops all later paid dispatch', 'No paid repeats, automatic model switching or test300/M4', 'Only ACCEPTED plus complete actual review context can enter one official Harness; preserve ERROR/INVALID_ENV']})
    print(json.dumps({'selected': len(selected), 'excluded': len(used), 'eligible': len(candidates), 'cached_images': sum(bool(x['cached_image_id']) for x in coverage)}))


if __name__ == '__main__':
    main()
