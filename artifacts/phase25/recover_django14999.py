import hashlib
import json
import os
import time
from dataclasses import replace
from pathlib import Path

from dotenv import load_dotenv
from codepilot.eval.assembly import InferenceAssembly, ProviderBundle
from codepilot.eval.candidate import prepare_candidate
from codepilot.eval.swebench import SWEBenchLoader
from codepilot.llm.config import load_llm_config
from codepilot.llm.dispatch_budget import DispatchBudget, configure_dispatch_budget
from codepilot.llm.registry import transport_provider
from codepilot.llm.settled_cache import SettledResponseCache, settled_archive_eligible
from codepilot.repo.workspace import WorkspaceManager
from codepilot.sandbox.official import OfficialInstanceSandbox
from codepilot.sandbox.runners import RunnerOverrides
from codepilot.sandbox.swebench_env import SWEBenchInstanceImageResolver
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.run_config import IndexSnapshot, InferenceRunConfig, StageRecovery

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'artifacts/phase25'
OLD = OUT / 'segment2/run/instances/django__django-14999'
PREVIOUS = next((OLD / 'runs').iterdir())
SEGMENT = OUT / 'django14999-recovery'
SEGMENT.mkdir(exist_ok=False)

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def write(path, value):
    path.write_bytes((json.dumps(value, indent=2, default=str) + '\n').encode())

load_dotenv(ROOT / '.env', override=False)
source_ledger = next(OLD.glob('dispatch-ledger-*.json'))
old_ledger = json.loads(source_ledger.read_text(encoding='utf8'))
assert not old_ledger['frozen'] and all(r['status'] == 'settled' for r in old_ledger['requests'])
old_cost = sum(r['cost_cny'] for r in old_ledger['requests'])
remaining = 2.0 - old_cost
assert remaining > 0.8114688
history = {str(source_ledger): sha(source_ledger)}
for record in json.loads((OUT / 'opening-audit.json').read_text(encoding='utf8'))['sources'].items():
    name, expected = record
    assert sha(ROOT / name) == expected
    history[str(ROOT / name)] = expected
first = OUT / 'run/instances/sphinx-doc__sphinx-8506/dispatch-ledger-phase25-test50-r1.json'
history[str(first)] = sha(first)
budget = DispatchBudget(remaining, SEGMENT / 'dispatch-ledger.json', opening_source=dict(kind='phase25_purposeful_recovery', historical_sources=history, instance_used_cny=old_cost, instance_grant_cny=2, remaining_instance_grant_cny=remaining, note='Other active first5 grants remain disjoint; no new authorization.'))
configure_dispatch_budget(budget)
task = SWEBenchLoader(ROOT / 'cache/swebench', revision='6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2').get_inference_task('test', 'django__django-14999')
llm = load_llm_config(ROOT / 'configs/phase22/llm.yaml').for_model('glm-5.3-flash-main', require_temperature=True)
providers = ProviderBundle.live(llm, budget_currency='CNY', record_llm=True)
transport = []
for role, chain in providers.roles.items():
    actual = transport_provider(chain)
    assert actual.spec.model == 'glm-5.3-flash' and actual.provider.name == 'bigmodel' and actual.policy.max_attempts == 1
    assert actual._client.trust_env is False
    transport.append(dict(role=role, model=actual.spec.model, retries=0, timeout=actual._client.timeout.as_dict(), trust_env=False))
write(SEGMENT / 'actual-client.json', transport)
archive = PREVIOUS / 'llm_calls.jsonl'
assert archive.is_file() and settled_archive_eligible([source_ledger])
providers = replace(providers, roles={role: SettledResponseCache(chain, archive=archive, alias='glm-5.3-flash-main', evidence=SEGMENT / 'settled-cache-hits.jsonl') for role, chain in providers.roles.items()})
images = SWEBenchInstanceImageResolver.load(OUT / 'django14999-image-overrides.json')
runners = RunnerOverrides.load(OUT / 'segment2/runners.json')
manager = WorkspaceManager(cache_root=OLD / 'repo-cache', default_mode='copy')

def sandbox(artifacts):
    return OfficialInstanceSandbox(artifacts=artifacts, allowed_workspace_root=SEGMENT, instance_id=task.instance_id, images=images, runners=runners, bind_workspace=True)

state = PREVIOUS / 'state_final.json'
raw = json.loads(state.read_text(encoding='utf8'))
runtime = InferenceRunConfig(budget=RepairBudget(max_iterations=5, max_test_runs=18, max_review_calls=2, max_runtime_seconds=1800, max_total_tokens=4_000_000, max_cost=remaining), index_snapshot=IndexSnapshot(path=PREVIOUS / 'index.sqlite3', sha256=sha(PREVIOUS / 'index.sqlite3')), recovery=StageRecovery(source_state=state, source_artifacts=PREVIOUS, source_state_sha256=sha(state), source_run_id=raw['run_id'], entry='build_env', historical_ledger_hashes=history, reason='Actual official image lacked pytest; derived image installs pytest7.4.4 only. Reuse settled localization and complete source index; rebuild native baseline under new image; retry saved reproduction through exact settled-response cache.', previous_runtime_seconds=1953.9917786121368))
write(SEGMENT / 'config.json', runtime.model_dump(mode='json'))
run_id = 'phase25-django14999-pytest-recovery'
process = dict(pid=os.getpid(), started_at=time.time(), status='running', source_ledger_sha256=sha(source_ledger), instance_total_cap_cny=2, new_limit_cny=remaining)
write(SEGMENT / 'process.json', process)
try:
    outcome = InferenceAssembly(task=task, workspace_manager=manager, work_root=SEGMENT, sandbox_factory=sandbox, provider_bundle=providers, runtime_config=runtime, expected_budget=runtime.budget, record_llm=True).run(run_id=run_id, seed=0)
    receipt = prepare_candidate(project=ROOT, run=SEGMENT / 'runs' / run_id, instance=task.instance_id, base=task.base_commit, source_ledger=SEGMENT / 'dispatch-ledger.json', revision='6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2', model_identity='glm-5.3-flash-main')
    write(SEGMENT / 'outcome.json', dict(status=str(outcome.status), stop_reason=str(outcome.stop_reason), receipt=str(receipt) if receipt else None, requests=len(budget.records), ledger_frozen=budget.frozen))
    print('recovery completed', outcome.status, receipt, flush=True)
finally:
    process.update(status='exited', finished_at=time.time(), ledger_frozen=budget.frozen, requests=len(budget.records))
    write(SEGMENT / 'process.json', process)
