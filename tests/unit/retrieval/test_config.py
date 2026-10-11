"""V-14: independent switches, preflight dependencies and frozen defaults."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from codepilot.retrieval.config import CHANNELS, RetrievalConfig
from codepilot.retrieval.service import retrieve_symbols
from support.retrieval import FakeEmbedding, FakeGraph, document


@pytest.fixture
def repository(tmp_path):
    (tmp_path / "a.py").write_text("def one():\n    return 1\n", encoding="utf-8")
    return tmp_path, [document("one", "one").symbol]


def test_default_configuration_keeps_single_channel_contract(repository):
    root, symbols = repository
    legacy = retrieve_symbols(root, symbols, "one")
    configured = retrieve_symbols(root, symbols, "one", config=RetrievalConfig())
    assert configured == legacy
    assert configured.enabled_channels == ["bm25"]
    assert configured.files == [] and configured.channel_weights == {}
    assert configured.fusion_method == "none"


@pytest.mark.parametrize("name", CHANNELS)
def test_each_channel_can_run_alone(repository, name):
    root, symbols = repository
    config = RetrievalConfig(**{channel: channel == name for channel in CHANNELS})
    result = retrieve_symbols(
        root,
        symbols,
        "one",
        config=config,
        embedding_provider=FakeEmbedding(),
        graph=FakeGraph(),
        seeds=[],
        churn={},
    )
    assert result.enabled_channels == [name]
    assert list(result.per_channel) == [name]


@pytest.mark.parametrize("disabled", CHANNELS)
def test_disabled_channels_are_not_invoked(repository, disabled, monkeypatch):
    root, symbols = repository
    called = []

    def search_text(name, *args):
        called.append(name)
        return []

    monkeypatch.setattr("codepilot.retrieval.service._query_candidates", search_text)
    for name, class_name in (
        ("structural", "StructuralChannel"),
        ("test", "TestProximityChannel"),
        ("history", "HistoryChannel"),
    ):

        def search(self, *args, label=name):
            called.append(label)
            return []

        monkeypatch.setattr(f"codepilot.retrieval.service.{class_name}.search", search)
    result = retrieve_symbols(
        root,
        symbols,
        "one",
        config=RetrievalConfig(**{c: c != disabled for c in CHANNELS}),
        embedding_provider=FakeEmbedding(),
        graph=FakeGraph(),
        seeds=[],
        churn={},
    )
    assert disabled not in called
    assert disabled not in result.per_channel
    assert called == [c for c in CHANNELS if c != disabled]


@pytest.mark.parametrize(
    "name, message",
    [
        ("semantic", "embedding_provider"),
        ("structural", "graph"),
        ("test", "graph"),
        ("history", "churn"),
    ],
)
def test_missing_dependencies_fail_before_reading_repository(name, message):
    config = RetrievalConfig(**{channel: channel == name for channel in CHANNELS})
    with pytest.raises(ValueError, match=message):
        retrieve_symbols(Path("missing"), [], "issue", config=config)


def test_configured_ripgrep_requires_executable_even_with_empty_input(monkeypatch):
    monkeypatch.setattr("codepilot.retrieval.service.shutil.which", lambda _: None)
    with pytest.raises(ValueError, match=r"ripgrep.*executable"):
        retrieve_symbols(Path("missing"), [], "", config=RetrievalConfig(bm25=False, ripgrep=True))


def test_disabled_seed_channel_is_not_implicitly_run(repository):
    root, symbols = repository
    with pytest.raises(ValueError, match=r"seed_channel.*enabled.*seeds"):
        retrieve_symbols(
            root,
            symbols,
            "one",
            config=RetrievalConfig(bm25=False, structural=True),
            graph=FakeGraph(),
        )
    result = retrieve_symbols(
        root, symbols, "one", config=RetrievalConfig(structural=True), graph=FakeGraph()
    )
    assert list(result.per_channel) == ["bm25", "structural"]


def test_all_disabled_is_empty_and_has_no_dependency_side_effects(repository):
    root, symbols = repository
    provider = FakeEmbedding()
    result = retrieve_symbols(
        root, symbols, "one", config=RetrievalConfig(bm25=False), embedding_provider=provider
    )
    assert result.per_channel == {} and result.enabled_channels == [] and result.symbols == []
    assert provider.calls == []


def test_configuration_validation_and_roundtrip():
    config = RetrievalConfig(semantic=True, history=True)
    assert RetrievalConfig.model_validate_json(config.model_dump_json()) == config
    with pytest.raises(ValidationError):
        RetrievalConfig(unknown=True)
    with pytest.raises(ValidationError):
        RetrievalConfig(bm25="false")
