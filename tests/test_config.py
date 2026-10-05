from exporter.config import MappingSource, Settings


def test_defaults_match_decisions():
    s = Settings(_env_file=None)
    assert s.bill_cache_hits is True
    assert s.mapping_order == [MappingSource.LABEL, MappingSource.USER_PATH]
    assert s.lag_alert_seconds == 15 * 60
    assert s.lago_batch_size == 100


def test_comma_separated_env_vars(monkeypatch):
    monkeypatch.setenv("EXPORTER_BILLABLE_PROVIDERS", "ollama-qai, vllm-main")
    monkeypatch.setenv("EXPORTER_MAPPING_ORDER", "user_path,label")
    s = Settings(_env_file=None)
    assert s.billable_providers == ["ollama-qai", "vllm-main"]
    assert s.mapping_order == [MappingSource.USER_PATH, MappingSource.LABEL]