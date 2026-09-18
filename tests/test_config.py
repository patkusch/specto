"""`specto.toml`: project settings, their precedence, their validation, `specto init` and the doctor line."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from specto import cli, config
from specto.cli import main

# What each flag was before the config file existed: nothing may drift.
OLD_RUN_DEFAULTS = {
    "provider": "claude", "model": "claude-opus-5", "reader_model": None, "effort": "high", "max_cost": None,
    "crop": None, "detect": "hash", "hash_distance": 8, "sample_fps": 1.0, "max_frames": 240,
    "frames_per_call": 8, "ocr": True, "out": None, "whisper_model": "base",
}


@pytest.fixture
def project(tmp_path: Path, monkeypatch):
    """An empty project folder that is the current folder, so ./specto.toml is looked up there."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _capture(monkeypatch, handler: str) -> dict:
    """Swap a command's handler for one that records the options it was given."""
    seen: dict = {}
    monkeypatch.setattr(cli, handler, lambda args: seen.update({k: v for k, v in vars(args).items() if k != "func"}) or 0)
    return seen


def _run(monkeypatch, *argv: str) -> dict:
    seen = _capture(monkeypatch, "cmd_run")
    assert main(["run", "walkthrough.mp4", *argv]) == 0
    return seen


def test_built_in_defaults_are_unchanged_without_a_file(project: Path, monkeypatch) -> None:
    seen = _run(monkeypatch)
    for key, value in OLD_RUN_DEFAULTS.items():
        assert seen[key] == value, key
    assert {s.name: s.default for s in config.SETTINGS if s.table == "run"} == OLD_RUN_DEFAULTS


def test_config_beats_default(project: Path, monkeypatch, capsys) -> None:
    (project / "specto.toml").write_text(
        '[run]\nprovider = "gemini"\nmodel = "gemini-2.5-pro"\nmax_cost = 5\nhash_distance = 4\nocr = false\n'
        'crop = "auto"\nsample_fps = 2\nout = "team-out"\n', encoding="utf-8")
    seen = _run(monkeypatch)
    assert seen["provider"] == "gemini" and seen["model"] == "gemini-2.5-pro"
    assert seen["max_cost"] == 5.0 and isinstance(seen["max_cost"], float)
    assert seen["hash_distance"] == 4 and seen["ocr"] is False and seen["crop"] == "auto"
    assert seen["sample_fps"] == 2.0 and seen["out"] == "team-out"
    assert seen["effort"] == "high"  # not in the file: the built-in default
    assert "config: specto.toml sets" in capsys.readouterr().out


def test_flag_beats_config(project: Path, monkeypatch) -> None:
    (project / "specto.toml").write_text('[run]\nmodel = "from-file"\nhash_distance = 4\nocr = false\n', encoding="utf-8")
    seen = _run(monkeypatch, "--model", "from-flag", "--hash-distance", "12", "--ocr")
    assert seen["model"] == "from-flag" and seen["hash_distance"] == 12 and seen["ocr"] is True


def test_flag_typed_with_its_default_value_still_beats_config(project: Path, monkeypatch) -> None:
    (project / "specto.toml").write_text(
        '[run]\nmodel = "from-file"\neffort = "low"\nprovider = "gemini"\nhash_distance = 4\nmax_frames = 10\n'
        'sample_fps = 3\ndetect = "scene"\nocr = false\nwhisper_model = "small"\nframes_per_call = 2\n', encoding="utf-8")
    seen = _run(monkeypatch, "--model", "claude-opus-5", "--effort", "high", "--provider", "claude",
                "--hash-distance", "8", "--max-frames", "240", "--sample-fps", "1", "--detect", "hash",
                "--ocr", "--whisper-model", "base", "--frames-per-call", "8")
    for key in ("model", "effort", "provider", "hash_distance", "max_frames", "sample_fps", "detect", "ocr",
                "whisper_model", "frames_per_call"):
        assert seen[key] == OLD_RUN_DEFAULTS[key], key


def test_no_ocr_flag_beats_config_that_turns_it_on(project: Path, monkeypatch) -> None:
    (project / "specto.toml").write_text("[run]\nocr = true\n", encoding="utf-8")
    assert _run(monkeypatch, "--no-ocr")["ocr"] is False


