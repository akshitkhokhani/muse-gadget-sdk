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

import json
import logging
import os
import sys
import time

import pytest

from musegadget import commands, config
from musegadget.commands import InvalidCommand
from musegadget.executor import COMMAND_SPECS, Account, Executor

SPEC = {
    "description": "Switch the pump.",
    "exec": ["/usr/local/bin/pump", "--quiet"],
    "required": {"on": {"type": "boolean", "description": "true for on."}},
    "optional": {"minutes": {"type": "integer", "description": "Run time."}},
    "timeout_ms": 10000,
}


def write(directory, name: str, data, mode: int = 0o644):
    path = directory / f"{name}.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    path.chmod(mode)
    return path


def script(tmp_path, body: str) -> list:
    """``exec`` for a Python script, so the tests need nothing but Python."""
    path = tmp_path / "cmd.py"
    path.write_text(f"import json, sys, time\n{body}\n")
    return [sys.executable, str(path)]


@pytest.fixture
def account(tmp_path):
    current = Account.current()
    return Account(current.name, current.uid, current.gid, str(tmp_path))


def drop_in(argv, **overrides):
    return commands.parse("pump.set", {**SPEC, "exec": argv, **overrides})


# -- Parsing ------------------------------------------------------------------

def test_parse_registers_the_spec_without_exec():
    command = commands.parse("pump.set", SPEC)
    assert command.argv == ("/usr/local/bin/pump", "--quiet")
    assert command.spec == {
        "description": "Switch the pump.",
        "required": SPEC["required"],
        "optional": SPEC["optional"],
        "timeout_ms": 10000 + commands.TIMEOUT_GRACE_MS,
    }


def test_parse_defaults():
    command = commands.parse("pump.set", {"description": "d", "exec": ["/bin/true"]})
    assert (command.required, command.optional) == ({}, {})
    assert command.timeout_ms == commands.DEFAULT_TIMEOUT_MS


@pytest.mark.parametrize("name", [
    "pump", "Pump.set", "pump..set", "pump.set.", "1pump.set", "pump-set.x", "a." + "b" * 64,
])
def test_parse_rejects_bad_names(name):
    with pytest.raises(InvalidCommand, match="command name"):
        commands.parse(name, SPEC)


@pytest.mark.parametrize("name", ["system.reboot", "file.delete", "device.health", "link.x"])
def test_parse_rejects_reserved_names(name):
    with pytest.raises(InvalidCommand, match="reserved"):
        commands.parse(name, SPEC)


def test_builtin_names_are_all_reserved():
    assert all(name.startswith(commands.RESERVED_PREFIXES) for name in COMMAND_SPECS)


@pytest.mark.parametrize("change, message", [
    ({"description": ""}, "description"),
    ({"description": None}, "description"),
    ({"exec": "/usr/local/bin/pump"}, "exec"),
    ({"exec": []}, "exec"),
    ({"exec": ["/bin/x", 3]}, "exec"),
    ({"exec": ["pump"]}, "absolute"),
    ({"required": []}, "required"),
    ({"required": {"on": {"type": "bool", "description": "d"}}}, "required.on"),
    ({"required": {"on": {"type": "boolean"}}}, "required.on"),
    ({"optional": {"x": {"type": "string", "default": 1}}}, "optional.x"),
    ({"optional": {"on": {"type": "boolean", "description": "d"}}}, "both required and optional"),
    ({"timeout_ms": 0}, "timeout_ms"),
    ({"timeout_ms": 600_001}, "timeout_ms"),
    ({"timeout_ms": "10"}, "timeout_ms"),
    ({"timeout_ms": True}, "timeout_ms"),
    ({"timeout": 10}, "unknown keys: timeout"),
])
def test_parse_rejects_bad_specs(change, message):
    with pytest.raises(InvalidCommand, match=message):
        commands.parse("pump.set", {**SPEC, **change})


def test_parse_rejects_a_non_object():
    with pytest.raises(InvalidCommand, match="JSON object"):
        commands.parse("pump.set", ["not", "an", "object"])


# -- Parameters -----------------------------------------------------------------

def test_check_params_keeps_only_declared_parameters():
    command = commands.parse("pump.set", SPEC)
    assert command.check_params({"on": True, "minutes": 5, "extra": 1}) == {"on": True, "minutes": 5}


