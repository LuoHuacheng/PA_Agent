"""配置审计: 僵尸键与两 env 块键集一致性."""
from __future__ import annotations

from scripts.audit_config import audit


def test_extra_key_is_reported_as_zombie():
    res = audit({"tushare": {"token": ""}, "provider": {}})
    assert ("tushare", None) in res["extra"]


def test_unknown_sub_key_is_reported():
    res = audit({"monitoring": {"poll_interval_minutes": 60, "not_a_field": 1}})
    assert ("monitoring", "not_a_field") in res["extra"]


def test_env_parity_false_when_key_sets_differ():
    res = audit({
        "binance_usdm_live": {"enabled": True},
        "binance_usdm_testnet": {"enabled": True, "dry_run": False},
    })
    assert res["env_parity"] is False


def test_env_parity_true_when_only_values_differ():
    res = audit({
        "binance_usdm_live": {"enabled": True, "dry_run": True},
        "binance_usdm_testnet": {"enabled": True, "dry_run": False},
    })
    assert res["env_parity"] is True


def test_clean_config_reports_no_zombie():
    res = audit({"monitoring": {"poll_interval_minutes": 60}})
    assert res["extra"] == []
