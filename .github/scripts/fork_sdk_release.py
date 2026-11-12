# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Verify the single 0.11.0+haven.1 SDK backport without importing its code."""

from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
import zipfile

BASE_SHA = "3295e67b5f4820639aa88c578a64af2196d0b752"
VERSION = "0.11.0+haven.1"
PACKAGE = "python/a2ui_agent"
MODEL = f"{PACKAGE}/src/a2ui/builder/core/base_model.py"
REGRESSION = f"{PACKAGE}/tests/test_builder_null_serialization.py"
ALLOWED = {MODEL, REGRESSION, f"{PACKAGE}/README.md", f"{PACKAGE}/CHANGELOG.md"}
STEM = f"a2ui_agent_sdk-{VERSION}"
DIST_INFO = f"{STEM}.dist-info"
WHEEL = f"{STEM}-py3-none-any.whl"
SDIST = f"{STEM}.tar.gz"
DEPENDENCIES = (
    "a2ui-core>=0.6.0,<0.7.0",
    "antlr4-python3-runtime>=4.13.0,<4.14.0",
    "jsonschema>=4.0.0",
    "pydantic>=2.11.0",
    "pyyaml>=6.0",
)


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args])


def validate_source(root: Path, sha: str) -> list[str]:
    if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise ValueError("source_sha must be an immutable lowercase 40-hex commit")
    if git(root, "rev-parse", f"{sha}^{{commit}}").decode().strip() != sha:
        raise ValueError("source_sha must name a commit, not a tag object")
    result = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", BASE_SHA, sha],
        check=False,
    )
    if result.returncode:
        raise ValueError("source_sha must be a descendant of the fixed SDK base")
    changed = git(root, "diff", "--name-only", "--no-renames", "-z", BASE_SHA, sha)
    paths = sorted(path.decode() for path in changed.split(b"\0") if path)
    if set(paths) - ALLOWED:
        raise ValueError("source changes are outside the SDK repair allowlist")
    if not {MODEL, REGRESSION}.issubset(paths):
        raise ValueError("source must contain both the repair and regression test")
    for path in paths:
        entry = git(root, "ls-tree", sha, "--", path)
        if not entry.startswith(b"100644 blob "):
            raise ValueError("repair paths must remain regular non-executable files")
    return paths


def package_files(root: Path, sha: str) -> dict[str, bytes]:
    entries = git(root, "ls-tree", "-rz", sha, "--", PACKAGE).split(b"\0")
    files = {}
    for entry in filter(None, entries):
        mode, _, rest = entry.split(b" ", 2)
        _, name = rest.split(b"\t", 1)
        if mode not in {b"100644", b"100755"}:
            raise ValueError("package must contain only regular files")
        path = name.decode()
        files[path.removeprefix(PACKAGE + "/")] = git(root, "show", f"{sha}:{path}")
    return files


def normalized_requirement(value: str) -> tuple[str, tuple[str, ...]]:
    match = re.fullmatch(
        r"([a-z0-9-]+)(.*)", value.lower().replace(" ", "").replace("_", "-")
    )
    if match is None:
        raise ValueError("invalid dependencies")
    return match[1], tuple(sorted(match[2].split(",")))


def verify_metadata(content: bytes) -> None:
    metadata = BytesParser().parsebytes(content)
    for name, value in {
        "Name": "a2ui-agent-sdk",
        "Version": VERSION,
        "Requires-Python": ">=3.10",
    }.items():
        if metadata.get_all(name) != [value]:
            raise ValueError(f"incorrect distribution metadata: {name}")
    actual = metadata.get_all("Requires-Dist", [])
    if sorted(map(normalized_requirement, actual)) != sorted(
        map(normalized_requirement, DEPENDENCIES)
    ):
        raise ValueError("distribution dependencies changed")


def verify_distributions(root: Path, sha: str, dist: Path) -> None:
    if {path.name for path in dist.iterdir()} != {WHEEL, SDIST}:
        raise ValueError("expected exactly one SDK wheel and one SDK sdist")
    source = package_files(root, sha)
    wheel_source = {
        path.removeprefix("src/"): content
        for path, content in source.items()
        if path.startswith("src/a2ui/")
    }
    with zipfile.ZipFile(dist / WHEEL) as archive:
        names = archive.namelist()
        metadata_names = {
            f"{DIST_INFO}/{name}"
            for name in ("METADATA", "WHEEL", "RECORD", "entry_points.txt")
        }
        if (
            len(names) != len(set(names))
            or set(names) != set(wheel_source) | metadata_names
        ):
            raise ValueError("unexpected or missing wheel payload")
        for name, content in wheel_source.items():
            if archive.read(name) != content:
                raise ValueError(f"wheel payload differs from source: {name}")
        verify_metadata(archive.read(f"{DIST_INFO}/METADATA"))
        wheel_metadata = BytesParser().parsebytes(archive.read(f"{DIST_INFO}/WHEEL"))
        if wheel_metadata.get_all("Tag") != ["py3-none-any"] or wheel_metadata.get_all(
            "Root-Is-Purelib"
        ) != ["true"]:
            raise ValueError("incorrect wheel metadata")
    # Hatchling carries the repository-root ignore file into the package sdist.
    sdist_source = {**source, ".gitignore": git(root, "show", f"{sha}:.gitignore")}
    with tarfile.open(dist / SDIST, "r:gz") as archive:
        seen = set()
        for member in archive.getmembers():
            if member.isdir():
                continue
            if not member.isfile() or not member.name.startswith(STEM + "/"):
                raise ValueError("unexpected sdist payload")
            name = member.name.removeprefix(STEM + "/")
            if name in seen:
                raise ValueError("duplicate sdist payload")
            seen.add(name)
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("missing sdist payload")
            content = stream.read()
            if name == "PKG-INFO":
                verify_metadata(content)
            elif name not in sdist_source or content != sdist_source[name]:
                raise ValueError(f"sdist payload differs from source: {name}")
        required = {
            ".gitignore",
            "PKG-INFO",
            "pyproject.toml",
            REGRESSION.removeprefix(PACKAGE + "/"),
            *("src/" + path for path in wheel_source),
        }
        if not required.issubset(seen):
            raise ValueError("missing sdist payload")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["source", "artifacts"])
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--dist", type=Path)
    parser.add_argument("--workflow-sha")
    parser.add_argument("--run-url")
    args = parser.parse_args()
    changed = validate_source(args.repository, args.source_sha)
    if args.command == "source":
        print("\n".join(changed))
        return
    if args.dist is None or not args.workflow_sha or not args.run_url:
        parser.error("artifacts requires --dist, --workflow-sha and --run-url")
    verify_distributions(args.repository, args.source_sha, args.dist)
    digests = {
        name: hashlib.sha256((args.dist / name).read_bytes()).hexdigest()
        for name in (WHEEL, SDIST)
    }
    manifest = {
        "repository": "markmadlangbayan/a2ui",
        "base_sha": BASE_SHA,
        "source_sha": args.source_sha,
        "workflow_sha": args.workflow_sha,
        "run_url": args.run_url,
        "version": VERSION,
        "core_version": "0.6.0",
        "pydantic_version": "2.13.5",
        "changed_files": changed,
        "sha256": digests,
    }
    manifest_path = args.dist / "source-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    digests[manifest_path.name] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (args.dist / "checksums.txt").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(digests.items()))
    )


if __name__ == "__main__":
    main()
