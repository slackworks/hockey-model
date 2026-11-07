"""Tests for scripts/tlc.sh. The tests use a fake curl or a fake jar, so no test downloads TLC."""

import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "tlc.sh"


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env=env,
    )


def test_help_prints_usage_and_exits_zero() -> None:
    result = _run("--help")
    assert result.returncode == 0
    assert "Usage: bash scripts/tlc.sh" in result.stdout
    assert "Exit codes:" in result.stdout


def test_help_prints_the_whole_exit_code_block() -> None:
    assert "for a deadlock" in _run("--help").stdout


def test_unknown_argument_exits_two_and_names_it() -> None:
    result = _run("--bogus")
    assert result.returncode == 2
    assert "'--bogus'" in result.stderr


def test_extra_argument_exits_two_and_names_it() -> None:
    result = _run("--help", "extra")
    assert result.returncode == 2
    assert "'extra'" in result.stderr


def _env_with_fakes(tmp_path: Path, jars: Path, **fakes: str) -> dict[str, str]:
    """Return an environment whose PATH starts with fake commands, and the jar directory."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in fakes.items():
        fake = bin_dir / name
        fake.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
        fake.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "TLA_TOOLS_DIR": str(jars),
    }


def _fake_java(version_line: str, rest: str = "exit 0") -> str:
    """Return a fake java that prints version_line for -version and runs rest otherwise."""
    return f"case \"$*\" in -version*) echo '{version_line}' >&2; exit 0 ;; esac\n{rest}"


_JAVA_17 = _fake_java('openjdk version "17.0.7" 2023-04-18')


def test_checksum_mismatch_deletes_jar_and_names_both_sums(tmp_path: Path) -> None:
    jars = tmp_path / "jars"
    jars.mkdir()
    jar = jars / "tla2tools-v1.7.4.jar"
    jar.write_bytes(b"not the tla tools jar")
    result = _run(env=_env_with_fakes(tmp_path, jars, java=_JAVA_17))
    assert result.returncode == 1
    assert "checksum mismatch" in result.stderr
    assert "expected: 936a262061c9" in result.stderr
    assert "actual:" in result.stderr
    assert not jar.exists()


def test_failed_download_names_version_and_url(tmp_path: Path) -> None:
    jars = tmp_path / "jars"
    result = _run(env=_env_with_fakes(tmp_path, jars, java=_JAVA_17, curl="exit 22"))
    assert result.returncode == 1
    assert "cannot download tla2tools v1.7.4 from https://github.com/tlaplus" in result.stderr
    assert not (jars / "tla2tools-v1.7.4.jar").exists()


@pytest.mark.parametrize(
    ("version_line", "major"),
    [
        ('openjdk version "11.0.2" 2019-01-15', "11"),
        ('openjdk version "17.0.7" 2023-04-18', "17"),
        ('openjdk version "21" 2023-09-19', "21"),
    ],
)
def test_java_at_or_above_eleven_passes_the_version_check(
    tmp_path: Path, version_line: str, major: str
) -> None:
    # The download is stubbed to fail, so the run ends after the version check.
    jars = tmp_path / "jars"
    env = _env_with_fakes(tmp_path, jars, java=_fake_java(version_line), curl="exit 22")
    result = _run(env=env)
    assert "Java" not in result.stderr, f"Java {major} must pass the version check"
    assert "cannot download tla2tools" in result.stderr


@pytest.mark.parametrize(
    ("version_line", "major"),
    [('java version "1.8.0_292"', "8"), ('openjdk version "9.0.4"', "9")],
)
def test_java_below_eleven_fails_and_names_the_version(
    tmp_path: Path, version_line: str, major: str
) -> None:
    jars = tmp_path / "jars"
    env = _env_with_fakes(tmp_path, jars, java=_fake_java(version_line))
    result = _run(env=env)
    assert result.returncode == 1
    assert f"Java {major} is too old" in result.stderr
    assert "Install Java 11 or later" in result.stderr


def test_unreadable_java_version_fails_and_asks_for_java_eleven(tmp_path: Path) -> None:
    jars = tmp_path / "jars"
    result = _run(env=_env_with_fakes(tmp_path, jars, java=_fake_java("garbage")))
    assert result.returncode == 1
    assert "cannot read the Java version" in result.stderr


def test_missing_sha256_tools_fail_and_name_both(tmp_path: Path) -> None:
    # A PATH with every system tool except the two sha256 tools, plus a fake java.
    jars = tmp_path / "jars"
    env = _env_with_fakes(tmp_path, jars, java=_JAVA_17)
    # The original PATH must not follow: it would still hold the sha256 tools.
    fake_bin = tmp_path / "bin"
    env["PATH"] = os.pathsep.join([str(fake_bin), _path_without({"sha256sum", "shasum"}, tmp_path)])
    result = _run(env=env)
    assert result.returncode == 1
    assert "neither sha256sum nor shasum is on PATH" in result.stderr


def _path_without(names: set[str], tmp_path: Path) -> str:
    """Return a directory of symlinks to every tool on PATH except the names given."""
    link_dir = tmp_path / "no-sha"
    link_dir.mkdir()
    for directory in os.environ["PATH"].split(os.pathsep):
        source = Path(directory)
        if not source.is_dir():
            continue
        for entry in source.iterdir():
            target = link_dir / entry.name
            if entry.name not in names and not (target.exists() or target.is_symlink()):
                target.symlink_to(entry)
    return str(link_dir)


def test_workers_flag_is_rejected() -> None:
    # TLC picks its own worker count, so the script has no --workers flag.
    result = _run("--workers")
    assert result.returncode == 2
    assert "'--workers'" in result.stderr


# The fake java logs its arguments and exits with the code that the test sets for each config.
_FAKE_JAVA = """\
case "$*" in -version*) echo 'openjdk version "17.0.7" 2023-04-18' >&2; exit 0 ;; esac
echo "$*" >> "$FAKE_JAVA_LOG"
case "$*" in
    *daily_prepare_stall.cfg*) exit "${FAKE_STALL_RC:-0}" ;;
    *) exit "${FAKE_MAIN_RC:-0}" ;;
