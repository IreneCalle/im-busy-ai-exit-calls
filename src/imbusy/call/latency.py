"""Turn latency measurement: the README number.

Server-side we can measure from "final transcript received" (`prompt`, last=true) to "first text
token sent". That excludes Twilio's end-of-speech detection and TTS start, which the server cannot
see. The browser demo measures the full user-perceived gap (speech end to `speechSynthesis` start)
and reports it back over the socket, so the README can show both and say which is which.
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum


class LatencyKind(StrEnum):
    SERVER_FIRST_TOKEN = "server_first_token"  # prompt received -> first token sent
    CLIENT_FIRST_AUDIO = "client_first_audio"  # demo only: speech end -> synthesis start


@dataclass(frozen=True)
class LatencySample:
    call_id: str
    turn_index: int
    kind: LatencyKind
    milliseconds: float


@dataclass
class LatencyRecorder:
    """Collects samples for one session; flushed to the call record at the end."""

    call_id: str
    samples: list[LatencySample] = field(default_factory=list)
    _pending: dict[int, float] = field(default_factory=dict)

    def prompt_received(self, turn_index: int) -> None:
        self._pending[turn_index] = time.perf_counter()

    def first_token_sent(self, turn_index: int) -> None:
        started = self._pending.pop(turn_index, None)
        if started is None:
            return
        ms = (time.perf_counter() - started) * 1000
        self.samples.append(
            LatencySample(self.call_id, turn_index, LatencyKind.SERVER_FIRST_TOKEN, ms)
        )

    def client_reported(self, turn_index: int, milliseconds: float) -> None:
        self.samples.append(
            LatencySample(self.call_id, turn_index, LatencyKind.CLIENT_FIRST_AUDIO, milliseconds)
        )


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile, q in (0, 100]. Raises on empty input."""
    if not values:
        raise ValueError("no values")
    if not 0 < q <= 100:
        raise ValueError("q must be in (0, 100]")
    ordered = sorted(values)
    rank = math.ceil(q / 100 * len(ordered))
    return ordered[rank - 1]
