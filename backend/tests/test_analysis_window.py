from dataclasses import replace

import pandas as pd
import pytest

from service.quant.configuration import QuantConfig, analysis_window


def test_analysis_uses_prototype_lookback_without_truncating_training_history():
    original = pd.DataFrame({"close": range(1298)}, index=pd.bdate_range("2021-08-03", periods=1298))
    selected = analysis_window(original, QuantConfig())
    pd.testing.assert_frame_equal(selected, original.iloc[-252:])
    assert len(original) == 1298
    selected.iloc[0, 0] = -1
    assert original.iloc[-252, 0] != -1
    short = original.tail(120)
    pd.testing.assert_frame_equal(analysis_window(short, QuantConfig()), short)
    with pytest.raises(ValueError, match="shorter"):
        analysis_window(original, replace(QuantConfig(), LOOKBACK_DAYS=60))
