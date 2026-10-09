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

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from musegadget import config

TOKEN = "mgst_" + "A" * 42 + "w"


def test_sdk_token_is_absent_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv(config.SDK_TOKEN_ENV, raising=False)
    assert config.sdk_token(tmp_path) is None


def test_sdk_token_reads_the_state_file(tmp_path, monkeypatch):
    monkeypatch.delenv(config.SDK_TOKEN_ENV, raising=False)
    (tmp_path / config.SDK_TOKEN_FILE).write_text(TOKEN + "\n")
    assert config.sdk_token(tmp_path) == TOKEN


def test_sdk_token_environment_overrides_the_file(tmp_path, monkeypatch):
    (tmp_path / config.SDK_TOKEN_FILE).write_text("mgst_" + "B" * 43)
    monkeypatch.setenv(config.SDK_TOKEN_ENV, TOKEN)
    assert config.sdk_token(tmp_path) == TOKEN


@pytest.mark.parametrize("bad", ["mgst_short", "mgst_" + "A" * 43 + "x", "mgst_" + "A" * 42 + "B"])
def test_sdk_token_rejects_tokens_gadgets_could_not_issue(tmp_path, monkeypatch, bad):
    monkeypatch.setenv(config.SDK_TOKEN_ENV, bad)
    with pytest.raises(ValueError):
        config.sdk_token(tmp_path)


def test_load_json_treats_undecodable_bytes_as_missing(tmp_path):
    (tmp_path / "pairing.json").write_bytes(bytes.fromhex("fffe") + b'{"mac": "x"}')
    assert config.load_json("pairing.json", tmp_path) is None


@pytest.mark.skipif(
    os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
    reason="needs POSIX file modes and a user the mode applies to",
)
def test_load_json_does_not_mistake_permission_denied_for_missing(tmp_path):
    path = tmp_path / "identity.json"
    path.write_text('{"mac": "x"}')
    path.chmod(0)
    try:
        with pytest.raises(PermissionError):
            config.load_json("identity.json", tmp_path)
    finally:
        path.chmod(0o600)


def test_concurrent_state_saves_publish_complete_independent_files(tmp_path, monkeypatch):
    # Pairing and service token rotation both save pairing.json. Hold their
    # real renames until both writers have finished their temporary files.
    barrier = threading.Barrier(2, timeout=5)
    replace = config.os.replace
    def publish(source, destination):
        barrier.wait()
        return replace(source, destination)
    monkeypatch.setattr(config.os, "replace", publish)
    records = [{"fixture": "a" * 1000}, {"fixture": "b" * 2000}]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(config.save_json, config.PAIRING_FILE, value, tmp_path)
                   for value in records]
        for future in futures:
            future.result(timeout=10)
    assert config.load_json(config.PAIRING_FILE, tmp_path) in records
    assert list(tmp_path.iterdir()) == [tmp_path / config.PAIRING_FILE]


def test_failed_state_serialization_keeps_old_file_and_removes_temp(tmp_path):
    config.save_json(config.PAIRING_FILE, {"fixture": "old"}, tmp_path)
    with pytest.raises(TypeError):
        config.save_json(config.PAIRING_FILE, {"invalid": object()}, tmp_path)
    assert config.load_json(config.PAIRING_FILE, tmp_path) == {"fixture": "old"}
    assert list(tmp_path.iterdir()) == [tmp_path / config.PAIRING_FILE]
