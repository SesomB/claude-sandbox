"""End-to-end on a real CMake/CTest project: build files are shared and edited via replay."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from harness.board import TicketStatus
from harness.project import Project, init_project
from tests.conftest import run_git
from tests.helpers import ScriptedAgents, call, finish, ticket
from tests.test_build_loop import orchestrator

pytestmark = pytest.mark.skipif(not shutil.which("cmake"), reason="needs cmake")

CONFIG = """\
providers: {fake: {type: fake}}
models: {default: {provider: fake, model: scripted}}
stacks: [cpp-cmake]
"""
# The built-in profile minus clang-tidy, whose findings vary across toolchain versions.
STACK = """\
name: cpp-cmake
detect: [CMakeLists.txt]
configure: cmake -S . -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
build: cmake --build build -j
test: ctest --test-dir build --output-on-failure --no-tests=error
test_one: ctest --test-dir build --output-on-failure --no-tests=error -R {test_id}
source_globs: ["**/*.cpp", "**/*.hpp"]
test_globs: ["tests/**/*_test.cpp"]
shared_files: ["**/CMakeLists.txt"]
artifact_globs: [build/**]
"""
CMAKE = """\
cmake_minimum_required(VERSION 3.16)
project(app CXX)
set(CMAKE_CXX_STANDARD 17)
enable_testing()
add_executable(smoke_test tests/smoke_test.cpp)
add_test(NAME smoke_test COMMAND smoke_test)
"""


def make_cpp_app(tmp_path: Path) -> Project:
    app = tmp_path / "app"
    init_project(app, user_config=tmp_path / "none.yaml")
    files = {
        ".harness/config.yaml": CONFIG,
        ".harness/stacks/cpp-cmake.yaml": STACK,
        "CMakeLists.txt": CMAKE,
        "tests/smoke_test.cpp": "int main() { return 0; }\n",
        ".gitignore": "build/\n",
    }
    for rel, content in files.items():
        (app / rel).parent.mkdir(parents=True, exist_ok=True)
        (app / rel).write_text(content)
    run_git(app, "add", "-A")
    run_git(app, "commit", "-q", "-m", "skeleton")
    return Project.open(app, user_config=tmp_path / "none.yaml")


async def test_cpp_ticket_with_build_file_edit_and_tdd_fix(tmp_path: Path) -> None:
    project = make_cpp_app(tmp_path)
    project.board.add_tickets(
        [
            ticket(
                "T-1",
                owned_files=["src/clamp.hpp", "tests/clamp_test.cpp"],
                acceptance=["clamp(v, lo, hi) limits v to [lo, hi]"],
            )
        ]
    )
    project.board.queue("T-1")
    buggy = "#pragma once\ninline int clamp(int v, int lo, int hi) { return v < lo ? lo : v; }\n"
    fixed = (
        "#pragma once\ninline int clamp(int v, int lo, int hi) "
        "{ return v < lo ? lo : (v > hi ? hi : v); }\n"
    )
    test_v1 = '#include "../src/clamp.hpp"\nint main() { return clamp(-5, 0, 10) == 0 ? 0 : 1; }\n'
    test_v2 = (
        '#include "../src/clamp.hpp"\nint main() {\n'
        "  if (clamp(-5, 0, 10) != 0) return 1;\n  if (clamp(50, 0, 10) != 10) return 2;\n"
        "  return 0;\n}\n"
    )
    agents = ScriptedAgents()
    agents.add(
        "implementer",
        "T-1",
        call("write_file", path="src/clamp.hpp", content=buggy),
        call("write_file", path="tests/clamp_test.cpp", content=test_v1),
        call(
            "edit_shared_file",
            path="CMakeLists.txt",
            op="append",
            text="add_executable(clamp_test tests/clamp_test.cpp)\n"
            "add_test(NAME clamp_test COMMAND clamp_test)\n",
        ),
        finish(summary="clamp"),
    )
    agents.add(
        "reviewer",
        "T-1",
        finish(
            verdict="changes_requested",
            summary="upper bound ignored",
            findings=[
                {
                    "id": "F-1",
                    "kind": "bug",
                    "severity": "high",
                    "file": "src/clamp.hpp",
                    "detail": "hi is never applied",
                    "repro": "clamp(50, 0, 10) returns 50",
                }
            ],
        ),
    )
    agents.add(
        "fixer",
        "T-1",
        call("write_file", path="tests/clamp_test.cpp", content=test_v2),
        call(
            "tdd_submit_red",
            finding_id="F-1",
            test_id="clamp_test",
            test_files=["tests/clamp_test.cpp"],
        ),
        call("write_file", path="src/clamp.hpp", content=fixed),
        call("tdd_submit_green", finding_id="F-1"),
        finish(summary="apply hi", addressed=[{"finding_id": "F-1", "resolution": "fixed"}]),
    )
    agents.add("reviewer", "T-1", finish(verdict="approve", summary="ok"))

    await orchestrator(project, agents).run(timeout_s=300)

    assert agents.unused() == {}
    assert project.board.get("T-1").status is TicketStatus.DONE, project.board.events_for("T-1")
    assert "add_test(NAME clamp_test" in (project.root / "CMakeLists.txt").read_text()
    assert "v > hi" in (project.root / "src/clamp.hpp").read_text()
    # Build outputs never got committed.
    assert "build/" not in run_git(project.root, "log", "--name-only", "--format=", "main")
