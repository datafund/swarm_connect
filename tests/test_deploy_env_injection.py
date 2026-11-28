"""deploy.yml passes values to the env-file steps through env:, not inline (#654).

Actions replaces a ${{ }} expression in a run: script with its value before
bash runs, so a quoted heredoc delimiter does not protect it: a variable or
secret holding a newline could add lines to the env file or change the
script. The two "write env file" steps therefore take every value through
their env: map, refuse a value with a control character before the file is
touched, and write the same file as before for every other value.
"""
import re
import stat

import pytest

from tests.deploy_env_steps import (
    BASH, BEFORE_654, ENV_STEPS, EXPRESSION, old_file, render, run_step, spec_lines, steps, written_names,
)

STEP_NAMES = list(ENV_STEPS)


def _before(step_name):
    """Today's heredoc body, frozen from origin/dev before #654."""
    return (BEFORE_654 / ENV_STEPS[step_name][1]).read_text()


def _names_in_old(step_name):
    return [m.group(2) for m in EXPRESSION.finditer(_before(step_name))]


def _all_set(step_name):
    """A distinct ordinary value for every setting the step writes."""
    return {name: f"value-{i}-{name.lower()}" for i, name in enumerate(_names_in_old(step_name))}


def _awkward(step_name):
    """Values a shell or an env file could mangle, but with no control characters."""
    samples = ["$HOME", "`id`", "$(id)", "a\\nb", "it's", 'say "hi"', " lead and trail ",
               "k=v=w", "# not a comment", "${X402_ENABLED}", "*", "~", "!x", "%s%d", "-n", "caf\u00e9 \u2713"]
    return {name: samples[i % len(samples)] for i, name in enumerate(_names_in_old(step_name))}


def _empty(step_name):
    return {name: "" for name in _names_in_old(step_name)}


VALUE_SETS = {
    "unset": lambda step_name: {},  # every default
    "empty": _empty,  # empty values also give the default
    "all_set": _all_set,
    "awkward": _awkward,
}


@pytest.mark.parametrize("step_name", STEP_NAMES)
def test_no_expression_in_the_write_env_scripts(step_name):
    assert "${{" not in steps()[step_name]["run"], step_name


@pytest.mark.parametrize("step_name", STEP_NAMES)
def test_env_keys_match_the_written_names_in_order(step_name):
    step = steps()[step_name]
    env = step.get("env") or {}
    names = written_names(step)
    assert names, step_name
    assert len(names) == len(set(names)), step_name
    assert list(env) == names, step_name
    for name, value in env.items():
        m = EXPRESSION.fullmatch(str(value))
        assert m and m.group(2) == name, (name, value)
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", name), name
        assert not _reserved(name), name


# The step reads each setting with ${!NAME}, and each name is also an env:
# key. A setting named like a bash or runner variable would break the step
# (PATH, IFS, LC_ALL, which the script sets) or write the shell's own value
# (PWD, RANDOM, UID, ...) instead of the GitHub variable.
RESERVED_NAMES = {
    "PATH", "HOME", "IFS", "PWD", "OLDPWD", "SHELL", "SHELLOPTS", "SECONDS", "RANDOM",
    "LINENO", "UID", "EUID", "PPID", "LC_ALL", "LC_CTYPE", "LANG", "ENV", "OPTIND",
    "OPTARG", "PS4", "TMPDIR", "USER", "CI",
}
RESERVED_PREFIXES = ("BASH", "GITHUB_", "RUNNER_", "ACTIONS_", "LC_")


def _reserved(name):
    return name in RESERVED_NAMES or name.startswith(RESERVED_PREFIXES)


def test_reserved_names_are_caught():
    for name in ("PATH", "HOME", "IFS", "PWD", "OLDPWD", "SHELL", "SECONDS", "RANDOM",
                 "LINENO", "UID", "EUID", "PPID", "LC_ALL", "LANG", "BASH_ENV", "BASHOPTS",
                 "GITHUB_TOKEN", "RUNNER_TEMP", "ACTIONS_STEP_DEBUG", "CI"):
        assert _reserved(name), name
    for name in ("X402_ENABLED", "CI_MODE", "PATH_PREFIX", "HOME_URL", "GATEWAY_DAILY_BZZ_CEILING",
                 "TRUSTED_PROXY_HOPS", "SUPPORT_CONTACT"):
        assert not _reserved(name), name


