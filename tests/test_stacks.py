from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from harness.executors import LocalExecutor
from harness.stacks import (
    StackError,
    builtin_profiles,
    detect_stacks,
    load_profiles,
    resolve_stacks,
)


def test_builtin_profiles() -> None:
    profiles = builtin_profiles()
    assert {"python", "cpp-cmake"} <= set(profiles)
    for p in profiles.values():
        assert p.test, f"{p.name} must define a test step"


def test_command_rendering_quotes_inputs() -> None:
    py = builtin_profiles()["python"]
    assert py.command("lint", files=["a.py", "b c.py"]) == "uv run ruff check a.py 'b c.py'"
    assert py.command("test_one", test_id="tests/t.py::test_x") == (
        "uv run pytest -q tests/t.py::test_x"
    )
    assert py.command("build") is None
    with pytest.raises(StackError):
        py.command("lint")


def test_shared_source_and_test_files() -> None:
    cpp = builtin_profiles()["cpp-cmake"]
    assert cpp.is_shared_file("CMakeLists.txt")
    assert cpp.is_shared_file("src/net/CMakeLists.txt")
    assert not cpp.is_shared_file("src/net/socket.cpp")
    assert cpp.is_source_file("src/net/socket.hpp")
    py = builtin_profiles()["python"]
    assert py.is_shared_file("pkg/__init__.py") and py.is_shared_file("pyproject.toml")
    assert py.is_test_file("tests/unit/test_x.py")


def test_detect_and_project_override(tmp_path: Path) -> None:
    (tmp_path / "CMakeLists.txt").write_text("")
    (tmp_path / "pyproject.toml").write_text("")
    assert set(detect_stacks(tmp_path, load_profiles())) == {"python", "cpp-cmake"}

    override = tmp_path / ".harness" / "stacks" / "python.yaml"
    override.parent.mkdir(parents=True)
    override.write_text("name: python\ndetect: [pyproject.toml]\ntest: python -m pytest\n")
    (py,) = resolve_stacks(tmp_path, ["python"])
    assert py.test == "python -m pytest"
    with pytest.raises(StackError, match="unknown stack"):
        resolve_stacks(tmp_path, ["cobol"])


CMAKE = """cmake_minimum_required(VERSION 3.16)
project(demo CXX)
add_executable(adder_test adder_test.cpp)
add_test(NAME adder_test COMMAND adder_test)
"""


@pytest.mark.skipif(not shutil.which("cmake") or not shutil.which("ctest"), reason="needs cmake")
async def test_cpp_profile_red_then_green(tmp_path: Path) -> None:
    """The real cpp-cmake profile can run a test that fails, then passes after a fix."""
    (tmp_path / "CMakeLists.txt").write_text(CMAKE)
    src = tmp_path / "adder_test.cpp"
    src.write_text("int add(int a,int b){return a-b;}\nint main(){return add(2,2)==4?0:1;}\n")
    (cpp,) = resolve_stacks(tmp_path)
    ex = LocalExecutor(tmp_path)

    for step in ("configure", "build"):
        res = await cpp.run(step, ex)
        assert res is not None and res.ok, res
    missing = await cpp.run("test_one", ex, test_id="no_such_test")
    assert missing is not None and not missing.ok  # "no tests found" must not count as a pass

    (tmp_path / "CMakeLists.txt").write_text(CMAKE + "enable_testing()\n")
    res = await cpp.run("configure", ex)
    assert res is not None and res.ok
    build = await cpp.run("build", ex)
    assert build is not None and build.ok
    red = await cpp.run("test_one", ex, test_id="adder_test")
    assert red is not None and not red.ok

    src.write_text("int add(int a,int b){return a+b;}\nint main(){return add(2,2)==4?0:1;}\n")
    build = await cpp.run("build", ex)
    assert build is not None and build.ok
    green = await cpp.run("test", ex)
    assert green is not None and green.ok, green.stdout
