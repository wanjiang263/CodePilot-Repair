"""One frozen independent development case, run by the existing full Agent graph."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv

from codepilot.eval.assembly import InferenceAssembly, ProviderBundle
from codepilot.llm.config import load_llm_config
from codepilot.llm.dispatch_budget import DispatchBudget, configure_dispatch_budget
from codepilot.repo.workspace import WorkspaceManager
from codepilot.sandbox.base import EnvironmentBuildResult
from codepilot.sandbox.docker import DockerSandbox
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.enums import ExecutionStatus

FILES = {
    "README.md": """# DraftStore
An in-memory draft catalogue used by a command-line report.

Public contract: put(key, text) adds a new item and rejects duplicate keys with
ValueError. update(key, text) replaces an existing item and raises KeyError for
missing keys. Replacement retains insertion order. render(key, prefix='') must
show the latest text. Both positional and keyword prefix calls are supported.
snapshot() returns an independent ordered list of (key, text) pairs.
""",
    "drafts/__init__.py": "from .store import DraftStore\n",
    "drafts/formatting.py": "def render_text(key, text, prefix=''):\n    return f'{prefix}{key}: {text}'\n",
    "drafts/store.py": """from .formatting import render_text


class DraftStore:
    def __init__(self):
        self._items = {}
        self._rendered = {}

    def put(self, key, text):
        if key in self._items:
            raise ValueError(key)
        self._items[key] = text

    def update(self, key, text):
        if key not in self._items:
            raise KeyError(key)
        self._items[key] = text

    def render(self, key, prefix=''):
        identity = (key, prefix)
        if identity not in self._rendered:
            self._rendered[identity] = render_text(key, self._items[key], prefix)
        return self._rendered[identity]

    def snapshot(self):
        return list(self._items.items())
""",
    "tests/test_store.py": """import pytest
from drafts import DraftStore


def test_render_observes_update_for_every_prefix():
    store = DraftStore()
    store.put('a', 'old')
    assert store.render('a') == 'a: old'
    assert store.render('a', '>') == '>a: old'
    store.update('a', 'new')
    assert store.render('a') == 'a: new'
    assert store.render('a', prefix='>') == '>a: new'


def test_update_preserves_order_and_snapshot_is_detached():
    store = DraftStore()
    store.put('a', 'one')
    store.put('b', 'two')
    snapshot = store.snapshot()
    store.update('a', 'three')
    assert store.snapshot() == [('a', 'three'), ('b', 'two')]
    assert snapshot == [('a', 'one'), ('b', 'two')]


def test_duplicate_and_missing_keys_keep_exception_contract():
    store = DraftStore()
    store.put('a', 'one')
    with pytest.raises(ValueError):
        store.put('a', 'two')
    with pytest.raises(KeyError):
        store.update('b', 'two')
    with pytest.raises(KeyError):
        store.render('missing')


def test_other_item_and_prefix_compatibility():
    store = DraftStore()
    store.put('a', 'one')
    store.put('b', 'two')
    assert store.render('b', '#') == '#b: two'
    store.update('a', 'three')
    assert store.render('b', prefix='#') == '#b: two'
    assert store.render('a', '') == 'a: three'
