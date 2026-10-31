"""Seeded random streams.

One root seed (``Config.seed``) feeds every random draw in the project through
numpy ``Generator`` objects. There is no ``random`` module, no
``np.random.seed`` and no global numpy state.

Each pipeline stage gets its own named stream derived from the root seed. That
way adding a draw in one stage cannot shift the numbers of another stage, which
is what keeps anomaly injection from changing the baseline data.

Streams are keyed by position in ``STREAMS`` through ``SeedSequence.spawn_key``,
not by Python's ``hash()`` (which is randomised per process). The order of
``STREAMS`` is therefore part of the reproducibility contract: append, never
reorder.

Note: numpy does not guarantee identical Generator output across major versions,
so requirements.txt pins numpy exactly.
"""
from __future__ import annotations

import numpy as np

STREAMS: tuple[str, ...] = ("master", "fx", "txn", "forecast", "anomaly")


def make_rng(seed: int, stream: str) -> np.random.Generator:
    """Return a fresh Generator for ``stream``, fully determined by ``seed``."""
    if stream not in STREAMS:
        raise ValueError(f"unknown stream {stream!r}; choose from {STREAMS}")
    if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed < 2**63:
        raise ValueError(f"seed must be an integer in [0, 2**63), got {seed!r}")
    sequence = np.random.SeedSequence(entropy=seed, spawn_key=(STREAMS.index(stream),))
    return np.random.Generator(np.random.PCG64(sequence))


class RngStreams:
    """The named streams for one run. Each stream is created once and then reused,
    so a stage draws from where the previous draw in that stream left off."""

    def __init__(self, seed: int):
        self.seed = seed
        self._streams: dict[str, np.random.Generator] = {}

    def get(self, stream: str) -> np.random.Generator:
        if stream not in self._streams:
            self._streams[stream] = make_rng(self.seed, stream)
        return self._streams[stream]

    def __getattr__(self, name: str) -> np.random.Generator:
        # rng.master, rng.fx, ... as shorthand for rng.get("master")
        if name in STREAMS:
            return self.get(name)
        raise AttributeError(name)
