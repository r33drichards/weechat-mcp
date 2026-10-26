<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Managed JavaScript PR image previews

The preview publisher is standalone CI infrastructure intended for `main`.
Landing it does **not** merge the experimental V8 runtime, change the default
Docker image, or deploy anything. The optional image producer runs only on source
branches containing both `flake.nix` and `docker_managed_js_test.py`.

## Build, test, then publish

For an eligible PR, the `Python checks` workflow checks out the **exact PR head**
for its managed-image job, builds the Nix `managed-js-image`, and smoke-tests that
archive without rebuilding. Successful same-repository PR runs upload those same
bytes for the separate publisher. All jobs in `Python checks` must succeed.
Forks can build and test, but do not upload publication artifacts, receive
registry credentials, publish images, or receive publisher comments.

Ordinary branches without the managed-image inputs skip the optional producer;
the publisher skips these non-image runs rather than reporting a missing image.
For an eligible image-producing run, however, a missing, expired, mismatched, or
unverifiable artifact **fails publication**. Absence is not silently treated as a
successful preview. Artifacts are retained for three days.

The `workflow_run` publisher uses only its default-branch event commit, never
checks out PR code, and copies the archive with `skopeo` without running it. It
verifies the producer workflow, repository, current open PR/head, run attempt,
artifact provenance, and artifact ZIP digest before publication. Only this
separate job has registry/comment write permissions through its short-lived
`GITHUB_TOKEN`; no PAT, deployment credential, or persistent registry login is
needed.

## Images and PR comments

The registry destination is `ghcr.io/r33drichards/weechat-mcp/managed-js`, for
**linux/amd64**. Each image has three SHA-versioned tag forms:

- `sha-<full-source-sha>`
- `pr-<number>-<full-source-sha>`
- `branch-<sanitized-branch>-<12-character-branch-hash>-<full-source-sha>`

The original branch name is hashed before sanitization to avoid collisions.
There are no moving `latest`, `stable`, release, or branch-alias tags. A rerun can
republish the same source tags, so use the **digest-pinned `docker pull` command**
in the bot comment when you need the exact published image.

One bot-owned comment per PR is created or updated with the tags, source SHA,
verified registry digest, and originating run/artifact. Human comments are not
edited. Closed PRs, advanced heads, superseded runs, and old attempts are skipped;
heads are rechecked before registry writes and comment updates. A head changing
during upload can leave an older SHA-versioned image in GHCR, but cannot advance
a moving alias or replace the current PR's comment.

## Activation and reruns

GitHub activates this publisher only after its workflow and helper land on the
default branch. A PR cannot activate its own new publisher. After the standalone
publisher merges into `main`, merge `main` into the managed-runtime feature branch
to bring in the producer and its checks; **no rebase is required**. Then update
that same-repository PR or rerun **all CI jobs**, and verify the publisher result.
Merging this infrastructure alone is not proof that an image has been published.

Do not rerun only failed jobs when recovering publication: that can retain an
image from an older attempt, which is deliberately rejected. Missing or expired
artifacts also require rerunning all jobs rather than weakening provenance checks.

GHCR may initially create the package as private. The publisher does not change
visibility or access; private pulls require an existing authorized GHCR login.
A denied push stays a failed publication. Preview images contain unreviewed PR
code: review before use and do not attach production data or credentials.

Offline publisher regression tests (no Nix, Docker, registry, or network):

```sh
python3 -m unittest test_preview_publisher -v
```