@pytest.mark.parametrize("step_name", STEP_NAMES)
def test_env_entries_keep_todays_expressions(step_name):
    """Same expression and default for every setting that existed before #654,
    in the same order. A setting added later is not in the frozen heredoc."""
    expected = [tuple(line.split("=", 1)) for line in _before(step_name).splitlines()
                if EXPRESSION.search(line)]
    known = {name for name, _ in expected}
    env = steps()[step_name].get("env") or {}
    assert [(k, str(v)) for k, v in env.items() if k in known] == expected


@pytest.mark.parametrize("step_name", STEP_NAMES)
@pytest.mark.parametrize("values", list(VALUE_SETS))
def test_rendered_file_is_byte_identical_to_before(step_name, values, tmp_path):
    """The file the heredoc wrote before #654 (frozen from origin/dev) and the
    file the step writes now, for the same variables, are the same bytes.
    Lines for settings added after #654 are left out of the comparison, so
    adding a setting does not mean editing the frozen copy."""
    variables = VALUE_SETS[values](step_name)
    step = steps()[step_name]
    proc, env_file = run_step(step, variables, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    added = tuple(f"{name}=" for name in written_names(step) if name not in _names_in_old(step_name))
    written = b"".join(line for line in env_file.read_bytes().splitlines(keepends=True)
                       if not (added and line.decode().startswith(added)))
    assert written == old_file(step_name, variables).encode()


@pytest.mark.parametrize("step_name", STEP_NAMES)
def test_env_file_is_owner_only(step_name, tmp_path):
    (tmp_path / "opt").mkdir()
    existing = tmp_path / "opt" / ENV_STEPS[step_name][0].rsplit("/", 1)[1]
    existing.write_text("OLD=1\n")
    existing.chmod(0o644)
    proc, env_file = run_step(steps()[step_name], _all_set(step_name), tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


INJECTED = "INJECTED_BY_TEST=1"


@pytest.mark.parametrize("step_name", STEP_NAMES)
@pytest.mark.parametrize("bad", [f"x\n{INJECTED}", f"x\r\n{INJECTED}", f"x\r{INJECTED}",
                                 f"x\t{INJECTED}", f"x\n", f"\n{INJECTED}\n"])
@pytest.mark.parametrize("target", ["X402_PROTOCOL_VERSIONS", "SUPPORT_CONTACT",
                                    "X402_FACILITATOR_PAYAI_API_KEY_SECRET"])
def test_a_control_character_fails_the_step_and_changes_nothing(step_name, bad, target, tmp_path):
    previous = b"PREVIOUS=kept\n"
    (tmp_path / "opt").mkdir()
    env_path = tmp_path / "opt" / ENV_STEPS[step_name][0].rsplit("/", 1)[1]
    env_path.write_bytes(previous)
    variables = dict(_all_set(step_name), **{target: bad})
    proc, env_file = run_step(steps()[step_name], variables, tmp_path)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert env_file.read_bytes() == previous
    assert INJECTED not in out
    # One annotation, naming the variable and nothing about its value.
    assert proc.stdout.splitlines() == [_error(target)], out
    assert proc.stderr == "", out


def _error(name):
    return (f"::error title=Invalid deploy setting::{name} contains a newline or another "
            f"control character. Fix the GitHub variable or secret {name} and re-run the "
            "deploy; the env file was not changed.")


@pytest.mark.parametrize("step_name", STEP_NAMES)
def test_every_bad_variable_is_named(step_name, tmp_path):
    variables = dict(_all_set(step_name), TERMS_URL="a\nb", SUPPORT_CONTACT="c\rd")
    proc, _ = run_step(steps()[step_name], variables, tmp_path)
    assert proc.returncode != 0
    assert proc.stdout.splitlines() == [_error("TERMS_URL"), _error("SUPPORT_CONTACT")]


def test_the_helper_renders_like_actions():
    assert render("A=${{ vars.A || 'd' }}", {}) == "A=d"
    assert render("A=${{ vars.A || 'd' }}", {"A": ""}) == "A=d"
    assert render("A=${{ vars.A || 'd' }}", {"A": "x"}) == "A=x"
    assert render("A=${{ vars.A }}", {}) == "A="
    assert render("A=${{ secrets.A || '' }}", {"A": "s"}) == "A=s"
    assert BASH == "/bin/bash"


def test_spec_lines_have_no_expression_and_no_blank_line():
    for step_name in STEP_NAMES:
        lines = spec_lines(steps()[step_name])
        assert all("${{" not in line and line for line in lines), step_name