@pytest.mark.parametrize("params, message", [
    ({}, "on is required"),
    ({"on": "yes"}, "on must be a boolean"),
    ({"on": True, "minutes": 1.5}, "minutes must be an integer"),
    ({"on": True, "minutes": False}, "minutes must be an integer"),
])
def test_check_params_rejects(params, message):
    with pytest.raises(InvalidCommand, match=message):
        commands.parse("pump.set", SPEC).check_params(params)


def test_number_accepts_integers_but_not_booleans():
    command = commands.parse("x.y", {"description": "d", "exec": ["/bin/true"],
                                     "required": {"n": {"type": "number", "description": "n"}}})
    assert command.check_params({"n": 2}) == {"n": 2}
    assert command.check_params({"n": 2.5}) == {"n": 2.5}
    with pytest.raises(InvalidCommand):
        command.check_params({"n": True})


# -- Loading the directory --------------------------------------------------------

def test_load_a_missing_directory(tmp_path):
    assert commands.load(tmp_path / "nope") == []


def test_load_skips_bad_files_and_keeps_good_ones(tmp_path, caplog):
    write(tmp_path, "pump.set", SPEC)
    write(tmp_path, "aaa.broken", "{not json")
    write(tmp_path, "system.reboot", SPEC)
    write(tmp_path, "lamp.set", {**SPEC, "exec": ["lamp"]})
    (tmp_path / "notes.txt").write_text("ignored")
    with caplog.at_level(logging.WARNING):
        loaded = commands.load(tmp_path)
    assert [c.name for c in loaded] == ["pump.set"]
    warnings = "\n".join(r.getMessage() for r in caplog.records)
    assert "aaa.broken.json: not valid JSON" in warnings
    assert "system.reboot.json: names starting with" in warnings
    assert "lamp.set.json: exec must start with" in warnings


def test_load_skips_deeply_nested_json(tmp_path, caplog):
    write(tmp_path, "pump.set", "[" * 60000)
    assert commands.load(tmp_path) == []
    assert "pump.set.json: not valid JSON" in caplog.text


def test_load_skips_symlinks_and_special_files(tmp_path, caplog):
    target = write(tmp_path, "real.target", SPEC)
    (tmp_path / "pump.set.json").symlink_to(target)
    os.mkfifo(tmp_path / "lamp.set.json")
    names = [c.name for c in commands.load(tmp_path)]
    assert names == ["real.target"]
    assert "pump.set.json: can't read it" in caplog.text
    assert "lamp.set.json: not a regular file" in caplog.text


def test_load_skips_oversize_files(tmp_path, caplog):
    write(tmp_path, "pump.set", {**SPEC, "description": "x" * commands.MAX_FILE_BYTES})
    assert commands.load(tmp_path) == []
    assert "larger than" in caplog.text


def test_load_skips_files_root_does_not_own(tmp_path, monkeypatch, caplog):
    # The service runs as root on a device; the files must be root's alone.
    write(tmp_path, "pump.set", SPEC)
    if os.geteuid() == 0:
        os.chown(tmp_path / "pump.set.json", 12345, 12345)
    monkeypatch.setattr(commands.os, "geteuid", lambda: 0)
    assert commands.load(tmp_path) == []
    assert "owned by root" in caplog.text


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to own the files")
def test_load_as_root_checks_the_mode(tmp_path, caplog):
    write(tmp_path, "pump.set", SPEC)
    write(tmp_path, "lamp.set", SPEC, mode=0o664)
    assert [c.name for c in commands.load(tmp_path)] == ["pump.set"]
    assert "lamp.set.json: must be owned by root" in caplog.text


def test_commands_dir(monkeypatch, tmp_path):
    monkeypatch.delenv(config.COMMANDS_DIR_ENV, raising=False)
    assert str(config.commands_dir()) == "/etc/musegadget/commands.d"
    monkeypatch.setenv(config.COMMANDS_DIR_ENV, str(tmp_path))
    assert config.commands_dir() == tmp_path


# -- Running them -------------------------------------------------------------------

def test_specs_add_drop_ins_after_the_builtins(account):
    executor = Executor(account, [commands.parse("pump.set", SPEC)])
    specs = executor.specs()
    assert list(specs) == [*COMMAND_SPECS, "pump.set"]
    assert "exec" not in specs["pump.set"]


def test_specs_are_unchanged_without_drop_ins(account):
    assert Executor(account).specs() == COMMAND_SPECS


