"""Project settings: an optional `specto.toml` so a team does not retype the same flags.

    [run]
    provider = "gemini"
    max_cost = 5.0

    [watch]
    interval = 30

The keys in `[run]` are the flag names with underscores (`--max-cost` is
`max_cost`). Lookup: `--config PATH` if given, else `./specto.toml`, else
nothing. Order of precedence: a flag typed on the command line, then the file,
then the built-in default. A flag left off never overwrites the file, and a flag
typed with its default value still wins over the file.

The file never holds secrets: a key that looks like one is refused, and no
value from the file is ever printed except the ones this module has checked.
"""
from __future__ import annotations

import difflib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "specto.toml"
SECRET_ADVICE = "put keys in the environment, not in this file"


class ConfigError(Exception):
    """A problem with the config file, worded as one line for the person who wrote it."""


class _Unset:
    """The argparse default for every setting the file may supply, so the code
    can tell "the flag was not typed" from "the flag was typed with its default"."""

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET = _Unset()


@dataclass(frozen=True)
class Setting:
    name: str
    table: str
    kind: str  # "text", "int", "number" or "bool"
    default: Any
    example: Any
    meaning: str
    choices: tuple[str, ...] = ()
    minimum: float | None = None


SETTINGS: tuple[Setting, ...] = (
    Setting("provider", "run", "text", "claude", "claude",
            'Which company\'s model reads the recording: "claude" or "gemini".', choices=("claude", "gemini")),
    Setting("model", "run", "text", "claude-opus-5", "claude-opus-5",
            "The model that reads the recording and writes the requirements."),
    Setting("reader_model", "run", "text", None, "claude-sonnet-5",
            "A cheaper model for the first look at each frame; the final write-up still uses model. Unset: one model does both."),
    Setting("effort", "run", "text", "high", "high",
            "How hard the model thinks: low, medium, high, xhigh or max. Higher costs more.",
            choices=("low", "medium", "high", "xhigh", "max")),
    Setting("max_cost", "run", "number", None, 5.0,
            "Stop before calling the model if the estimate is above this many US dollars. Unset: no limit.", minimum=0),
    Setting("crop", "run", "text", None, "auto",
            'Keep only part of the picture: "auto" finds the shared window, or give x,y,width,height in pixels. Unset: the whole picture.'),
    Setting("detect", "run", "text", "hash", "hash",
            'How a change of screen is spotted: "hash" compares pictures, "scene" watches brightness.',
            choices=("hash", "scene")),
    Setting("hash_distance", "run", "int", 8, 8,
            "How different a frame must look to count as a new screen. Lower catches typed text.", minimum=0),
    Setting("sample_fps", "run", "number", 1.0, 1.0,
            "How many frames a second are looked at.", minimum=0.01),
    Setting("max_frames", "run", "int", 240, 240,
            "The most still images kept; over this, the ones that changed least are dropped first.", minimum=1),
    Setting("frames_per_call", "run", "int", 8, 8,
            "How many images go to the model in one request.", minimum=1),
    Setting("ocr", "run", "bool", True, True,
            "Read the text on each frame and show it to the model (needs the ocr extra)."),
    Setting("out", "run", "text", None, "out/team",
            "Where results are written. Unset: out/<recording name>, or for watch a folder called out beside the watched one."),
    Setting("whisper_model", "run", "text", "base", "base",
            "Size of the local speech-to-text model, used when there is no transcript file."),
    Setting("interval", "watch", "number", 10, 10,
            "Seconds between looks at the watched folder.", minimum=0.01),
)

BY_NAME = {s.name: s for s in SETTINGS}
TABLES: dict[str, tuple[str, ...]] = {
    table: tuple(s.name for s in SETTINGS if s.table == table) for table in ("run", "watch")
}
RUN_KEYS = TABLES["run"]

# Which settings each command takes from the file. `watch` runs the whole
# pipeline on every item, so it takes every [run] key too, even the ones it has
# no flag for.
COMMAND_KEYS: dict[str, tuple[str, ...]] = {
    "run": RUN_KEYS,
    "watch": RUN_KEYS + ("interval",),
    "live": ("provider", "model", "effort", "whisper_model", "out"),
    "resolve": ("provider", "model", "effort"),
    "demo": ("out",),
}


@dataclass
class Config:
    path: Path
    values: dict[str, Any]


_SECRET_WORDS = {"key", "keys", "apikey", "token", "secret", "secrets", "password", "passwd", "credential", "credentials"}
_SECRET_VALUE = re.compile(r"^(sk-|AIza)")


def _looks_like_secret_name(name: str) -> bool:
    return any(part in _SECRET_WORDS for part in re.split(r"[^a-z0-9]+", name.lower()))


def _refuse_secrets(path: Path, data: dict) -> None:
    """Look at key names (and only whether a text value starts like a key) at any depth."""
    for name, value in data.items():
        if _looks_like_secret_name(name):
            raise ConfigError(f'{path}: "{name}" looks like a secret; {SECRET_ADVICE}')
        if isinstance(value, str) and _SECRET_VALUE.match(value):
            raise ConfigError(f'{path}: "{name}" holds something that looks like an API key; {SECRET_ADVICE}')
        if isinstance(value, dict):
            _refuse_secrets(path, value)


def _allowed(names: tuple[str, ...]) -> str:
    return ", ".join(names)


