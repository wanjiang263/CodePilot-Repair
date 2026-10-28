"""Grade the completed recovery candidate, without any model invocation."""
import json
import subprocess
import time
from pathlib import Path

project = Path(__file__).resolve().parents[2]
root = project / 'artifacts/phase25/django14999-recovery'
while json.loads((root / 'process.json').read_text(encoding='utf8'))['status'] == 'running':
    time.sleep(10)
ledger = json.loads((root / 'dispatch-ledger.json').read_text(encoding='utf8'))
assert not ledger['frozen'], 'Keep unknown reserve and investigate frozen segment before grading'
receipt = root / 'runs/phase25-django14999-pytest-recovery/official-candidate.json'
assert receipt.is_file(), 'No legal candidate receipt; inspect recovery outcome'
base = '/mnt/d/Projects/Desktop/CodePilot-Repair'
target = base + '/artifacts/phase25/django14999-recovery'
argv = ['wsl', '-d', 'Ubuntu-24.04', '--', 'env',
    'DOCKER_HOST=unix:///mnt/wsl/docker-desktop/shared-sockets/guest-services/docker.proxy.sock',
    'OPENBLAS_NUM_THREADS=1', 'OMP_NUM_THREADS=1', base+'/.venv-linux/bin/python',
    base+'/scripts/l2cal10e_official.py', '--root', target,
    '--instance', 'django__django-14999', '--label', 'phase25-django14999-recovery',
    '--run-id', 'phase25-django14999-pytest-recovery', '--snapshot-root', base+'/cache/swebench',
    '--harness-python', base+'/.venv-swebench-linux/bin/python',
    '--candidate-receipt', target+'/runs/phase25-django14999-pytest-recovery/official-candidate.json',
    '--output', target+'/official-evaluation.json']
(root/'official-invocation.json').write_text(json.dumps(argv, indent=2)+'\n', encoding='utf8')
with (root/'official.log').open('wb') as stream:
    result = subprocess.run(argv, cwd=project, stdout=stream, stderr=subprocess.STDOUT, check=False)
(root/'official-process-result.json').write_text(json.dumps(dict(exit_code=result.returncode))+'\n', encoding='utf8')
raise SystemExit(result.returncode)
