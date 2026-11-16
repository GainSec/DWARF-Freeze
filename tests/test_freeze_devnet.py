import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dwarf"))

from freeze import devnet  # noqa: E402


def test_override_only_adds_to_the_first_amaru_relay():
    document = devnet.override("dwarf/freeze-amaru:x-static", "secret", port=7400, frozen=True)
    assert set(document["services"]) == {"freeze-target-extract", "amaru-relay-1"}
    relay = document["services"]["amaru-relay-1"]
    assert relay["environment"] == {
        "AMARU_PEER_REMOVAL_COOLDOWN_SECS": "5", "AMARU_BIN": "/target/amaru", "AMARU_MIGRATE_CHAIN_DB": "true", "DWARF_FREEZE_LISTEN": "0.0.0.0:7373",
        "DWARF_FREEZE_TOKEN": "secret", "DWARF_FREEZE_START": "frozen",
        "DWARF_FREEZE_RESIGN": "1", "DWARF_FREEZE_DEVNET_CONFIG": "/freeze-producers",
    }
    assert relay["volumes"] == [
        "freeze-target-bin:/target:ro",
        "p1-configs:/freeze-producers/p1:ro",
        "p2-configs:/freeze-producers/p2:ro",
        "p3-configs:/freeze-producers/p3:ro",
    ]
    assert relay["ports"] == ["127.0.0.1:7400:7373"], "the agent is published on localhost only"
    assert "image" not in relay and "entrypoint" not in relay, "the relay keeps DWARF's wrapper and bootstrap"
    extract = document["services"]["freeze-target-extract"]
    assert extract["image"] == "dwarf/freeze-amaru:x-static" and extract["platform"] == "linux/amd64"
    assert "$" not in extract["command"][0], "nothing for compose to interpolate"
    assert "DWARF_FREEZE_START" not in devnet.override("i", "t")["services"]["amaru-relay-1"]["environment"]
    off = devnet.override("i", "t", resign=False)["services"]["amaru-relay-1"]["environment"]
    assert off["DWARF_FREEZE_RESIGN"] == "0" and off["DWARF_FREEZE_DEVNET_CONFIG"] == "/freeze-producers"


def test_baseline_is_dwarfs_own_file_and_has_the_services_the_override_names():
    assert devnet.BASELINE.is_file()
    text = devnet.BASELINE.read_text(encoding="utf-8")
    assert "\n  amaru-relay-1:" in text and "AMARU_NETWORK: testnet_42" in text
    assert "freeze" not in text.lower(), "the baseline is not modified by Freeze"


def test_nothing_running_means_no_connection_details(tmp_path):
    assert devnet.current(tmp_path) is None
    devnet.down(tmp_path)  # a no-op when Freeze never started a devnet


def test_shipped_profiles_point_at_dwarf_layouts_with_a_replaceable_node(tmp_path):
    shipped = {profile.name: profile for profile in devnet.profiles(tmp_path)}
    assert {"relay", "relay-2", "kes-security", "miniprotocol-security"} <= set(shipped)
    for profile in shipped.values():
        assert devnet.find_profile(profile.name, tmp_path).target in devnet.targets(profile.files[0])
    assert devnet.find_profile(None, tmp_path).name == "relay"
    layouts = dict(devnet.topologies())
    assert layouts[devnet.BASELINE] == ["amaru-relay-1", "amaru-relay-2"]


