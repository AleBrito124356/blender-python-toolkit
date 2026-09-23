"""bpt launcher: Blender discovery and command construction (no Blender needed)."""

import json
import os
import subprocess
import sys
import types

import pytest

from bpt import cli, discovery


def make_exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("fake")
    return str(path)


@pytest.mark.parametrize("path, version", [
    ("C:/Program Files/Blender Foundation/Blender 5.2/blender.exe", (5, 2)),
    ("C:\\Program Files\\Blender Foundation\\Blender 4.2\\blender.exe", (4, 2)),
    ("/opt/blender-4.2.3-linux-x64/blender", (4, 2, 3)),
    ("/Applications/Blender 3.6.app/Contents/MacOS/Blender", (3, 6)),
    ("/Applications/Blender.app/Contents/MacOS/Blender", None),
    ("/usr/bin/blender", None),
])
def test_version_from_path(path, version):
    assert discovery.version_from_path(path) == version


def test_newest_windows_install_wins(tmp_path):
    old = make_exe(tmp_path / "Blender Foundation" / "Blender 4.2" / "blender.exe")
    new = make_exe(tmp_path / "Blender Foundation" / "Blender 5.2" / "blender.exe")
    make_exe(tmp_path / "Blender Foundation" / "Blender 3.6" / "blender.exe")
    env = {"ProgramFiles": str(tmp_path)}
    found = discovery.find_candidates(env=env, platform="win32", which=lambda _: None)
    assert [c.path for c in found][:2] == [new, old]
    best = discovery.find_blender(env=env, platform="win32", which=lambda _: None)
    assert best.path == new and best.version == (5, 2) and best.source == "install"


def test_version_sorting_is_numeric_not_lexical(tmp_path):
    make_exe(tmp_path / "Blender Foundation" / "Blender 4.9" / "blender.exe")
    ten = make_exe(tmp_path / "Blender Foundation" / "Blender 4.10" / "blender.exe")
    best = discovery.find_blender(env={"ProgramFiles": str(tmp_path)}, platform="win32", which=lambda _: None)
    assert best.path == ten


def test_precedence_env_then_path_then_install(tmp_path):
    install = make_exe(tmp_path / "Blender Foundation" / "Blender 5.2" / "blender.exe")
    on_path = make_exe(tmp_path / "bin" / "blender")
    via_env = make_exe(tmp_path / "custom" / "blender.exe")
    env = {"ProgramFiles": str(tmp_path), "BLENDER_EXE": via_env}
    order = discovery.find_candidates(env=env, platform="win32", which=lambda _: on_path)
    assert [(c.path, c.source) for c in order] == [(via_env, "BLENDER_EXE"), (on_path, "PATH"),
                                                   (install, "install")]
    explicit = make_exe(tmp_path / "explicit" / "blender.exe")
    assert discovery.find_blender(explicit, env=env, platform="win32").source == "--blender"


def test_duplicates_are_listed_once(tmp_path):
    exe = make_exe(tmp_path / "Blender Foundation" / "Blender 5.2" / "blender.exe")
    env = {"ProgramFiles": str(tmp_path), "BLENDER_EXE": exe}
    found = discovery.find_candidates(env=env, platform="win32", which=lambda _: exe)
    assert len(found) == 1 and found[0].source == "BLENDER_EXE"


def test_linux_and_macos_patterns(tmp_path):
    home = tmp_path / "home"
    exe = make_exe(home / "blender-4.2.1-linux-x64" / "blender")
    best = discovery.find_blender(env={}, platform="linux", which=lambda _: None, home=str(home))
    assert best.path == exe and best.version == (4, 2, 1)
    mac = discovery.install_patterns("darwin", {}, "/Users/me")
    assert "/Applications/Blender*.app/Contents/MacOS/Blender" in mac


def test_errors_are_actionable(tmp_path):
    with pytest.raises(discovery.BlenderNotFound, match="blender.org"):
        discovery.find_blender(env={"ProgramFiles": str(tmp_path)}, platform="win32", which=lambda _: None)
    with pytest.raises(discovery.BlenderNotFound, match="BLENDER_EXE"):
        discovery.find_blender(env={"BLENDER_EXE": str(tmp_path / "missing.exe")}, platform="win32")
    with pytest.raises(discovery.BlenderNotFound, match="--blender"):
        discovery.find_blender(str(tmp_path / "nope"), env={})


def test_build_command_always_adds_safe_flags():
    cmd = cli.build_command("blender", "scripts/procedural_city.py", ["--blocks", "2"])
    assert cmd == ["blender", "--background", "--factory-startup", "--python-exit-code", "1",
                   "--python", "scripts/procedural_city.py", "--", "--blocks", "2"]
    cmd = cli.build_command("blender", "s.py", ["--", "--x"], factory_startup=False)
    assert "--factory-startup" not in cmd and cmd[-2:] == ["--", "--x"] and cmd.count("--") == 1
    assert cli.build_command("blender", "s.py")[-1] == "--"


