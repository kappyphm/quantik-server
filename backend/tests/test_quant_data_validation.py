from copy import deepcopy

from service.quant_data_validation import history_accepted, history_header_accepted, universe_accepted
from service.screening_validation import certify_history, certify_universe
from test_adjustment_validation import inputs


def test_provider_contract_acceptance_keeps_unverified_adjustment_warning():
    history, _ = inputs()
    payload = certify_history(history).payload
    assert history_accepted(payload)
    assert history_header_accepted(payload["metadata"])
    assert payload["metadata"]["provenance"] == "LIVE_UNVERIFIED"
    assert payload["metadata"]["adjustment"] == "PROVIDER_ADJUSTMENT_UNVERIFIED"
    assert "CORPORATE_ACTION_ADJUSTMENT_UNVERIFIED" in payload["metadata"]["warnings"]
    for field, value in (("provider_version", "5.0.2"), ("input_mode", "PROVISIONAL_DAILY"),
                         ("adjustment", "RAW"), ("source", "unknown")):
        changed = deepcopy(payload); changed["metadata"][field] = value
        assert not history_accepted(changed)
    changed = deepcopy(payload); changed["bars"][0]["close"] += 1
    assert not history_accepted(changed)
    assert not history_accepted(history.payload)  # No content-bound contract evidence.


def test_universe_requires_content_bound_contract_and_fixture_is_rejected():
    payload = {"provenance": "LIVE_UNVERIFIED", "members": {
        "FPT": {"exchange": "HOSE", "sector": "9500"}}}
    assert not universe_accepted(payload)
    accepted = certify_universe(payload)
    assert universe_accepted(accepted)
    accepted["members"]["FPT"]["exchange"] = "HNX"
    assert not universe_accepted(accepted)
    history, _ = inputs()
    history.payload["metadata"]["provenance"] = "FIXTURE"
    assert not history_accepted(history.payload)
