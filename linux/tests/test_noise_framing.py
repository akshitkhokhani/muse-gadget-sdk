# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from types import SimpleNamespace

from musegadget.noise import framing


def frame(chunk_id, index, total=2):
    return framing.encode_noise_frame(framing.NoiseTransportFrame(
        chunk_id=chunk_id, chunk_index=index, total_chunks=total, payload=b"x",
    ))


def test_wall_clock_forward_jump_does_not_discard_incomplete_message(monkeypatch):
    clock = {"wall": 1000.0, "elapsed": 10.0}
    monkeypatch.setattr(framing, "time", SimpleNamespace(
        time=lambda: clock["wall"], monotonic=lambda: clock["elapsed"],
    ))
    decoder = framing.NoiseFrameDecoder()
    assert decoder.decode(frame(1, 0)) is None
    clock.update(wall=2000.0, elapsed=11.0)
    assert decoder.decode(frame(1, 1)) == b"xx"


def test_wall_clock_backward_jump_does_not_retain_expired_assemblies(monkeypatch):
    clock = {"wall": 1000.0, "elapsed": 10.0}
    monkeypatch.setattr(framing, "time", SimpleNamespace(
        time=lambda: clock["wall"], monotonic=lambda: clock["elapsed"],
    ))
    decoder = framing.NoiseFrameDecoder()
    for chunk_id in range(framing.MAX_PENDING_ASSEMBLIES):
        assert decoder.decode(frame(chunk_id, 0)) is None
    clock.update(wall=500.0, elapsed=10.0 + framing.ASSEMBLY_TTL_SECONDS + 1)
    # An expired table must make room without poisoning a healthy decoder.
    assert decoder.decode(frame(100, 0)) is None
    assert decoder.decode(frame(100, 1)) == b"xx"
