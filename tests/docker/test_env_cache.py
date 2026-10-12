"""S-23：真实 Docker 环境镜像缓存验收。

同一 ``(repository, base_commit, EnvSpec)`` 连续构建两次：第一次必须真实构建
（有构建容器、有依赖安装），第二次必须命中同一镜像且**不产生任何新容器**——
这是"没有重复执行依赖安装"最直接的行为证据。所有耗时都是本机实测。
"""

from __future__ import annotations

from typing import Any

import pytest
from docker.errors import ImageNotFound

from codepilot.sandbox.docker import environment_cache_key
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.schemas.enums import ExecutionStatus
from conftest import spec_with

CACHE_ENV_SPEC = EnvSpec(
    install=("python -m pip install pytest",),
    timeout_seconds=120,
)
CHANGED_ENV_SPEC = CACHE_ENV_SPEC.model_copy(
    update={"install": ("python -m pip install pytest", "python -m pip install six")}
)
PASSING_TEST = "def test_cache_probe():\n    assert True\n"


def _build(
    sandbox: Any,
    repo: Any,
    *,
    env_spec: EnvSpec,
    base_commit: str | None = None,
    repository: str | None = None,
    allow_network: bool = True,
) -> Any:
    return sandbox.build_env(
        repository=repository or str(repo.path.resolve()),
        base_commit=base_commit or repo.head,
        workspace=repo.path,
        env_spec=env_spec,
        allow_network=allow_network,
    )


@pytest.mark.docker
def test_second_build_hits_same_image_without_reinstalling(
    docker_client: Any,
    fixture_workspace: Any,
    make_sandbox: Any,
    observer: Any,
    acceptance_run_id: str,
    record_evidence: Any,
) -> None:
    repo = fixture_workspace(
        {"test_cache_probe.py": PASSING_TEST},
    )
    cold_sandbox, cold_client = make_sandbox()
    cold = _build(cold_sandbox, repo, env_spec=CACHE_ENV_SPEC)
    warm_sandbox, warm_client = make_sandbox()
    warm = _build(warm_sandbox, repo, env_spec=CACHE_ENV_SPEC)

    run = cold_sandbox.run_tests(
        workspace=repo.path,
        environment=warm,
        env_spec=CACHE_ENV_SPEC,
        tests=("test_cache_probe.py::test_cache_probe",),
    )

    record_evidence(
        "S-23-cache-hit",
        {
            "cache_key": cold.cache_key,
            "image_ref": cold.image_ref,
            "cold_image_id": cold.image_id,
            "warm_image_id": warm.image_id,
            "cold_duration_seconds": round(cold.duration_seconds, 3),
            "warm_duration_seconds": round(warm.duration_seconds, 3),
            "cold_from_cache": cold.from_cache,
            "warm_from_cache": warm.from_cache,
            "cold_build_containers_created": len(cold_client.created_ids),
            "warm_build_containers_created": len(warm_client.created_ids),
            "daemon_containers_seen_warm": observer.ids_for_run(acceptance_run_id),
            "run_status": run.status.value,
            "run_tests_passed": run.tests_passed,
        },
    )

    # 1) 第一次：真实冷构建，确实建了构建容器并提交了镜像。
    assert cold.status is ExecutionStatus.PASS, cold.error
    assert cold.from_cache is False
    assert cold.image_ref and cold.image_id
    assert cold_client.created_ids, "冷构建没有创建任何容器，无法证明依赖真的装过"

    # 2) 第二次：命中同一镜像。
    assert warm.status is ExecutionStatus.PASS, warm.error
    assert warm.from_cache is True
    assert warm.cache_key == cold.cache_key
    assert warm.image_ref == cold.image_ref
    assert warm.image_id == cold.image_id, "缓存命中却拿到不同镜像"

    # 3) 命中时没有任何新容器 → 依赖安装没有重复执行。
    assert warm_client.created_ids == [], f"缓存命中仍创建了容器: {warm_client.created_ids}"

    # 4) 实测耗时：命中必须显著快于冷构建。
    assert warm.duration_seconds < cold.duration_seconds

    # 5) 缓存镜像可用：真实执行通过。
    assert run.status is ExecutionStatus.PASS, run.model_dump()
    assert run.tests_passed == 1