""",
}
ISSUE = """DraftStore shows stale output after editing an already rendered draft.
Reproduce: put('a', 'old'), render('a'), update('a', 'new'), render('a') still
returns 'a: old'. Fix this behavior while retaining the public API and all
compatibility behavior in README.md. Multiple prefixes can be rendered for one
key. Preserve the other entries, ordering, snapshot independence and exception
types. Run the target tests and regression suite; do not edit existing tests.
"""


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    out = root / "artifacts/phase27/agent-case"
    out.mkdir(exist_ok=True)
    source = out / "source"
    source.mkdir(exist_ok=False)
    for relative, content in FILES.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf8", newline="\n")

    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(source), *args], text=True).strip()

    git("init")
    git("add", ".")
    git(
        "-c",
        "user.name=CodePilot Validation",
        "-c",
        "user.email=validation@example.invalid",
        "commit",
        "-m",
        "Freeze independent DraftStore development case",
    )
    base = git("rev-parse", "HEAD")
    load_dotenv(root / ".env")
    config = load_llm_config(root / "configs/phase22/llm.yaml").for_model("glm-5.3-flash-main")
    assert set(config.roles.values()) == {"glm-5.3-flash-main"}
    assert config.providers["bigmodel"].max_retries == 0
    # Reconcile all prior round records, including the frozen cancellation.
    prior = 0.0
    for name in ("glm-normal", "glm-cancel"):
        ledger = json.loads((out.parent / name / "dispatch-ledger.json").read_text())
        prior += sum(
            float(r["cost_cny"] if r["status"] == "settled" else r["reserved_cny"])
            for r in ledger["requests"]
        )
    limit = (
        min(
            3.0, float(json.loads((out.parent / "opening-audit.json").read_text())["remaining_cny"])
        )
        - prior
    )
    budget = RepairBudget(
        max_iterations=2,
        max_test_runs=12,
        max_tool_calls_per_iteration=16,
        max_review_calls=2,
        max_runtime_seconds=900,
        max_cost=limit,
    )
    image = "phase25/django-14999:pytest744"
    import docker

    client = docker.from_env()
    image_id = client.images.get(image).id
    env = EnvSpec(
        timeout_seconds=40,
        test_command=(
            "/opt/miniconda3/envs/testbed/bin/python -m pytest -p no:cacheprovider --junitxml=/out/junit.xml"
        ),
        env={"PYTHONPATH": "/workspace"},
    )
    frozen = {
        "issue": ISSUE,
        "base": base,
        "source_sha256": {
            name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in FILES
        },
        "env": env.model_dump(mode="json"),
        "image": image,
        "image_id": image_id,
        "budget": budget.model_dump(mode="json"),
        "prior_round_upper": prior,
        "segment_limit": limit,
        "model": "bigmodel/glm-5.3-flash",
        "config_sha256": hashlib.sha256(
            (root / "configs/phase22/llm.yaml").read_bytes()
        ).hexdigest(),
    }
    (out / "frozen-case.json").write_text(json.dumps(frozen, indent=2) + "\n", encoding="utf8")

    class CaseSandbox(DockerSandbox):
        def build_env(self, **_kwargs):
            assert self.client.images.get(image).id == image_id
            return EnvironmentBuildResult(
                status=ExecutionStatus.PASS,
                image_ref=image,
                image_id=image_id,
                cache_key="phase27-frozen-existing-image",
                from_cache=True,
            )

    configure_dispatch_budget(
        DispatchBudget(
            limit,
            out / "dispatch-ledger.json",
            opening_source={
                "round_cap": 3,
                "prior_round_upper": prior,
                "frozen_case": str(out / "frozen-case.json"),
            },
        )
    )
    bundle = ProviderBundle.live(config, record_llm=True)
    try:
        assembly = InferenceAssembly(
            task=SimpleNamespace(
                task_id="phase27-draftstore",
                issue=ISSUE,
                base_commit=base,
                repository=source,
                env_spec=env,
                mode="user",
            ),
            workspace_manager=WorkspaceManager(cache_root=out / "cache"),
            work_root=out,
            sandbox_factory=lambda artifacts: CaseSandbox(
                artifacts=artifacts, allowed_workspace_root=out, client=client, bind_workspace=True
            ),
            provider_bundle=bundle,
            budget=budget,
            record_llm=True,
            checkpoint=True,
            allow_build_network=False,
        )
        outcome = assembly.run(run_id="phase27-draftstore", seed=0)
        (out / "outcome.json").write_text(
            json.dumps(asdict(outcome), indent=2, default=str) + "\n", encoding="utf8"
        )
        print(outcome.status, outcome.stop_reason, flush=True)
    finally:
        bundle.close()
        client.close()


if __name__ == "__main__":
    main()