def test_config_flag_picks_the_file_and_skips_the_local_one(project: Path, monkeypatch) -> None:
    (project / "specto.toml").write_text('[run]\nmodel = "local"\n', encoding="utf-8")
    other = project / "team.toml"
    other.write_text('[run]\nmodel = "team"\n', encoding="utf-8")
    assert _run(monkeypatch, "--config", str(other))["model"] == "team"


def test_missing_config_flag_target_is_an_error(project: Path, capsys) -> None:
    assert main(["run", "walkthrough.mp4", "--config", "nope.toml"]) == 2
    assert "nope.toml: no such config file" in capsys.readouterr().err


def test_commands_take_only_the_settings_they_have(project: Path, monkeypatch) -> None:
    (project / "specto.toml").write_text(
        '[run]\nmodel = "m"\nprovider = "gemini"\neffort = "low"\nwhisper_model = "small"\nout = "o"\nmax_cost = 1\n'
        "[watch]\ninterval = 30\n", encoding="utf-8")
    seen = _capture(monkeypatch, "cmd_resolve")
    assert main(["resolve", "somewhere"]) == 0
    assert (seen["model"], seen["provider"], seen["effort"]) == ("m", "gemini", "low")
    assert "out" not in seen

    seen = _capture(monkeypatch, "cmd_demo")
    assert main(["demo"]) == 0
    assert seen["out"] == "o"

    seen = _capture(monkeypatch, "cmd_live")
    assert main(["live"]) == 0  # --out comes from the file
    assert seen["out"] == "o" and seen["whisper_model"] == "small" and seen["model"] == "m"
    assert seen["interval"] == 3.0  # live's own screenshot interval is not the watch interval

    seen = _capture(monkeypatch, "cmd_watch")
    assert main(["watch", "folder", "--model", "flag"]) == 0
    assert seen["model"] == "flag" and seen["interval"] == 30.0 and seen["max_cost"] == 1.0
    assert seen["effort"] == "low" and seen["whisper_model"] == "small"  # no watch flag, still honoured


def test_live_without_out_anywhere_still_fails(project: Path, monkeypatch, capsys) -> None:
    _capture(monkeypatch, "cmd_live")
    with pytest.raises(SystemExit) as stop:
        main(["live"])
    assert stop.value.code == 2
    assert "--out" in capsys.readouterr().err


def test_watch_item_options_come_from_the_settings(project: Path) -> None:
    args = cli.build_parser().parse_args(["watch", "folder", "--fake"])
    config.apply_config(args, config.parse_config(Path("specto.toml"),
                                                  '[run]\neffort = "low"\nreader_model = "cheap"\nmax_frames = 50\n'))
    item = cli._watch_item_args(args)
    assert (item.effort, item.reader_model, item.max_frames, item.fake) == ("low", "cheap", 50, True)
    assert (item.frames_per_call, item.detect, item.ocr, item.scene_threshold) == (8, "hash", True, 0.3)


# ------------------------------------------------------------ validation


def _load(project: Path, text: str) -> config.Config:
    (project / "specto.toml").write_text(text, encoding="utf-8")
    return config.parse_config(Path("specto.toml"), text)


def _error(project: Path, text: str) -> str:
    with pytest.raises(config.ConfigError) as error:
        _load(project, text)
    message = str(error.value)
    assert "\n" not in message
    return message


def test_unknown_key_names_the_file_the_key_and_the_allowed_keys(project: Path) -> None:
    message = _error(project, '[run]\nmodle = "x"\n')
    assert message.startswith("specto.toml:") and '"modle"' in message and "[run]" in message
    assert "allowed keys: provider, model, reader_model, effort, max_cost" in message
    assert 'did you mean "model"' in message


def test_unknown_key_in_the_watch_table_and_stray_tables(project: Path) -> None:
    assert "allowed keys: interval" in _error(project, "[watch]\nspeed = 3\n")
    assert "unknown table [extra]" in _error(project, "[extra]\na = 1\n")
    assert "put settings under [run] or [watch]" in _error(project, 'model = "x"\n')


