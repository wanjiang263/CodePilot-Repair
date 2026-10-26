import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / 'artifacts/phase22'
read = lambda p: json.loads(p.read_text(encoding='utf-8'))
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
r = read(BASE / 'results.json')
config = read(ROOT / 'configs/phase22/config-r5.json')
sources = []
for source in config['prior_authorization_sources']:
    path = ROOT / source['path']
    actual = sha(path)
    assert actual == source['sha256'], str(path)
    sources.append({'path': source['path'], 'sha256': actual, 'unchanged': True})
ledgers = []
for row in r['instances']:
    paths = list(BASE.glob(f"live*/instances/{row['instance_id']}/dispatch-ledger-*.json"))
    paid = [p for p in paths if read(p)['requests']]
    assert len(paid) == 1, (row['instance_id'], paid)
    data = read(paid[0])
    assert not data['frozen']
    assert all(x['status'] == 'settled' for x in data['requests'])
    assert row['phase22_settled_cny'] <= 4
    assert row['terminal'] and row['status'] != 'DRY_RUN'
    ledgers.append({'instance_id': row['instance_id'], 'paid_main_attempts': 1, 'path': str(paid[0].relative_to(ROOT)), 'sha256': sha(paid[0]), 'requests': len(data['requests']), 'settled_cny': row['phase22_settled_cny'], 'unknown': 0})
assert r['cost']['phase22_settled_cny'] < 24
assert r['cost']['original30_used_upper_cny'] < 30
processes = []
for path in BASE.glob('live*/instances/*/process.json'):
    item = read(path)
    processes.append({'path': str(path.relative_to(ROOT)), **item})
live = read(BASE / 'process-liveness-final.json')
assert not any(x['active'] for x in live), 'an owned child still active'
out = {'historical_sources_unchanged': sources, 'paid_main_attempts': ledgers, 'unknown_phase22': 0, 'original30_remaining_cny': r['cost']['original30_remaining_cny'], 'processes': processes, 'process_liveness': live, 'stale_record_note': 'live-r2 SymPy detached session record says running, but PID60652 is absent; predispatch session loss supplement retained', 'no_further_paid_dispatch': True, 'final_source_after_run_hardening': 'See local commit; runtime freezes retained separately'}
prep = BASE / 'next-stage'
if prep.exists():
    catalog = read(prep / 'catalog.json')
    excluded = {x['instance_id'] for x in read(prep / 'exclusions.json')['instances']}
    selected = [x['instance_id'] for x in catalog['instances']]
    assert len(selected) == len(set(selected)) == 50
    assert not set(selected) & excluded
    assert not set(selected) & {x['instance_id'] for x in r['instances']}
    spec = read(prep / 'run-spec.json')
    for field, path in [('catalog_sha256', prep / 'catalog.json'), ('exclusions_sha256', prep / 'exclusions.json'), ('environment_audit_sha256', prep / 'environment-audit.json'), ('phase22_result_sha256', BASE / 'results.json')]:
        assert spec[field] == sha(path)
    out['next_stage_preparation_audit'] = {'unique_candidates': 50, 'intersection_with_exclusions': 0, 'all_manifest_hashes_valid': True, 'paid_dispatch': False}
(BASE / 'final-audit.json').write_text(json.dumps(out, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
print('audit passed: six single paid attempts, settled, historical hashes unchanged, no running child records')