def test_a_profile_chooses_the_node_and_its_settings(tmp_path):
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "mine.yaml").write_text(
        "name: mine\ncompose: antithesis/cardano_amaru_relay_bootstrap_control/docker-compose.yaml\n"
        "target: amaru-relay-2\npeer_cooldown: 30\nresign: false\nenvironment: {AMARU_LOG: trace}\n", encoding="utf-8")
    mine = devnet.find_profile("mine", tmp_path)
    assert (mine.origin, mine.peer_cooldown) == ("yours", 30)
    document = devnet.override("image", "secret", profile=mine)
    assert set(document["services"]) == {"freeze-target-extract", "amaru-relay-2"}
    assert document["services"]["amaru-relay-2"]["environment"]["AMARU_LOG"] == "trace"
    assert document["services"]["amaru-relay-2"]["environment"]["DWARF_FREEZE_RESIGN"] == "0"

    assert devnet.find_profile(None, tmp_path, compose=str(devnet.BASELINE), target="amaru-relay-2").target == "amaru-relay-2"
    for wrong in (dict(name="nope"), dict(name="mine", target="p1"), dict(name=None, compose=str(tmp_path / "x.yaml"))):
        try:
            devnet.find_profile(wrong.get("name"), tmp_path, wrong.get("compose"), wrong.get("target"))
        except devnet.DevnetError as error:
            assert str(error)
        else:
            raise AssertionError(f"{wrong} should have been refused")
    (tmp_path / "profiles" / "bad.yaml").write_text("compose: x.yaml\ntarget: a\ncolour: blue\n", encoding="utf-8")
    try:
        devnet.profiles(tmp_path)
    except devnet.DevnetError as error:
        assert "colour" in str(error)
    else:
        raise AssertionError("an unknown setting should be refused")


@pytest.mark.parametrize("name", ["", ".hidden", "../escape", "two words", "semi;colon", "$(touch pwned)", "tick`id`", "quote'bad"])
@pytest.mark.parametrize("operation", [devnet.snapshot_save, devnet.snapshot_restore, devnet.snapshot_delete])
def test_snapshot_names_reject_nonportable_and_shell_metacharacters(tmp_path, name, operation):
    with pytest.raises(devnet.DevnetError, match="simple name"):
        operation(tmp_path, name)


def _restart_fixture(tmp_path, monkeypatch, *, resign=False):
    profile = devnet.Profile(name="relay", compose=[str(devnet.BASELINE)], resign=True)
    home = devnet.directory(tmp_path)
    home.mkdir(parents=True)
    token_file = home / "token"
    token_file.write_text("secret", encoding="utf-8")
    info = {
        "host": "127.0.0.1", "port": 7373, "token": "secret", "token_file": str(token_file),
        "image": "freeze:test", "cooldown": 5, "profile": "relay", "target": profile.target,
        "network": "testnet_42", "resign": resign,
    }
    (home / "run.json").write_text(json.dumps({key: value for key, value in info.items() if key != "token"}), encoding="utf-8")
    monkeypatch.setattr(devnet, "active", lambda _root: profile)
    monkeypatch.setattr(devnet, "_compose", lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(devnet, "wait_ready", lambda _root, **_kwargs: devnet.current(tmp_path))
    return home, info


def test_restart_preserves_the_persisted_effective_no_resign_setting(tmp_path, monkeypatch):
    home, _ = _restart_fixture(tmp_path, monkeypatch, resign=False)

    restarted = devnet.restart_target(tmp_path)

    document = yaml.safe_load((home / "override.yml").read_text(encoding="utf-8"))
    environment = document["services"]["amaru-relay-1"]["environment"]
    assert environment["DWARF_FREEZE_RESIGN"] == "0"
    assert restarted["resign"] is False


def test_snapshot_restore_preserves_no_resign_through_its_restart(tmp_path, monkeypatch):
    home, _ = _restart_fixture(tmp_path, monkeypatch, resign=False)
    snapshot = home / "snapshots" / "before-ingest"
    snapshot.mkdir(parents=True)
    (snapshot.parent / "before-ingest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(devnet, "state_volume", lambda _root, **_more: ("state-volume", "copy-image"))
    monkeypatch.setattr(devnet, "_copy", lambda *_args, **_kwargs: "")

    restored = devnet.snapshot_restore(tmp_path, "before-ingest")

    document = yaml.safe_load((home / "override.yml").read_text(encoding="utf-8"))
    environment = document["services"]["amaru-relay-1"]["environment"]
    assert environment["DWARF_FREEZE_RESIGN"] == "0"
    assert restored["resign"] is False