def test_wrong_types_fail_with_a_one_line_message(project: Path) -> None:
    for text, key, expect in (
        ('[run]\nmax_cost = "five"\n', "max_cost", "a number"),
        ("[run]\nhash_distance = 2.5\n", "hash_distance", "a whole number"),
        ('[run]\nocr = "yes"\n', "ocr", "true or false"),
        ("[run]\nmodel = 3\n", "model", "text in quotes"),
        ("[run]\nmax_frames = true\n", "max_frames", "a whole number"),
        ('[watch]\ninterval = "soon"\n', "interval", "a number"),
    ):
        message = _error(project, text)
        assert message.startswith("specto.toml:") and f'"{key}"' in message and expect in message and "allowed keys:" in message


def test_wrong_values_fail_too(project: Path) -> None:
    assert "one of claude, gemini" in _error(project, '[run]\nprovider = "openai"\n')
    assert "one of low, medium, high, xhigh, max" in _error(project, '[run]\neffort = "extreme"\n')
    assert '"crop"' in _error(project, '[run]\ncrop = "left half"\n')
    assert "at least 1" in _error(project, "[run]\nmax_frames = 0\n")
    assert "not valid TOML" in _error(project, "[run\nmodel = 1\n")


def test_valid_crop_forms_are_accepted(project: Path) -> None:
    assert _load(project, '[run]\ncrop = "10,20,800,600"\n').values["crop"] == "10,20,800,600"
    assert _load(project, '[run]\ncrop = "AUTO"\n').values["crop"] == "AUTO"


@pytest.mark.parametrize("text", [
    '[run]\napi_key = "abc"\n',
    '[run]\nANTHROPIC_API_KEY = "abc"\n',
    '[run]\ngemini_api_key = "abc"\n',
    '[run]\ntoken = "abc"\n',
    'password = "abc"\n',
    '[anthropic]\napi_key = "abc"\n',
    '[run]\nmodel = "sk-ant-api03-abcdef"\n',
])
def test_a_secret_in_the_file_is_refused_and_never_echoed(project: Path, text: str) -> None:
    message = _error(project, text)
    assert "put keys in the environment, not in this file" in message
    assert '"abc"' not in message and "sk-ant" not in message and "abcdef" not in message


def test_a_secret_stops_the_command_before_it_runs(project: Path, monkeypatch, capsys) -> None:
    (project / "specto.toml").write_text('[run]\napi_key = "hunter2"\n', encoding="utf-8")
    seen = _capture(monkeypatch, "cmd_run")
    assert main(["run", "walkthrough.mp4"]) == 2
    captured = capsys.readouterr()
    assert not seen
    assert "put keys in the environment, not in this file" in captured.err
    assert "hunter2" not in captured.out + captured.err


def test_a_broken_file_stops_the_command_with_one_line(project: Path, monkeypatch, capsys) -> None:
    (project / "specto.toml").write_text("[run]\nmax_cost = \"x\"\n", encoding="utf-8")
    seen = _capture(monkeypatch, "cmd_run")
    assert main(["run", "walkthrough.mp4"]) == 2
    err = capsys.readouterr().err.strip()
    assert len(err.splitlines()) == 1 and err.startswith("specto.toml:")
    assert not seen


# ------------------------------------------------------------------ init


def test_init_writes_a_file_that_parses_and_changes_nothing(project: Path, monkeypatch, capsys) -> None:
    assert main(["init"]) == 0
    text = (project / "specto.toml").read_text(encoding="utf-8")
    assert tomllib.loads(text) == {"run": {}, "watch": {}}  # everything is commented out
    for setting in config.SETTINGS:
        assert f"# {setting.name} = " in text, setting.name
        assert f"# {setting.meaning}" in text, setting.name
    assert not re.search(r"^[a-z_]+ = ", text, flags=re.M)

    loaded = config.load_config()
    assert loaded is not None and loaded.values == {}
    with_file = _run(monkeypatch)
    (project / "specto.toml").unlink()
    without_file = _run(monkeypatch)
    assert with_file == without_file
    capsys.readouterr()


def test_init_starter_is_valid_once_every_line_is_switched_on(project: Path) -> None:
    text = config.starter_text()
    switched_on = re.sub(r"^# ([a-z_]+ = )", r"\1", text, flags=re.M)
    loaded = _load(project, switched_on)
    assert set(loaded.values) == {s.name for s in config.SETTINGS}
    for setting in config.SETTINGS:
        assert loaded.values[setting.name] == setting.example


