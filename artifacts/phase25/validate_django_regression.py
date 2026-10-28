import hashlib
import json
import subprocess
from pathlib import Path

from codepilot.artifacts import ArtifactStore
from codepilot.eval.swebench import SWEBenchLoader
from codepilot.sandbox.official import OfficialInstanceSandbox
from codepilot.sandbox.runners import RunnerOverrides, parse_django
from codepilot.sandbox.swebench_env import SWEBenchInstanceImageResolver

ROOT = Path(__file__).resolve().parents[2]
segment = ROOT / 'artifacts/phase25/django14999-recovery'
run = segment / 'runs/phase25-django14999-pytest-recovery'
report = json.loads((run / 'report.json').read_text(encoding='utf8'))
workspace = run / 'workspace'
source = run / 'attempts/1012'
requested = tuple(report['baseline']['outcomes'])
parsed = parse_django((source/'stdout.log').read_text(encoding='utf8'), (source/'stderr.log').read_text(encoding='utf8'), exit_code=None, workspace=workspace, requested=requested, timed_out=True)
out = segment / 'postrun-validation'
out.mkdir(exist_ok=False)
(out/'original-timeout-reclassification.json').write_text(json.dumps(dict(status=parsed.status.value, error=parsed.error_reason, parsed=len(parsed.tests), missing=len(parsed.diagnostics['requested_missing']), original_status=report['regression']['status'], source='attempts/1012'), indent=2)+'\n', encoding='utf8')
assert len(parsed.tests) == 228 and len(parsed.diagnostics['requested_missing']) == 386
before = subprocess.check_output(['git', '-C', str(workspace), 'diff', '--binary', 'HEAD'])
task = SWEBenchLoader(ROOT/'cache/swebench', revision='6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2').get_inference_task('test', 'django__django-14999')
artifacts = ArtifactStore(out, 'same-candidate-full-regression')
backend = OfficialInstanceSandbox(artifacts=artifacts, allowed_workspace_root=segment, instance_id=task.instance_id, images=SWEBenchInstanceImageResolver.load(ROOT/'artifacts/phase25/django14999-image-overrides.json'), runners=RunnerOverrides.load(ROOT/'artifacts/phase25/segment2/runners.json'), bind_workspace=True)
spec = task.env_spec.model_copy(update={'timeout_seconds': 600})
environment = backend.build_env(repository=str(task.repository), base_commit=task.base_commit, workspace=workspace, env_spec=spec)
result = backend.run_tests(workspace=workspace, environment=environment, env_spec=spec, tests=list(requested), artifact_iteration=1)
after = subprocess.check_output(['git', '-C', str(workspace), 'diff', '--binary', 'HEAD'])
assert before == after, 'Candidate changed during regression'
payload = dict(candidate_unchanged=True, git_diff_sha256=hashlib.sha256(before).hexdigest(), requested=len(requested), execution=result.model_dump(mode='json'), model_requests=0, original_internal_status_preserved=True)
(out/'full-regression.json').write_text(json.dumps(payload, indent=2)+'\n', encoding='utf8')
print(result.status, len(result.tests), result.error_reason, flush=True)
assert result.exit_code == 0 and len(result.tests) == len(requested) and result.tests_failed == 0
