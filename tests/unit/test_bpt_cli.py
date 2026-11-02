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
    assert cli.main(["run", str(tmp_path / "nope.py")], runner=fake_runner(0)) == 2  # invalid input
    assert cli.main(["run"], runner=fake_runner(0)) == 2


def test_run_missing_script_answers_in_json(fake_blender, tmp_path, capsys):
    assert cli.main(["--json", "run", str(tmp_path / "nope.py")], runner=fake_runner(0)) == 2
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["status"] == "invalid_input" and "script not found" in data["error"]
    assert "script not found" in captured.err


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


# ---------------------------------------------------------------------------
# bpy-module runtime: kind detection, discovery order, shim command, JSON mode
# ---------------------------------------------------------------------------
CURRENT_BPY_PYTHON = discovery.current_bpy_python  # the real one, before hermetic_runtime patches it


@pytest.fixture(autouse=True)
def hermetic_runtime(monkeypatch):
    """No test may pick up a bpy installed in the interpreter running pytest."""
    monkeypatch.setattr(discovery, "current_bpy_python", lambda: None)
    monkeypatch.delenv("BLENDER_PYTHON", raising=False)


def no_probe(path):
    raise AssertionError(f"--version probe should not run for {path}")


@pytest.mark.parametrize("path, kind", [
    ("/home/me/.venvs/bpy/bin/python", "python"),
    ("/usr/bin/python3", "python"),
    ("/usr/local/bin/python3.13", "python"),
    ("C:\\Python313\\python.exe", "python"),
    ("C:\\Python313\\pythonw.exe", "python"),
    ("/opt/py/bin/python3.13t", "python"),
    ("C:/Program Files/Blender Foundation/Blender 5.2/blender.exe", "blender"),
    ("/Applications/Blender.app/Contents/MacOS/Blender", "blender"),
    ("/var/lib/flatpak/exports/bin/org.blender.Blender", "blender"),
    ("/home/me/blender-env/bin/python", "python"),  # only the file name counts
    ("/usr/local/bin/b3d", None),
    ("/usr/bin/python3.13-config", None),
])
def test_kind_from_name(path, kind):
    assert discovery.kind_from_name(path) == kind


@pytest.mark.parametrize("banner, kind", [
    ("Blender 5.2.1 LTS\n\tbuild date: 2026-10-01\n", "blender"),
    ("\nPython 3.13.18\n", "python"),
    ("Color management: using fallback mode\nBlender 4.2.3 LTS\n", "blender"),
    ("usage: whatever\n", None),
    ("", None),
])
def test_kind_from_banner(banner, kind):
    assert discovery.kind_from_banner(banner) == kind


def test_detect_kind_probes_only_ambiguous_names():
    assert discovery.detect_kind("/x/python3", probe=no_probe) == "python"
    assert discovery.detect_kind("/x/blender", probe=no_probe) == "blender"
    assert discovery.detect_kind("/x/b3d", probe=lambda p: "Blender 5.2.1\n") == "blender"
    assert discovery.detect_kind("/x/wrapper", probe=lambda p: "Python 3.13.1") == "python"
    with pytest.raises(discovery.BlenderNotFound, match="neither a Blender executable nor a Python"):
        discovery.detect_kind("/x/ls", probe=lambda p: "ls (GNU coreutils) 9.4")


def test_probe_version_banner_survives_a_missing_file(tmp_path):
    assert discovery.probe_version_banner(str(tmp_path / "missing")) == ""


def test_blender_exe_may_be_a_python_interpreter(tmp_path):
    py = make_exe(tmp_path / "venv" / "bin" / "python3.13")
    found = discovery.find_blender(env={"BLENDER_EXE": py}, platform="linux", which=lambda _: None,
                                   home=str(tmp_path), probe=no_probe)
    assert (found.path, found.kind, found.source, found.version) == (py, "python", "BLENDER_EXE", None)
    assert found.is_python and found.version_string == "unknown"


def test_bpy_interpreters_come_after_every_blender(tmp_path):
    home = tmp_path / "home"
    install = make_exe(home / "blender-5.2.1-linux-x64" / "blender")
    via_env_py = make_exe(tmp_path / "bpyenv" / "bin" / "python")
    current = make_exe(tmp_path / "dev" / "bin" / "python3")
    env = {"BLENDER_PYTHON": via_env_py}
    order = discovery.find_candidates(env=env, platform="linux", which=lambda _: None, home=str(home),
                                      probe=no_probe, bpy_python=lambda: current)
    assert [(c.path, c.source, c.kind) for c in order] == [
        (install, "install", "blender"), (via_env_py, "BLENDER_PYTHON", "python"),
        (current, "current Python", "python")]
    only_python = discovery.find_blender(env=env, platform="linux", which=lambda _: None,
                                         home=str(tmp_path / "empty"), probe=no_probe, bpy_python=lambda: current)
    assert only_python.path == via_env_py and only_python.kind == "python"
    last = discovery.find_blender(env={}, platform="linux", which=lambda _: None, home=str(tmp_path / "empty"),
                                  probe=no_probe, bpy_python=lambda: current)
    assert (last.path, last.source, last.kind) == (current, "current Python", "python")