def test_format_command_quotes_paths_with_spaces():
    cmd = ["C:/Program Files/Blender Foundation/Blender 5.2/blender.exe", "--python", "my script.py"]
    text = cli.format_command(cmd)
    if os.name == "nt":
        assert text.startswith('"C:/Program Files/Blender Foundation/Blender 5.2/blender.exe"')
        assert '"my script.py"' in text
    else:
        assert text.startswith("'C:/Program Files/")


def fake_runner(returncode=0, calls=None):
    def run(cmd, **kwargs):
        if calls is not None:
            calls.append(cmd)
        return types.SimpleNamespace(returncode=returncode, stdout="", stderr="")
    return run


@pytest.fixture
def fake_blender(tmp_path, monkeypatch):
    exe = make_exe(tmp_path / "Blender 5.2" / "blender.exe")
    monkeypatch.setenv("BLENDER_EXE", exe)
    return exe


def test_main_runs_script_and_propagates_exit_code(fake_blender):
    calls = []
    code = cli.main(["bars", "--csv", "missing.csv"], runner=fake_runner(1, calls))
    assert code == 1
    (cmd,) = calls
    script = cmd[cmd.index("--python") + 1]
    assert cmd[0] == fake_blender and script.endswith(os.path.join("scripts", "csv_to_bars3d.py"))
    assert cmd[cmd.index("--") + 1:] == ["--csv", "missing.csv"]
    assert cli.main(["city"], runner=fake_runner(0)) == 0


def test_main_dry_run_prints_and_does_not_execute(fake_blender, capsys):
    calls = []
    assert cli.main(["--dry-run", "city", "--blocks", "3"], runner=fake_runner(0, calls)) == 0
    out = capsys.readouterr().out
    assert calls == []
    assert "--factory-startup" in out and "--python-exit-code 1" in out and "procedural_city.py" in out
    assert out.strip().endswith("-- --blocks 3")


def test_script_help_is_passed_through(fake_blender):
    calls = []
    cli.main(["city", "--help"], runner=fake_runner(0, calls))
    assert calls[0][-1] == "--help"


def test_run_arbitrary_script(fake_blender, tmp_path):
    script = tmp_path / "mine.py"
    script.write_text("print('hi')")
    calls = []
    cli.main(["run", str(script), "--flag"], runner=fake_runner(0, calls))
    assert calls[0][calls[0].index("--python") + 1] == str(script) and calls[0][-1] == "--flag"
    with pytest.raises(SystemExit):
        cli.main(["run", str(tmp_path / "nope.py")], runner=fake_runner(0))


def test_missing_blender_is_an_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BLENDER_EXE", str(tmp_path / "missing.exe"))
    assert cli.main(["city"], runner=fake_runner(0)) == 1
    assert "BLENDER_EXE" in capsys.readouterr().err


def test_unknown_command_is_rejected(fake_blender):
    with pytest.raises(SystemExit) as info:
        cli.main(["nonsense"])
    assert info.value.code == 2


def test_doctor_parses_blender_answer(fake_blender, capsys):
    payload = ('{"version": "5.2.1 LTS", "python": "3.13.1", "python_executable": "py", '
               '"engines": ["BLENDER_EEVEE", "CYCLES"], "ffmpeg": true, "cycles_gpu": {}, '
               '"numpy": "2.3.4", "pyyaml": null}')

    def run(cmd, **kwargs):
        assert "--python-expr" in cmd and "--factory-startup" in cmd
        return types.SimpleNamespace(returncode=0, stdout="noise\nBPT_DOCTOR=" + payload + "\n", stderr="")

    assert cli.main(["doctor"], runner=run) == 0
    out = capsys.readouterr().out
    assert "5.2.1 LTS" in out and "BLENDER_EEVEE, CYCLES" in out and "pip install pyyaml" in out
    assert "renders on the CPU" in out


def test_doctor_json_flag_works_before_or_after_the_command(fake_blender, capsys):
    payload = ('{"version": "5.2.1 LTS", "python": "3.13.1", "python_executable": "py", '
               '"engines": ["BLENDER_EEVEE"], "ffmpeg": false, "cycles_gpu": {"OPTIX": ["RTX"]}, '
               '"numpy": null, "pyyaml": "6.0"}')

    def run(cmd, **kwargs):
        return types.SimpleNamespace(returncode=0, stdout="BPT_DOCTOR=" + payload, stderr="")

    for argv in (["doctor", "--json"], ["--json", "doctor"]):
        assert cli.main(argv, runner=run) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["blender"]["version"] == "5.2.1 LTS" and data["blender"]["cycles_gpu"] == {"OPTIX": ["RTX"]}
    with pytest.raises(SystemExit):
        cli.main(["doctor", "--bogus"], runner=run)


def test_doctor_reports_a_broken_blender(fake_blender, capsys):
    def run(cmd, **kwargs):
        return types.SimpleNamespace(returncode=1, stdout="crash", stderr="boom")
    assert cli.main(["doctor"], runner=run) == 1
    assert "did not answer" in capsys.readouterr().err


def test_python_dash_m_entry_point(repo_root):
    out = subprocess.run([sys.executable, "-m", "bpt", "--version"], cwd=repo_root,
                         capture_output=True, text=True, check=True).stdout
    assert out.strip().startswith("bpt ")
