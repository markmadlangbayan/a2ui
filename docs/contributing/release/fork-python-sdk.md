# Fork-only Python SDK backport

The [fork workflow](../../../.github/workflows/release-fork-python-sdk.yml)
builds `a2ui-agent-sdk==0.11.0+haven.1` and publishes GitHub prerelease assets
only in `markmadlangbayan/a2ui`. It has no PyPI, pub.dev, Google Cloud, or upstream
publication step. Do not dispatch `release-pypi.yml` from this fork: even its
dry run stages artifacts in Google's OSS infrastructure.

## Review the source

The release workflow must first be reviewed and merged into the fork's `main`.
The SDK repair is reviewed separately on a release branch based on
`3295e67b5f4820639aa88c578a64af2196d0b752`, the `0.11.0` SDK release commit.
Do not merge unrelated `main` changes into that branch.

The [source gate](../../../.github/scripts/fork_sdk_release.py) requires a full
40-character lowercase commit SHA descended from that exact base. It requires
changes to both `python/a2ui_agent/src/a2ui/builder/core/base_model.py` and
`python/a2ui_agent/tests/test_builder_null_serialization.py`. Only those files and
`python/a2ui_agent/src/a2ui/builder/README.md` and
`python/a2ui_agent/CHANGELOG.md` may differ. The package-root `README.md`,
metadata, and core changes are rejected. The source does not need to be merged
into `main`.

Run the full SDK test suite on the reviewed backport before approving publication.
The workflow runs only the backport's null-serialization regression file against
an installed wheel, outside the source tree, with published `a2ui-core==0.6.0`
and `pydantic==2.13.5` on Python 3.13. That regression file must be self-contained
and cover nested serialization as well as omission of null optional fields.

## Dry run, then approve publication

Set `SOURCE_SHA` to the actual reviewed backport commit, not a branch or tag.
There is deliberately no default SHA. Dispatch only from the fork's `main`:

```bash
: "${SOURCE_SHA:?Set the reviewed 40-character backport commit first}"
gh workflow run release-fork-python-sdk.yml --repo markmadlangbayan/a2ui \
  --ref main -f source_sha="$SOURCE_SHA" -f dry_run=true
```

Inspect the matching run and download its `fork-python-sdk` artifact. Confirm the
source SHA, changed-file list, package version, checksums, and installed dependency
versions. A dry run builds, tests, and uploads an Actions artifact; it creates no
Git tag, GitHub release, or GitHub attestation.

After review and explicit publication approval, dispatch the same source SHA with
`-f dry_run=false`. Build, provenance, and publication belong to this workflow;
do not build or publish a substitute locally. The version comes from the supported
`SETUPTOOLS_SCM_PRETEND_VERSION` override used by hatch-vcs, without editing SDK code
or creating a local tag.

## Release contents and verification

The prerelease tag `python/a2ui-agent-sdk/v0.11.0+haven.1` points at the exact
source SHA. Assets include the wheel, source distribution, `checksums.txt`,
`source-manifest.json`, installed dependency versions, and a GitHub provenance
bundle. The manifest records the separate workflow commit, run, base and source.
Payload files must match the pinned source bytes. The source distribution also
requires Hatchling's copied `.gitignore`, matched against the repository-root
`.gitignore` at that same commit.
The GitHub attestation identifies the `main` workflow commit; it also attests the
manifest binding the artifacts to the backport, rather than claiming the workflow
was run from that backport commit.

```bash
sha256sum --strict --check checksums.txt
gh attestation verify a2ui_agent_sdk-0.11.0+haven.1-py3-none-any.whl \
  --repo markmadlangbayan/a2ui
```

The source build has read-only repository permission. A separate publish job gets
only repository contents, attestation, and OIDC write permissions and never
executes source code. It uses the ephemeral `GITHUB_TOKEN` and GitHub's attestation
identity; no repository secret or cloud identity is needed.

An existing tag or release blocks another run, including a dry run. The workflow
never updates or deletes either. If publication fails after creating the tag,
stop and inspect the failed run; retries deliberately refuse to overwrite it.
Any recovery or new version needs a separate maintainer decision and reviewed
workflow change.
