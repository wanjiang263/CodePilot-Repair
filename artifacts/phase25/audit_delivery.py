"""Read-only audit of fixed first five; never reads benchmark answer fields."""
import csv
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'artifacts/phase25'

def read(path):
    return json.loads(path.read_text(encoding='utf8'))

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def relative(path):
    return path.relative_to(ROOT).as_posix()

def money(value):
    return Decimal(str(value or 0))

catalog_path = ROOT / 'artifacts/phase22/next-stage/catalog.json'
assert digest(catalog_path) == '3cfa3b13cbbf281aafb216a6942bfbdc4c249c5c082c715218ea786c2cc0c3be'
catalog = read(catalog_path)
old = read(OUT / 'opening-audit.json')
prior_settled = Decimal(0)
prior_unknown = Decimal(0)
for name, expected in old['sources'].items():
    assert digest(ROOT / name) == expected, name
    for request in read(ROOT / name)['requests']:
        if request['status'] == 'settled':
            prior_settled += money(request['cost_cny'])
        else:
            prior_unknown += money(request['reserved_cny'])
assert abs(prior_settled + prior_unknown - money(old['authorized_upper_cny'])) < Decimal('0.000000001')
opening = read(OUT / 'opening.json')
for source in opening['sources']:
    assert digest(ROOT / source['path']) == source['sha256'], source['path']

ids = [item['instance_id'] for item in catalog['instances']]
assert len(ids) == 50 and len(set(ids)) == 50
roots = [
    OUT / 'run/instances' / ids[0],
    OUT / 'django14999-recovery',
    OUT / 'first5-case3/run-r2/instances' / ids[2],
    OUT / 'first5-case4/run-r2/instances' / ids[3],
    OUT / 'first5-case5/run-r2/instances' / ids[4],
]
rows = []
for n, (instance, root) in enumerate(zip(ids[:5], roots)):
    reports = list(root.glob('runs/*/report.json'))
    evaluations = list(root.glob('official-evaluation*.json'))
    assert len(reports) == 1 and len(evaluations) <= 1, (instance, reports, evaluations)
    report_path = reports[0]
    report = read(report_path)
    evaluation_path = evaluations[0] if evaluations else None
    evaluation = read(evaluation_path) if evaluation_path else {}
    if evaluation:
        assert evaluation['official_verdict'] in ('RESOLVED', 'UNRESOLVED')
        assert evaluation['instance_id'] == instance
        assert evaluation['official']['exit_code'] == 0
    else:
        batch = read(root.parents[1] / 'batch-state.json')
        assert batch['status'] == 'completed' and batch['instances'][0]['status'] == 'NO_PATCH'
        assert not report['git_diff'] and not report['modified_files']
    ledgers = list(root.glob('dispatch-ledger*.json'))
    recovery = []
    runtime = money(report['runtime_seconds'])
    if n == 1:
        initial = OUT / 'segment2/run/instances' / instance
        ledgers.extend(initial.glob('dispatch-ledger*.json'))
        initial_report = next(initial.glob('runs/*/report.json'))
        runtime += money(read(initial_report)['runtime_seconds'])
        recovery.append('pytest absent in official internal image; install pytest7.4.4 in derived image; reuse index/localization; initial paid requests retained')
    if n in (2, 3, 4):
        recovery.append('unpaid index process stopped under memory pressure; restarted with at most two inference workers')
    settled = Decimal(0)
    reserved = Decimal(0)
    request_count = 0
    ledger_sources = []
    for ledger_path in sorted(set(ledgers)):
        ledger = read(ledger_path)
        assert not ledger['frozen'], ledger_path
        for request in ledger['requests']:
            request_count += 1
            assert request['model'] == 'glm-5.3-flash'
            if request['status'] == 'settled':
                settled += money(request['cost_cny'])
            else:
                reserved += money(request['reserved_cny'])
        ledger_sources.append(dict(path=relative(ledger_path), sha256=digest(ledger_path), requests=len(ledger['requests'])))
    assert reserved == 0
    prediction = evaluation.get('prediction', {})
    process_path = root / 'process.json'
    code_sha = read(process_path).get('code_sha') if process_path.exists() else None
    if n == 0:
        code_sha = '9ef018d70ecf9346896557ce6c19e5a2f2925f8d'
    if n == 1:
        code_sha = '80b453a30a93775f3f51dad96b76042aabcb8995'
    row = dict(order=n+1, instance_id=instance, dispatched=True, model='bigmodel/glm-5.3-flash', code_sha=code_sha,
        requests=request_count, settled_cny=float(settled.quantize(Decimal('0.00000001'))), unknown_reserved_cny=float(reserved),
        runtime_seconds=float(runtime), internal_status=report['status'], stop_reason=report['stop_reason'],
        legal_candidate=bool(evaluation), patch_sha256=evaluation.get('patch_sha256') or prediction.get('patch_sha256'), official=evaluation.get('official_verdict','NOT_EVALUATED_NO_PATCH'),
        report=relative(report_path), official_evaluation=relative(evaluation_path) if evaluation_path else None, ledger_sources=ledger_sources,
        recovery=recovery)
    if evaluation:
        assert row['patch_sha256'] and len(row['patch_sha256']) == 64
    else:
        state = read(report_path.parent / 'state_final.json')
        assert len(state['tool_calls']) == 30
        assert all(call['ok'] or call['error_code'] == 'invalid_arguments' for call in state['tool_calls'])
        assert all(call['name'] in ('read_file','search_code','get_git_diff') for call in state['tool_calls'])
        row['reason'] = 'NO_PATCH: 30 read-only coder tool calls (29 succeeded, one model JSON argument error) exhausted configured local tool budget; no workspace/code patch generated'
    rows.append(row)

