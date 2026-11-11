from typing import Final
from unittest.mock import MagicMock

import pytest


class _CallCapture:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}


@pytest.mark.asyncio
async def test_hamsa_realtime_dispatches_to_its_pcm_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    from litellm.realtime_api import main as realtime_main

    captured: Final = _CallCapture()

    async def hamsa_realtime(**kwargs: object) -> None:
        captured.values = kwargs

    monkeypatch.setattr("litellm.llms.hamsa.realtime.handler.hamsa_realtime", hamsa_realtime)

    await realtime_main._arealtime.__wrapped__(
        model="hamsa/hamsa-stt",
        api_base="http://hamsa.test",
        websocket=MagicMock(),
        litellm_logging_obj=MagicMock(),
        api_key="configured-upstream",
    )

    assert captured.values["model"] == "hamsa-stt"
    assert captured.values["api_base"] == "http://hamsa.test"
    assert captured.values["api_key"] == "configured-upstream"
