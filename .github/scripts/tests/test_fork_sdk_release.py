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

"""Tests for the bounded fork SDK release gate."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).parents[1]))

import fork_sdk_release as release


class ReleaseGateTest(unittest.TestCase):

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Release test")
        self.ignore_data = (Path(__file__).parents[3] / ".gitignore").read_bytes()
        (self.root / ".gitignore").write_bytes(self.ignore_data)
        self.write(release.MODEL, "original\n")
        self.write(f"{release.PACKAGE}/pyproject.toml", "unchanged metadata\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.base_patch = patch.object(release, "BASE_SHA", self.base)
        self.base_patch.start()
        self.addCleanup(self.base_patch.stop)
        self.write(release.MODEL, "repair\n")
        self.write(release.REGRESSION, "regression\n")
        self.git("add", ".")
        self.git("commit", "-qm", "repair")
        self.sha = self.git("rev-parse", "HEAD")

    def git(self, *args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(self.root), *args], text=True
        ).strip()

    def write(self, name: str, content: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def test_accepts_backport_without_main_ancestry(self) -> None:
        self.assertEqual(
            set(release.validate_source(self.root, self.sha)),
            {release.MODEL, release.REGRESSION},
        )

    def test_accepts_reviewed_builder_documentation(self) -> None:
        path = "python/a2ui_agent/src/a2ui/builder/README.md"
        self.write(path, "Required null fields survive flattening.\n")
        self.git("add", ".")
        self.git("commit", "-qm", "document repair")
        self.assertEqual(
            set(release.validate_source(self.root, self.git("rev-parse", "HEAD"))),
            {release.MODEL, release.REGRESSION, path},
        )

    def test_rejects_unreviewed_package_documentation(self) -> None:
        self.write("python/a2ui_agent/README.md", "Unrelated package change.\n")
        self.git("add", ".")
        self.git("commit", "-qm", "unrelated documentation")
        with self.assertRaisesRegex(ValueError, "outside"):
            release.validate_source(self.root, self.git("rev-parse", "HEAD"))

    def test_rejects_symbolic_or_abbreviated_revision(self) -> None:
        for revision in ["HEAD", self.sha[:12], "-" + self.sha, self.sha + "\n"]:
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                release.validate_source(self.root, revision)

    def test_rejects_unchanged_base(self) -> None:
        with self.assertRaisesRegex(ValueError, "repair and regression"):
            release.validate_source(self.root, self.base)

    def test_rejects_unrelated_or_metadata_changes(self) -> None:
        for path in ["python/a2ui_core/source.py", f"{release.PACKAGE}/pyproject.toml"]:
            with self.subTest(path=path):
                self.write(path, "unreleased change\n")
                self.git("add", ".")
                self.git("commit", "-qm", "unrelated")
                with self.assertRaisesRegex(ValueError, "outside"):
                    release.validate_source(self.root, self.git("rev-parse", "HEAD"))

    def test_rejects_missing_base_ancestry(self) -> None:
        with patch.object(release, "BASE_SHA", self.sha):
            with self.assertRaisesRegex(ValueError, "descendant"):
                release.validate_source(self.root, self.base)

    def test_rejects_symlink_repair(self) -> None:
        (self.root / release.MODEL).unlink()
        (self.root / release.MODEL).symlink_to("elsewhere")
        self.git("add", ".")
        self.git("commit", "-qm", "symlink")
        with self.assertRaisesRegex(ValueError, "regular"):
            release.validate_source(self.root, self.git("rev-parse", "HEAD"))

    def archives(
        self,
        *,
        extra: str | None = None,
        version: str | None = None,
        dependencies: tuple[str, ...] | None = None,
        changed_payload: bool = False,
        extra_sdist: str | None = None,
        wheel_tag: str = "py3-none-any",
        gitignore: bytes | None = None,
        omit_gitignore: bool = False,
    ) -> Path:
        dist = self.root / "dist"
        dist.mkdir(exist_ok=True)
        metadata = (
            "Metadata-Version: 2.4\nName: a2ui-agent-sdk\n"
            f"Version: {version or release.VERSION}\nRequires-Python: >=3.10\n"
            + "".join(
                f"Requires-Dist: {value}\n"
                for value in dependencies or release.DEPENDENCIES
            )
            + "\n"
        )
        with zipfile.ZipFile(dist / release.WHEEL, "w") as archive:
            archive.writestr(
                "a2ui/builder/core/base_model.py",
                "tampered\n" if changed_payload else "repair\n",
            )
            for name in ["METADATA", "WHEEL", "RECORD", "entry_points.txt"]:
                wheel_content = metadata if name == "METADATA" else ""
                if name == "WHEEL":
                    wheel_content = (
                        f"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: {wheel_tag}\n"
                    )
                archive.writestr(f"{release.DIST_INFO}/{name}", wheel_content)
            if extra:
                archive.writestr(extra, "unexpected")
        with tarfile.open(dist / release.SDIST, "w:gz") as archive:
            files = {
                "PKG-INFO": metadata.encode(),
                "src/a2ui/builder/core/base_model.py": b"repair\n",
                "pyproject.toml": b"unchanged metadata\n",
                "tests/test_builder_null_serialization.py": b"regression\n",
            }
            if not omit_gitignore:
                files[".gitignore"] = (
                    self.ignore_data if gitignore is None else gitignore
                )
            if extra_sdist:
                files[extra_sdist] = b"unexpected"
            for name, content in files.items():
                info = tarfile.TarInfo(f"{release.STEM}/{name}")
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        return dist

    def test_accepts_matching_distributions(self) -> None:
        release.verify_distributions(self.root, self.sha, self.archives())

    def test_sdist_gitignore_matches_commit_not_working_tree(self) -> None:
        self.write(".gitignore", "uncommitted ignore rules\n")
        release.verify_distributions(self.root, self.sha, self.archives())

    def test_rejects_altered_sdist_gitignore(self) -> None:
        with self.assertRaisesRegex(ValueError, "sdist payload differs.*.gitignore"):
            release.verify_distributions(
                self.root, self.sha, self.archives(gitignore=b"altered\n")
            )

    def test_rejects_missing_sdist_gitignore(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing sdist payload"):
            release.verify_distributions(
                self.root, self.sha, self.archives(omit_gitignore=True)
            )

    def test_manifest_binds_verified_artifacts_to_source_and_workflow(self) -> None:
        dist = self.archives()
        with patch.object(
            sys,
            "argv",
            [
                "fork_sdk_release.py",
                "artifacts",
                "--repository",
                str(self.root),
                "--source-sha",
                self.sha,
                "--dist",
                str(dist),
                "--workflow-sha",
                "1" * 40,
                "--run-url",
                "https://github.com/markmadlangbayan/a2ui/actions/runs/1",
            ],
        ):
            release.main()
        manifest = json.loads((dist / "source-manifest.json").read_text())
        self.assertEqual(manifest["source_sha"], self.sha)
        self.assertEqual(manifest["base_sha"], self.base)
        self.assertEqual(manifest["workflow_sha"], "1" * 40)
        for line in (dist / "checksums.txt").read_text().splitlines():
            digest, name = line.split("  ")
            self.assertEqual(
                digest, hashlib.sha256((dist / name).read_bytes()).hexdigest()
            )

    def test_rejects_mismatched_wheel_tag(self) -> None:
        with self.assertRaisesRegex(ValueError, "wheel metadata"):
            release.verify_distributions(
                self.root, self.sha, self.archives(wheel_tag="cp313-none-any")
            )

    def test_rejects_extra_sdist_payload(self) -> None:
        for name in ["../escape", "src/a2ui/core/copied.py", ".env"]:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "payload"):
                release.verify_distributions(
                    self.root, self.sha, self.archives(extra_sdist=name)
                )

    def test_rejects_wrong_metadata_version(self) -> None:
        with self.assertRaisesRegex(ValueError, "metadata"):
            release.verify_distributions(
                self.root, self.sha, self.archives(version="0.11.0")
            )

    def test_rejects_changed_dependency_bounds(self) -> None:
        with self.assertRaisesRegex(ValueError, "dependencies"):
            release.verify_distributions(
                self.root, self.sha, self.archives(dependencies=("a2ui-core>=0.7",))
            )

    def test_rejects_extra_wheel_payload(self) -> None:
        for name in ["a2ui/core/copied.py", "other.py", "../escaped", "a2ui/extra.py"]:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "payload"):
                release.verify_distributions(
                    self.root, self.sha, self.archives(extra=name)
                )

    def test_rejects_payload_not_matching_source(self) -> None:
        with self.assertRaisesRegex(ValueError, "payload"):
            release.verify_distributions(
                self.root, self.sha, self.archives(changed_payload=True)
            )

    def test_rejects_missing_wheel_source_file(self) -> None:
        self.write(f"{release.PACKAGE}/src/a2ui/required.py", "required\n")
        self.git("add", ".")
        self.git("commit", "-qm", "additional source")
        with self.assertRaisesRegex(ValueError, "payload"):
            release.verify_distributions(
                self.root, self.git("rev-parse", "HEAD"), self.archives()
            )

    def test_rejects_unexpected_distribution(self) -> None:
        dist = self.archives()
        (dist / "a2ui_core-0.6.0.whl").write_text("extra")
        with self.assertRaisesRegex(ValueError, "exactly"):
            release.verify_distributions(self.root, self.sha, dist)


if __name__ == "__main__":
    unittest.main()
