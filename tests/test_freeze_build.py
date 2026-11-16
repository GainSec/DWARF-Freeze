import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dwarf"))

from freeze import build, launch  # noqa: E402

AMARU = Path(__file__).resolve().parents[1] / "dwarf" / "testplease" / "amaru" / "v10.11.20260925"


def test_recipe_is_chosen_by_what_applies_and_instruments_a_copy(tmp_path):
    recipe = build.choose_recipe(AMARU)
    assert recipe.name == "v10.11.20260925"
    assert len(recipe.digest()) == 64 and recipe.digest() == build.choose_recipe(AMARU, "v10.11.20260925").digest()

    out = tmp_path / "src"
    build.instrument(AMARU, recipe, out)
    assert (out / "crates" / "amaru-freeze" / "src" / "agent.rs").is_file()
    runtime = (out / "crates" / "amaru-pure-stage" / "src" / "tokio.rs").read_text(encoding="utf-8")
    assert "crate::freeze::input(&stage_name, msg)" in runtime and "crate::freeze::action(name, input_seq, eff)" in runtime
    assert "amaru_freeze::init_from_env" in (out / "crates/amaru/src/bin/amaru/cmd/node/run.rs").read_text(encoding="utf-8")
    assert not (AMARU / "crates" / "amaru-freeze").exists(), "the original source is never touched"


def test_source_the_hooks_do_not_fit_is_refused(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    with pytest.raises(build.BuildError, match="need porting"):
        build.choose_recipe(tmp_path)
    with pytest.raises(build.BuildError, match="no recipe named"):
        build.choose_recipe(AMARU, "nope")
    with pytest.raises(build.BuildError, match="does not look like"):
        build.build(tmp_path / "missing", tmp_path)


def test_freeze_keeps_out_of_measurement_recipes():
    repo = Path(__file__).resolve().parents[1]
    targets = repo / "dwarf" / "targets"
    assert not list(targets.rglob("*freeze*")), "Freeze must not place files under dwarf/targets"
    for manifest in (repo / "dwarf" / "freeze" / "targets").rglob("manifest.json"):
        assert "measurement" not in manifest.read_text(encoding="utf-8")


def test_launch_names_and_missing_run(tmp_path):
    assert launch.container_name("testnet_42") == "dwarf-freeze-testnet-42"
    assert launch.current("preprod", tmp_path) is None