@pytest.mark.docker
def test_cache_identity_changes_with_envspec_commit_and_repository(
    fixture_workspace: Any,
    make_sandbox: Any,
    record_evidence: Any,
) -> None:
    """EnvSpec / base commit / repository 任一变化都必须让缓存身份变化。"""
    repo = fixture_workspace({"test_cache_probe.py": PASSING_TEST})
    sandbox, _client = make_sandbox()
    built = _build(sandbox, repo, env_spec=CACHE_ENV_SPEC)
    assert built.status is ExecutionStatus.PASS, built.error

    repo.write("extra.py", "VALUE = 1\n")
    second_commit = repo.commit("second")
    other_repo = fixture_workspace({"test_other.py": PASSING_TEST}, name="other-workspace")

    variants = {
        "env_spec_changed": {
            "env_spec": CHANGED_ENV_SPEC,
            "base_commit": repo.head,
            "repository": str(repo.path.resolve()),
            "key": environment_cache_key(str(repo.path.resolve()), repo.head, CHANGED_ENV_SPEC),
        },
        "base_commit_changed": {
            "env_spec": CACHE_ENV_SPEC,
            "base_commit": second_commit,
            "repository": str(repo.path.resolve()),
            "key": environment_cache_key(str(repo.path.resolve()), second_commit, CACHE_ENV_SPEC),
        },
        "repository_changed": {
            "env_spec": CACHE_ENV_SPEC,
            "base_commit": repo.head,
            "repository": str(other_repo.path.resolve()),
            "key": environment_cache_key(str(other_repo.path.resolve()), repo.head, CACHE_ENV_SPEC),
        },
    }

    outcomes: dict[str, dict[str, Any]] = {}
    for name, variant in variants.items():
        probe_sandbox, _probe_client = make_sandbox()
        # 不许联网重建：只问 daemon "这条身份是否已有可用镜像"。
        miss = _build(
            probe_sandbox,
            repo,
            env_spec=variant["env_spec"],
            base_commit=variant["base_commit"],
            repository=variant["repository"],
            allow_network=False,
        )
        outcomes[name] = {
            "cache_key": miss.cache_key,
            "status": miss.status.value,
            "from_cache": miss.from_cache,
            "hit_image": miss.image_ref,
            "error": miss.error,
        }
        # 身份变了：新 key，且不得复用已缓存镜像。
        assert miss.cache_key != built.cache_key, name
        assert miss.cache_key == variant["key"], name
        assert miss.status is ExecutionStatus.ENV_ERROR, outcomes[name]
        assert miss.from_cache is False, outcomes[name]
        assert miss.image_ref is None, outcomes[name]

    record_evidence(
        "S-23-cache-identity",
        {
            "baseline_cache_key": built.cache_key,
            "baseline_image_ref": built.image_ref,
            "variants": outcomes,
        },
    )


@pytest.mark.docker
def test_retagged_image_is_not_reused_for_another_repository(
    docker_client: Any,
    fixture_workspace: Any,
    make_sandbox: Any,
    record_evidence: Any,
) -> None:
    """缓存不会错误复用其他仓库的环境：标签身份必须一并匹配。"""
    repo = fixture_workspace({"test_cache_probe.py": PASSING_TEST})
    sandbox, _client = make_sandbox()
    built = _build(sandbox, repo, env_spec=CACHE_ENV_SPEC)
    assert built.status is ExecutionStatus.PASS, built.error

    other_repository = "file:///somewhere/else/repo"
    borrowed_key = environment_cache_key(other_repository, repo.head, CACHE_ENV_SPEC)
    image = docker_client.images.get(built.image_ref or "")
    image.tag("codepilot-env", borrowed_key)
    try:
        probe_sandbox, _probe_client = make_sandbox()
        miss = _build(
            probe_sandbox,
            repo,
            env_spec=CACHE_ENV_SPEC,
            repository=other_repository,
            allow_network=False,
        )
        record_evidence(
            "S-23-foreign-repository",
            {
                "baseline_cache_key": built.cache_key,
                "borrowed_key": borrowed_key,
                "status": miss.status.value,
                "from_cache": miss.from_cache,
                "hit_image": miss.image_ref,
            },
        )
        assert miss.status is ExecutionStatus.ENV_ERROR, miss.model_dump()
        assert miss.from_cache is False
    finally:
        borrowed_ref = f"codepilot-env:{borrowed_key}"
        docker_client.api.remove_image(borrowed_ref, force=False)
        with pytest.raises(ImageNotFound):
            docker_client.images.get(borrowed_ref)


