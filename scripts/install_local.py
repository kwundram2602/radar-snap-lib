"""Install radar-snap-lib as an editable dependency into another uv project."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _uv_add(target_project: Path, package_path: Path) -> None:
    cmd = [
        "uv",
        "--project",
        str(target_project),
        "add",
        "--editable",
        str(package_path),
    ]
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        subprocess.check_call(
            ["uv", "add", "--editable", str(package_path)],
            cwd=target_project,
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install radar-snap-lib as editable into another uv project."
    )
    parser.add_argument(
        "--project",
        default=".",
        help="Target project directory containing pyproject.toml",
    )
    parser.add_argument(
        "--venv",
        dest="project",
        help="Alias for --project (target project path)",
    )
    parser.add_argument(
        "--package-path",
        default=str(_repo_root()),
        help="Path to the package repo (defaults to this repo)",
    )
    args = parser.parse_args()

    target_project = Path(args.project).resolve()
    package_path = Path(args.package_path).resolve()
    _uv_add(target_project, package_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
