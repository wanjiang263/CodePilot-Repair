"""Offline mini_auth service acceptance; synthetic vectors, never a quality evaluation."""

import json
import sys
import tempfile
from pathlib import Path

import httpx

from codepilot.llm.config import ProviderSpec
from codepilot.repo.index import Indexer, RepositoryIndex
from codepilot.repo.workspace import Workspace, normalize_source
from codepilot.retrieval.embeddings import CachedEmbeddingProvider, OpenAICompatibleEmbeddingProvider
from codepilot.retrieval.service import retrieve_symbols
from codepilot.storage.db import create_engine_from_url, create_session_factory, init_db
from codepilot.storage.repository import Stores

root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(root / "tests"))
from support.gitfixture import init_repo  # noqa: E402

evidence = Path(__file__).parent
fixture = root / "tests/fixtures/repos/mini_auth"
issue = "login fails for inactive users"
target = "miniauth.session.Session.login"
requests = []


def handler(request):
    inputs = json.loads(request.content)["input"]
    requests.append(len(inputs))
    assert len(inputs) <= 7
    data = []
    for index, text in enumerate(inputs):
        match = text == issue or f"\nsymbol: {target}\n" in text
        data.append({"index": index, "embedding": [1, 0] if match else [0, 1]})
    return httpx.Response(200, json={"data": list(reversed(data))})


files = {
    path.relative_to(fixture).as_posix(): path.read_text(encoding="utf-8")
    for path in fixture.rglob("*")
    if path.is_file() and path.suffix in {".py", ".toml", ".md"}
}
engine = create_engine_from_url("sqlite://")
init_db(engine)
try:
    with tempfile.TemporaryDirectory(prefix="codepilot-semantic-") as directory:
        temporary = Path(directory)
        repo = init_repo(temporary / "repo", files)
        workspace = Workspace(path=repo.path, source=normalize_source(str(repo.path)), base_commit=repo.head, repository_id="semantic_smoke")
        with create_session_factory(engine)() as session, httpx.Client(transport=httpx.MockTransport(handler)) as client:
            stores = Stores(session)
            Indexer().index(stores, workspace)
            index = RepositoryIndex(stores, workspace.repository_id)
            symbols = index.symbol_objects()
            api = OpenAICompatibleEmbeddingProvider(provider=ProviderSpec(name="mock", base_url="https://mock.invalid/v1", api_key_env="UNUSED"), model="synthetic-code-model", dimensions=2, revision="fixture-v1", api_key="mock-only", batch_size=7, client=client)
            provider = CachedEmbeddingProvider(api, temporary / "embeddings")
            first = retrieve_symbols(repo.path, symbols, issue, channel="semantic", embedding_provider=provider, limit=5)
            first_requests = list(requests)
            requests.clear()
            second = retrieve_symbols(repo.path, symbols, issue, channel="semantic", embedding_provider=provider, semantic_backend="faiss", limit=5)
            assert requests == []
            assert first == second
            assert first.symbols[0].qualified_name == target
            assert repo.git("status", "--porcelain") == ""
            (evidence / "semantic-numpy.json").write_text(first.model_dump_json(indent=2), encoding="utf-8")
            (evidence / "semantic-faiss.json").write_text(second.model_dump_json(indent=2), encoding="utf-8")
            summary = {"status": "PASS", "embedding": "MockTransport synthetic vectors; no real API", "fixture_commit": repo.head, "files": len(index.files()), "symbols": len(symbols), "first_request_batch_sizes": first_requests, "second_request_count": len(requests), "numpy_faiss_result_equal": first == second, "repository_clean": True}
            (evidence / "semantic-smoke.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            print(json.dumps(summary, indent=2))
            provider.close()
finally:
    engine.dispose()