def test_current_bpy_python_uses_find_spec(monkeypatch):
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object() if name == "bpy" else None)
    assert CURRENT_BPY_PYTHON() == sys.executable
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert CURRENT_BPY_PYTHON() is None


def test_explicit_runtime_kinds_and_path_lookup(tmp_path):
    py = make_exe(tmp_path / "bin" / "python3")
    assert discovery.find_blender(py, env={}, probe=no_probe).kind == "python"
    found = discovery.find_blender("python3", env={}, which=lambda name: py if name == "python3" else None,
                                   probe=no_probe)
    assert (found.path, found.kind, found.source) == (py, "python", "--blender")
    with pytest.raises(discovery.BlenderNotFound, match="--blender"):
        discovery.find_blender("no-such-runtime", env={}, which=lambda _: None)


def test_not_found_mentions_pip_install_bpy_and_a_missing_blender_python(tmp_path):
    kwargs = dict(platform="linux", which=lambda _: None, home=str(tmp_path), probe=no_probe,
                  bpy_python=lambda: None)
    with pytest.raises(discovery.BlenderNotFound, match="pip install bpy"):
        discovery.find_blender(env={}, **kwargs)
    with pytest.raises(discovery.BlenderNotFound, match="BLENDER_PYTHON"):
        discovery.find_blender(env={"BLENDER_PYTHON": str(tmp_path / "gone" / "python")}, **kwargs)


def test_build_command_for_a_bpy_interpreter_uses_the_shim():
    cmd = cli.build_command("/venv/bin/python", "scripts/x.py", ["--a"], kind="python")
    assert cmd[:2] == ["/venv/bin/python", cli.SHIM] and os.path.isfile(cli.SHIM)
    assert cmd[2:] == ["--background", "--factory-startup", "--python-exit-code", "1", "--python",
                       "scripts/x.py", "--", "--a"]
    assert cli.build_command("blender", "s.py", kind="blender")[0] == "blender"


@pytest.fixture
def fake_python(tmp_path, monkeypatch):
    exe = make_exe(tmp_path / "bpyenv" / "bin" / "python3.13")
    monkeypatch.setenv("BLENDER_EXE", exe)
    return exe


def test_dry_run_prints_the_shim_command(fake_python, capsys):
    assert cli.main(["--dry-run", "city", "--blocks", "3"], runner=fake_runner(0)) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith(cli.format_command([fake_python, cli.SHIM]))
    assert "--background --factory-startup --python-exit-code 1 --python" in out
    assert out.endswith("-- --blocks 3")


def test_dry_run_json(fake_python, capsys):
    assert cli.main(["--json", "--dry-run", "inspect", "--input", "a.blend"], runner=fake_runner(0)) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["dry_run"] and data["runtime"] == {"kind": "python", "path": fake_python, "source": "BLENDER_EXE"}
    assert data["argv"][:2] == [fake_python, cli.SHIM] and data["argv"][-2:] == ["--input", "a.blend"]
    assert data["argv"][data["argv"].index("--python") + 1].endswith(os.path.join("scripts", "inspect_render.py"))


def test_doctor_with_a_bpy_interpreter(fake_python, capsys):
    payload = ('{"version": "5.2.3 LTS", "python": "3.13.18", "python_executable": "py", "bpy_module": true, '
               '"engines": ["BLENDER_EEVEE", "BLENDER_WORKBENCH", "CYCLES"], "ffmpeg": true, "cycles_gpu": {}, '
               '"export": {"gltf": true, "draco": false, "usd": true}, "egl": "libEGL.so.1", '
               '"numpy": "2.5.4", "pyyaml": null}')
    seen = []

    def run(cmd, **kwargs):
        seen.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="BPT_DOCTOR=" + payload + "\n", stderr="")

    assert cli.main(["doctor"], runner=run) == 0
    assert seen[0][:2] == [fake_python, cli.SHIM] and "--python-expr" in seen[0]
    out = capsys.readouterr().out
    assert "bpy module in " + fake_python in out and "no Blender install needed" in out
    assert "Draco no" in out and "USD/USDZ yes" in out and "libEGL.so.1" in out
    assert cli.main(["--json", "doctor"], runner=run) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["blender"]["kind"] == "python" and data["blender"]["export"]["draco"] is False


def test_doctor_explains_a_python_without_bpy(fake_python, capsys):
    def run(cmd, **kwargs):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="cannot import bpy")
    assert cli.main(["doctor"], runner=run) == 1
    assert "could not import bpy" in capsys.readouterr().err