def test_drop_in_gets_its_params_on_stdin_and_returns_json(account, tmp_path):
    argv = script(tmp_path, (
        "params = json.load(sys.stdin)\n"
        "print(json.dumps({'got': params, 'argv': sys.argv[1:]}))"
    )) + ["--flag"]
    result = Executor(account, [drop_in(argv)]).run("pump.set", {"on": True, "junk": 1})
    assert result == {"ok": True, "payload": {"got": {"on": True}, "argv": ["--flag"]}}


def test_drop_in_runs_in_the_account_home_with_the_safe_environment(account, tmp_path, monkeypatch):
    argv = script(tmp_path, (
        "import os\n"
        "print(json.dumps({'cwd': os.getcwd(), 'home': os.environ['HOME'],"
        " 'secret': os.environ.get('MUSE_TEST_SECRET')}))"
    ))
    monkeypatch.setenv("MUSE_TEST_SECRET", "leak")
    payload = Executor(account, [drop_in(argv)]).run("pump.set", {"on": False})["payload"]
    assert os.path.realpath(payload["cwd"]) == os.path.realpath(tmp_path)
    assert payload["home"] == str(tmp_path)
    assert payload["secret"] is None


def test_drop_in_plain_text_output(account, tmp_path):
    argv = script(tmp_path, "print('pump on')")
    result = Executor(account, [drop_in(argv)]).run("pump.set", {"on": True})
    assert result == {"ok": True, "payload": {"output": "pump on\n"}}


def test_drop_in_json_that_is_not_an_object_is_text(account, tmp_path):
    argv = script(tmp_path, "print('[1, 2]')")
    result = Executor(account, [drop_in(argv)]).run("pump.set", {"on": True})
    assert result["payload"] == {"output": "[1, 2]\n"}


def test_drop_in_large_output_is_truncated(account, tmp_path):
    argv = script(tmp_path, "sys.stdout.write('x' * 200000)")
    payload = Executor(account, [drop_in(argv)]).run("pump.set", {"on": True})["payload"]
    assert payload["truncated"] is True
    assert len(payload["output"]) == 96 * 1024


def test_drop_in_failure_reports_the_last_stderr_line(account, tmp_path):
    argv = script(tmp_path, (
        "print('first', file=sys.stderr)\n"
        "print('relay busy', file=sys.stderr)\n"
        "sys.exit(3)"
    ))
    result = Executor(account, [drop_in(argv)]).run("pump.set", {"on": True})
    assert result == {"ok": False, "error": "pump.set exited with 3: relay busy"}


def test_drop_in_failure_without_stderr(account, tmp_path):
    argv = script(tmp_path, "sys.exit(2)")
    result = Executor(account, [drop_in(argv)]).run("pump.set", {"on": True})
    assert result == {"ok": False, "error": "pump.set exited with 2"}


def test_drop_in_bad_params_never_start_the_program(account, tmp_path):
    marker = tmp_path / "ran"
    argv = script(tmp_path, f"open({str(marker)!r}, 'w')")
    result = Executor(account, [drop_in(argv)]).run("pump.set", {"on": "yes"})
    assert result == {"ok": False, "error": "on must be a boolean"}
    assert not marker.exists()


def test_drop_in_times_out_and_kills_the_process_group(account, tmp_path):
    argv = script(tmp_path, (
        "import subprocess\n"
        "subprocess.Popen(['sleep', '30'])\n"
        "time.sleep(30)"
    ))
    started = time.monotonic()
    result = Executor(account, [drop_in(argv, timeout_ms=300)]).run("pump.set", {"on": True})
    assert result == {"ok": False, "error": "pump.set timed out after 0.3s"}
    assert time.monotonic() - started < 5


def test_drop_in_that_cannot_start(account, tmp_path):
    missing = tmp_path / "missing"
    result = Executor(account, [drop_in([str(missing)])]).run("pump.set", {"on": True})
    assert not result["ok"]
    assert result["error"].startswith(f"could not start {missing}:")


def test_drop_in_cannot_replace_a_builtin(account):
    # parse() reserves the names; Executor also refuses them, whatever their source.
    fake = commands.DropInCommand("system.run", "x", ("/bin/false",), {}, {}, 1000)
    executor = Executor(account, [fake])
    assert executor.drop_ins == {}
    assert executor.specs()["system.run"] == COMMAND_SPECS["system.run"]


def test_unknown_commands_are_still_unsupported(account):
    result = Executor(account).run("pump.set", {})
    assert result == {"ok": False, "error": "unsupported command: pump.set"}