esac
"""


def _run_with_fake_java(
    tmp_path: Path, stall_rc: int, main_rc: int
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run a copy of tlc.sh whose pinned hash matches a fake jar, and return the java calls."""
    jar_bytes = b"a fake tla tools jar"
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "specs").mkdir()
    pinned = re.subn(
        r'TLA_TOOLS_SHA256="[0-9a-f]{64}"',
        f'TLA_TOOLS_SHA256="{hashlib.sha256(jar_bytes).hexdigest()}"',
        SCRIPT.read_text(encoding="utf-8"),
    )
    assert pinned[1] == 1, "tlc.sh must pin the jar with one TLA_TOOLS_SHA256 line"
    (root / "scripts" / "tlc.sh").write_text(pinned[0], encoding="utf-8")
    jars = tmp_path / "jars"
    jars.mkdir()
    (jars / "tla2tools-v1.7.4.jar").write_bytes(jar_bytes)
    log = tmp_path / "java.log"
    env = _env_with_fakes(tmp_path, jars, java=_FAKE_JAVA)
    env.update(FAKE_JAVA_LOG=str(log), FAKE_STALL_RC=str(stall_rc), FAKE_MAIN_RC=str(main_rc))
    result = subprocess.run(
        ["bash", str(root / "scripts" / "tlc.sh")],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env=env,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result, calls


def test_both_configs_pass_and_the_stall_config_runs_first(tmp_path: Path) -> None:
    result, calls = _run_with_fake_java(tmp_path, stall_rc=0, main_rc=0)
    assert result.returncode == 0
    assert len(calls) == 2
    assert "-config specs/daily_prepare_stall.cfg" in calls[0]
    assert "-config specs/daily_prepare.cfg " in calls[1]
    assert all("-workers auto" in call for call in calls)


def test_a_failed_first_config_stops_before_the_second(tmp_path: Path) -> None:
    result, calls = _run_with_fake_java(tmp_path, stall_rc=12, main_rc=0)
    assert result.returncode == 12
    assert len(calls) == 1
    assert "specs/daily_prepare_stall.cfg failed" in result.stderr


def test_a_failed_second_config_returns_its_code(tmp_path: Path) -> None:
    result, calls = _run_with_fake_java(tmp_path, stall_rc=0, main_rc=13)
    assert result.returncode == 13
    assert len(calls) == 2
    assert "specs/daily_prepare.cfg failed" in result.stderr


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (11, "found a deadlock (exit 11)"),
        (12, "found a violated invariant (exit 12)"),
        (13, "found a violated action or temporal property (exit 13)"),
        (255, "failed with exit 255. No counterexample"),
    ],
)
def test_a_tlc_exit_code_is_returned_and_named(tmp_path: Path, code: int, message: str) -> None:
    result, _ = _run_with_fake_java(tmp_path, stall_rc=0, main_rc=code)
    assert result.returncode == code
    assert message in result.stderr