class FakePopen:
    """Stands in for subprocess.Popen: prints output and writes the summary file."""

    def __init__(self, code=0, output="log line\n", summary=None):
        self.code, self.output, self.summary, self.calls = code, output, summary, []

    def __call__(self, cmd, env=None, **kwargs):
        self.calls.append((cmd, env))
        if self.summary is not None:
            with open(env[cli.SUMMARY_ENV], "w", encoding="utf-8") as fh:
                fh.write(self.summary if isinstance(self.summary, str) else json.dumps(self.summary))
        proc = types.SimpleNamespace(stdout=iter(self.output.splitlines(keepends=True)))
        proc.wait = lambda: self.code
        proc.kill = lambda: None
        return proc


def test_json_mode_prints_one_object_and_sends_logs_to_stderr(fake_blender, capsys):
    popen = FakePopen(code=0, output="Blender 5.2.1\n[export] wrote a.glb\n",
                      summary={"script": "export_web", "status": "ok", "outputs": ["a.glb"]})
    assert cli.main(["--json", "inspect", "--input", "a.blend"], popen=popen) == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)  # exactly one JSON document on stdout
    assert data["status"] == "ok" and data["exit_code"] == 0 and data["command"] == "inspect"
    assert data["summary"]["outputs"] == ["a.glb"] and "log_tail" not in data
    assert data["runtime"]["kind"] == "blender" and data["seconds"] >= 0
    assert "[export] wrote a.glb" in captured.err and "Blender 5.2.1" in captured.err
    cmd, env = popen.calls[0]
    assert env[cli.SUMMARY_ENV].endswith("summary.json") and not os.path.exists(env[cli.SUMMARY_ENV])


@pytest.mark.parametrize("code, status", [(1, "failed"), (2, "invalid_input"), (3, "check_failed"),
                                          (127, "not_executable"), (-11, "error")])
def test_json_mode_names_each_exit_code(fake_blender, capsys, code, status):
    popen = FakePopen(code=code, output="".join(f"line {i}\n" for i in range(100)))
    assert cli.main(["--json", "city"], popen=popen) == code
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == status and data["summary"] is None
    assert data["log_tail"][-1] == "line 99" and len(data["log_tail"]) == 40


def test_json_mode_reports_an_unreadable_summary(fake_blender, capsys):
    assert cli.main(["--json", "batch", "--input", "x"], popen=FakePopen(summary="{not json")) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["summary"] is None and "unreadable summary" in data["summary_error"]


def test_json_mode_without_a_runtime_still_answers_in_json(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BLENDER_EXE", str(tmp_path / "missing.exe"))
    assert cli.main(["--json", "inspect", "--input", "a.blend"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "no_runtime" and "BLENDER_EXE" in captured.err


def test_run_to_stderr_reports_a_missing_executable(capsys):
    code, tail = cli.run_to_stderr(["/definitely/not/here"], env=dict(os.environ))
    assert code == 127 and "cannot execute" in tail[0]


# ---------------------------------------------------------------------------
# The shim's argument handling (pure; the behaviour is tested in tests/blender)
# ---------------------------------------------------------------------------
def load_shim():
    import importlib.util

    spec = importlib.util.spec_from_file_location("bpt_blender_shim_under_test", cli.SHIM)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shim_parses_blender_arguments_in_order():
    shim = load_shim()
    settings, actions = shim.parse_args(["--background", "--factory-startup", "scene.blend", "--python-exit-code",
                                         "1", "--python", "a.py", "-P", "b.py", "--python-expr", "x=1", "--",
                                         "--python", "not-for-blender.py", "positional"])
    assert settings == {"factory_startup": True, "autoexec": False}
    assert actions == [("blend", "scene.blend"), ("exit_code", 1), ("python", "a.py"), ("python", "b.py"),
                       ("expr", "x=1")]
    assert shim.parse_args(["-b", "-y", "--python-use-system-env"])[0]["autoexec"] is True


@pytest.mark.parametrize("argv, message", [
    (["--python"], "needs a value"),
    (["--python-exit-code", "x"], "integer"),
    (["--python-exit-code", "256"], "integer"),
    (["-f", "1"], "unsupported Blender argument '-f'"),
    (["--render-anim"], "unsupported"),
])
def test_shim_rejects_what_it_cannot_emulate(argv, message):
    shim = load_shim()
    with pytest.raises(shim.UsageError, match=message):
        shim.parse_args(argv)
    assert shim.main(argv) == 1


def test_shim_sys_path_drops_its_folder_and_adds_the_toolkit_root():
    shim = load_shim()
    path = shim.fix_sys_path([shim.HERE, "/somewhere/else"])
    assert shim.HERE not in path and path == ["/somewhere/else", shim.TOOLKIT_ROOT]
    assert shim.fix_sys_path(list(path)) == path  # idempotent