def _unknown(path: Path, name: str, where: str, allowed: tuple[str, ...]) -> ConfigError:
    guess = difflib.get_close_matches(name, allowed, n=1)
    hint = f' (did you mean "{guess[0]}"?)' if guess else ""
    return ConfigError(f'{path}: unknown key "{name}" {where}; allowed keys: {_allowed(allowed)}{hint}')


_KIND_WORDS = {"text": "text in quotes", "int": "a whole number", "number": "a number", "bool": "true or false"}


def _check_value(path: Path, setting: Setting, value: Any) -> Any:
    where = f"[{setting.table}]"
    allowed = TABLES[setting.table]
    ok = {
        "text": isinstance(value, str),
        "int": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "bool": isinstance(value, bool),
    }[setting.kind]
    if not ok:
        got = type(value).__name__
        raise ConfigError(f'{path}: "{setting.name}" in {where} must be {_KIND_WORDS[setting.kind]}, got {got}; '
                          f"allowed keys: {_allowed(allowed)}")
    if setting.choices and value not in setting.choices:
        raise ConfigError(f'{path}: "{setting.name}" in {where} must be one of {", ".join(setting.choices)}, '
                          f'got "{value}"; allowed keys: {_allowed(allowed)}')
    if setting.minimum is not None and value < setting.minimum:
        raise ConfigError(f'{path}: "{setting.name}" in {where} must be at least {setting.minimum:g}, got {value}; '
                          f"allowed keys: {_allowed(allowed)}")
    if setting.name == "crop" and value.strip().lower() != "auto":
        parts = value.split(",")
        if len(parts) != 4 or not all(p.strip().lstrip("-").isdigit() for p in parts):
            raise ConfigError(f'{path}: "crop" in {where} must be "auto" or x,y,width,height in pixels; '
                              f"allowed keys: {_allowed(allowed)}")
    return float(value) if setting.kind == "number" else value


def parse_config(path: Path, text: str) -> Config:
    """Check `text` (the contents of a config file) and return what it sets."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: not valid TOML ({' '.join(str(error).split())})") from None
    _refuse_secrets(path, data)
    values: dict[str, Any] = {}
    for name, body in data.items():
        if name not in TABLES:
            if isinstance(body, dict):
                raise ConfigError(f"{path}: unknown table [{name}]; allowed tables: {_allowed(tuple(TABLES))}")
            raise ConfigError(f'{path}: unknown key "{name}" at the top level; put settings under [run] or [watch]; '
                              f"allowed tables: {_allowed(tuple(TABLES))}")
        if not isinstance(body, dict):
            raise ConfigError(f'{path}: "{name}" must be a table written [{name}]; allowed tables: {_allowed(tuple(TABLES))}')
        for key, value in body.items():
            if key not in TABLES[name]:
                raise _unknown(path, key, f"in [{name}]", TABLES[name])
            values[key] = _check_value(path, BY_NAME[key], value)
    return Config(path=path, values=values)


def find_config(explicit: str | None = None) -> Path | None:
    """`--config PATH` if given (it must exist), else ./specto.toml, else None."""
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise ConfigError(f"{path}: no such config file")
        return path
    path = Path(CONFIG_FILENAME)
    return path if path.is_file() else None


def load_config(explicit: str | None = None) -> Config | None:
    path = find_config(explicit)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"{path}: cannot read it ({error.__class__.__name__})") from None
    return parse_config(path, text)


def apply_config(args, config: Config | None) -> list[str]:
    """Fill every setting the command left untyped: the file's value, else the
    built-in default. Returns the names that came from the file. A setting the
    person typed on the command line is never touched."""
    from_file: list[str] = []
    for name in COMMAND_KEYS.get(getattr(args, "command", ""), ()):
        if getattr(args, name, UNSET) is not UNSET:
            continue
        if config is not None and name in config.values:
            setattr(args, name, config.values[name])
            from_file.append(name)
        else:
            setattr(args, name, BY_NAME[name].default)
    return from_file


def value_or_default(args, name: str) -> Any:
    """`args.<name>`, or the built-in default when the command has no such setting yet."""
    value = getattr(args, name, UNSET)
    return BY_NAME[name].default if value is UNSET else value


def describe(config: Config) -> str:
    """`provider = "gemini", max_cost = 5.0`, or a line saying it sets nothing."""
    if not config.values:
        return f"{config.path} found, but it sets nothing (every line is commented out)"
    pairs = ", ".join(f"{k} = {json.dumps(v)}" for k, v in config.values.items())
    return f"{config.path} found; it sets {pairs}"


def starter_text() -> str:
    """A commented-out specto.toml: every key with its meaning and its default."""
    lines = [
        "# specto.toml: project settings for specto, so nobody retypes the same flags.",
        "# Remove the leading # from a line to use it. A flag typed on the command line always wins over this file.",
        "# Never put an API key here; keep ANTHROPIC_API_KEY or GEMINI_API_KEY in the environment.",
    ]
    for table in ("run", "watch"):
        lines += ["", f"[{table}]"]
        for setting in (s for s in SETTINGS if s.table == table):
            lines.append(f"# {setting.meaning}")
            lines.append(f"# {setting.name} = {json.dumps(setting.example)}")
    return "\n".join(lines) + "\n"