def test_init_refuses_to_overwrite_without_force(project: Path, capsys) -> None:
    (project / "specto.toml").write_text('[run]\nmodel = "mine"\n', encoding="utf-8")
    assert main(["init"]) == 2
    assert "already exists" in capsys.readouterr().err
    assert (project / "specto.toml").read_text(encoding="utf-8") == '[run]\nmodel = "mine"\n'
    assert main(["init", "--force"]) == 0
    assert "# model = " in (project / "specto.toml").read_text(encoding="utf-8")


# ---------------------------------------------------------------- doctor


def _doctor_line(capsys) -> str:
    main(["doctor"])
    lines = [line for line in capsys.readouterr().out.splitlines() if "Project settings" in line]
    assert len(lines) == 1
    return lines[0]


def test_doctor_says_when_there_is_no_file(project: Path, capsys) -> None:
    line = _doctor_line(capsys)
    assert line.startswith("-- ") and "no specto.toml here" in line and "specto init" in line


def test_doctor_lists_the_values_the_file_sets(project: Path, capsys) -> None:
    (project / "specto.toml").write_text('[run]\nprovider = "gemini"\nmax_cost = 5\n[watch]\ninterval = 30\n', encoding="utf-8")
    line = _doctor_line(capsys)
    assert line.startswith("ok ")
    assert "specto.toml found" in line
    assert 'provider = "gemini"' in line and "max_cost = 5.0" in line and "interval = 30.0" in line


def test_doctor_says_when_the_file_sets_nothing_and_when_it_is_broken(project: Path, capsys) -> None:
    main(["init"])
    capsys.readouterr()
    assert "sets nothing" in _doctor_line(capsys)
    (project / "specto.toml").write_text('[run]\nmodle = "x"\n', encoding="utf-8")
    line = _doctor_line(capsys)
    assert line.startswith("-- ") and 'unknown key "modle"' in line


def test_doctor_reads_the_file_named_by_config(project: Path, capsys) -> None:
    other = project / "team.toml"
    other.write_text('[run]\nmodel = "team-model"\n', encoding="utf-8")
    main(["doctor", "--config", str(other)])
    assert 'model = "team-model"' in capsys.readouterr().out


# ------------------------------------------------------------ watch, real


def _screenshots(folder: Path) -> None:
    from PIL import Image, ImageDraw

    folder.mkdir()
    for n, (colour, label) in enumerate((((40, 70, 140), "Search"), ((245, 245, 245), "Details")), start=1):
        img = Image.new("RGB", (640, 360), colour)
        ImageDraw.Draw(img).text((40, 40), label, fill=(0, 0, 0) if sum(colour) > 380 else (255, 255, 255))
        img.save(folder / f"shot_{n}.png")


def test_watch_once_takes_out_from_the_config(project: Path, capsys) -> None:
    incoming = project / "incoming"
    incoming.mkdir()
    _screenshots(incoming / "shots_a")
    (project / "specto.toml").write_text('[run]\nout = "team-out"\nmax_cost = 1000\n', encoding="utf-8")

    assert main(["watch", str(incoming), "--fake", "--once"]) == 0
    text = capsys.readouterr().out
    assert "1 processed, 0 skipped, 0 failed" in text
    assert (project / "team-out" / "shots_a" / "analysis.json").exists()
    assert not (project / "out").exists()


def test_watch_once_takes_max_cost_from_the_config(project: Path, capsys) -> None:
    incoming = project / "incoming"
    incoming.mkdir()
    _screenshots(incoming / "shots_a")
    (project / "specto.toml").write_text('[run]\nout = "team-out"\nmax_cost = 0.0000001\n', encoding="utf-8")

    assert main(["watch", str(incoming), "--fake", "--once"]) == 0
    captured = capsys.readouterr()
    assert "0 processed, 0 skipped, 1 failed" in captured.out
    assert "--max-cost" in captured.err
    assert not (project / "team-out" / "shots_a" / "analysis.json").exists()

    # a flag typed on the command line beats the file: a generous cap lets the item through
    assert main(["watch", str(incoming), "--fake", "--once", "--max-cost", "1000"]) == 0
    assert "1 processed, 0 skipped, 0 failed" in capsys.readouterr().out
    assert (project / "team-out" / "shots_a" / "analysis.json").exists()