new_settled = sum((money(row['settled_cny']) for row in rows), Decimal(0))
new_unknown = sum((money(row['unknown_reserved_cny']) for row in rows), Decimal(0))
upper = money(old['authorized_upper_cny']) + new_settled + new_unknown
assert upper <= 30
versions = {}
for row in rows:
    group = versions.setdefault(row['code_sha'], dict(dispatched=0, official=0, resolved=0))
    group['dispatched'] += 1
    group['official'] += row['legal_candidate']
    group['resolved'] += row['official'] == 'RESOLVED'
resolved = sum(row['official']=='RESOLVED' for row in rows)
result = dict(generated_at=datetime.now(timezone.utc).isoformat(), scope='User narrowed to fixed first5, then evaluate remaining45; no additional dispatch authorized by this audit',
    catalog=relative(catalog_path), catalog_sha256=digest(catalog_path), first5=rows,
    remaining45=[dict(instance_id=instance, status='NOT_RUN_USER_SCOPE_CHANGED') for instance in ids[5:]],
    statistics=dict(dispatched=5, original_catalog_size=50, dispatched_coverage=0.1, valid_official=4,
        official_coverage_of_legal_candidates=1.0, official_coverage_of_dispatched=0.8, official_coverage_of_original_catalog=0.08,
        resolved=resolved, resolved_over_50=resolved/50, resolved_over_first5=resolved/5, resolved_over_valid_official=resolved/4, by_version=versions,
        no_patch=1, final_environment_invalid=0, final_system_error=0),
    fees=dict(currency='CNY', original_authorization=30, prior_original30_upper=float(money(old['authorized_upper_cny'])),
        prior_original30_settled=float(prior_settled), prior_original30_unknown_reserved=float(prior_unknown),
        new_settled=float(new_settled), new_unknown_reserved=float(new_unknown), original30_upper=float(upper),
        original30_remaining=float(Decimal(30)-upper), historical_all_sources_settled=opening['settled_cny'],
        historical_all_sources_unknown_reserved=opening['unknown_reserved_cny'], historical_unknown_calls=opening['unknown_cost_calls'],
        all_sources_upper=float(money(opening['total_upper_bound_cny'])+new_settled+new_unknown),
        note='Historical opening totals include older authorizations; do not add that full opening to original30. Each new ledger counted once; replicated opening fields excluded.'))
(OUT / 'first5-results.json').write_text(json.dumps(result, indent=2, ensure_ascii=False)+'\n', encoding='utf8')
columns = ['order','instance_id','code_sha','requests','settled_cny','unknown_reserved_cny','runtime_seconds','internal_status','stop_reason','legal_candidate','patch_sha256','official']
with (OUT / 'first5-results.csv').open('w', encoding='utf-8-sig', newline='') as stream:
    writer = csv.DictWriter(stream, fieldnames=columns, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)
print(json.dumps(dict(statistics=result['statistics'], fees=result['fees']), indent=2))
