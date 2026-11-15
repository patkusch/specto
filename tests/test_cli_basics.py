import pytest

from specto.cli import main


def run(capsys, argv):
    try:
        code = main(argv)
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr()
    return code, out.out, out.err


def test_help_exits_cleanly_and_names_the_tool(capsys):
    code, out, _ = run(capsys, ["--help"])
    assert code == 0
    assert "specto" in out


def test_an_option_before_a_command_is_refused_and_nothing_runs(capsys):
    code, out, err = run(capsys, ["--nope"])
    assert code == 2
    assert out == ""
    assert "error" in err.lower()


def test_no_command_is_refused_with_a_usage_message(capsys):
    code, _, err = run(capsys, [])
    assert code == 2
    assert "usage" in err.lower()


def test_an_unknown_command_is_refused(capsys):
    code, _, err = run(capsys, ["bogus"])
    assert code == 2
    assert "bogus" in err