@pytest.mark.docker
def test_missing_image_is_rebuilt_and_cached_again(
    docker_client: Any,
    fixture_workspace: Any,
    make_sandbox: Any,
    record_evidence: Any,
) -> None:
    """镜像缺失时可以正常重建，重建后再次命中。"""
    repo = fixture_workspace({"test_cache_probe.py": PASSING_TEST})
    first_sandbox, _first_client = make_sandbox()
    first = _build(first_sandbox, repo, env_spec=CACHE_ENV_SPEC)
    assert first.status is ExecutionStatus.PASS, first.error

    # 只删除本轮这条 CodePilot 自有镜像：先核对归属，再按精确引用删除。
    image = docker_client.images.get(first.image_ref or "")
    labels = (image.attrs.get("Config", {}) or {}).get("Labels", {}) or {}
    assert labels.get("codepilot") == "1", labels
    assert labels.get("codepilot.cache_key") == first.cache_key
    docker_client.api.remove_image(first.image_ref or "", force=False)

    rebuild_sandbox, rebuild_client = make_sandbox()
    rebuilt = _build(rebuild_sandbox, repo, env_spec=CACHE_ENV_SPEC)
    rehit_sandbox, rehit_client = make_sandbox()
    rehit = _build(rehit_sandbox, repo, env_spec=CACHE_ENV_SPEC)
    run = rebuild_sandbox.run_tests(
        workspace=repo.path,
        environment=rebuilt,
        env_spec=CACHE_ENV_SPEC,
        tests=("test_cache_probe.py::test_cache_probe",),
    )

    record_evidence(
        "S-23-cache-rebuild",
        {
            "first_cache_key": first.cache_key,
            "first_image_id": first.image_id,
            "first_duration_seconds": round(first.duration_seconds, 3),
            "rebuilt_from_cache": rebuilt.from_cache,
            "rebuilt_image_id": rebuilt.image_id,
            "rebuilt_duration_seconds": round(rebuilt.duration_seconds, 3),
            "rebuilt_containers_created": len(rebuild_client.created_ids),
            "rehit_from_cache": rehit.from_cache,
            "rehit_duration_seconds": round(rehit.duration_seconds, 3),
            "rehit_containers_created": len(rehit_client.created_ids),
            "run_status": run.status.value,
        },
    )

    assert rebuilt.status is ExecutionStatus.PASS, rebuilt.error
    assert rebuilt.from_cache is False
    assert rebuilt.cache_key == first.cache_key
    assert rebuild_client.created_ids, "重建没有真的建构建容器"
    assert rehit.from_cache is True
    assert rehit.image_id == rebuilt.image_id
    assert rehit_client.created_ids == []
    assert run.status is ExecutionStatus.PASS, run.model_dump()


@pytest.mark.docker
def test_cli_shows_cache_hit_on_the_second_run_of_the_same_identity(
    fixture_workspace: Any,
    make_sandbox: Any,
    record_evidence: Any,
) -> None:
    """同一次进程里连续两次 build_env：第二次直接命中，不做任何容器工作。"""
    repo = fixture_workspace({"test_cache_probe.py": PASSING_TEST})
    sandbox, client = make_sandbox()
    first = _build(sandbox, repo, env_spec=spec_with())
    second = _build(sandbox, repo, env_spec=spec_with())
    record_evidence(
        "S-23-same-sandbox-instance",
        {
            "cache_key": first.cache_key,
            "first_from_cache": first.from_cache,
            "second_from_cache": second.from_cache,
            "first_duration_seconds": round(first.duration_seconds, 3),
            "second_duration_seconds": round(second.duration_seconds, 3),
            "containers_created_total": len(client.created_ids),
        },
    )
    assert first.from_cache is False
    assert second.from_cache is True
    assert second.image_id == first.image_id
    # 只有冷构建那一次创建了构建容器。
    assert len(client.created_ids) == 1
