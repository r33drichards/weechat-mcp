# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted workflow_run publisher. Treat PR artifacts as data, never as code.

Only run this default-branch script with the publisher job's short-lived token.
The source image is copied by skopeo, not loaded into or executed by Docker.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlencode

WORKFLOW = ".github/workflows/ci.yml"
MARKER = "<!-- managed-js-preview:v1 -->"
RUN_MARKER = re.compile(r"<!-- managed-js-preview-run:(\d+):(\d+) -->")
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
MAX_ARCHIVE_BYTES = 4 * 1024**3


class SkipPreview(Exception):
    """The run no longer represents an eligible current, open same-repo PR."""


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def positive_id(value):
    require(type(value) is int and value > 0, "Invalid GitHub identifier")
    return value


class GitHub:
    """Use the installed GitHub CLI for authenticated API calls and redirects."""

    def __init__(self, repository):
        require(
            re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository),
            "Invalid repository",
        )
        self.repository = repository

    def command(self, route, method="GET"):
        return [
            "gh",
            "api",
            "--hostname",
            "github.com",
            "--method",
            method,
            f"repos/{self.repository}" + (f"/{route}" if route else ""),
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
        ]

    def request(self, route, method="GET", body=None):
        command = self.command(route, method)
        if body is not None:
            command += ["--input", "-"]
        result = subprocess.run(
            command,
            input=None if body is None else json.dumps(body),
            text=True,
            capture_output=True,
            check=True,
            timeout=60,
        )
        return json.loads(result.stdout)

    def pages(self, route, key=None):
        values = []
        for page in range(1, 101):
            separator = "&" if "?" in route else "?"
            result = self.request(f"{route}{separator}per_page=100&page={page}")
            batch = result if key is None else result[key]
            values.extend(batch)
            if len(batch) < 100:
                return values
        raise ValueError("Too many API pages; refusing incomplete provenance")

    def download(self, artifact_id, destination):
        # gh handles the authenticated API -> signed storage redirect; the
        # destination/route never comes from artifact-controlled JSON.
        with destination.open("xb") as output:
            subprocess.run(
                self.command(f"actions/artifacts/{positive_id(artifact_id)}/zip"),
                stdout=output,
                check=True,
                timeout=300,
            )


def resolve_preview(api, event):
    require(event.get("action") == "completed", "Not a completed workflow_run event")
    expected = event["workflow_run"]
    run_id = positive_id(expected["id"])
    run = api.request(f"actions/runs/{run_id}")
    repository = api.request("")
    workflow = api.request("actions/workflows/ci.yml")
    repo_id = positive_id(repository["id"])
    require(
        repository["full_name"].lower() == api.repository.lower(), "Wrong repository"
    )
    require(event["repository"]["id"] == repo_id, "Event repository mismatch")
    require(run["repository"]["id"] == repo_id, "Run repository mismatch")
    require(workflow["path"] == WORKFLOW, "Unexpected producer workflow path")
    require(run["workflow_id"] == workflow["id"], "Unexpected producer workflow ID")
    require(run["path"] == WORKFLOW, "Unexpected run workflow path")
    for key in ("id", "workflow_id", "head_sha", "head_branch", "run_attempt"):
        if run[key] != expected[key]:
            raise SkipPreview("Superseded or mismatched workflow run")
    if (
        run["event"] != "pull_request"
        or run["status"] != "completed"
        or run["conclusion"] != "success"
    ):
        raise SkipPreview("Only successful completed pull_request runs may publish")
    if not run.get("head_repository") or run["head_repository"]["id"] != repo_id:
        raise SkipPreview("Fork previews never receive registry writes")
    require(
        expected.get("head_repository", {}).get("id") == repo_id,
        "Event head repository mismatch",
    )
    require(SHA.fullmatch(run["head_sha"]), "Invalid source SHA")
    positive_id(run["run_attempt"])
    branch = run["head_branch"]
    require(
        isinstance(branch, str) and 0 < len(branch) <= 1024, "Invalid source branch"
    )
    owner = repository["owner"]["login"]
    query = urlencode({"state": "open", "head": f"{owner}:{branch}"})
    candidates = api.pages(f"pulls?{query}")
    candidates = [pr for pr in candidates if current_pr(pr, run, repo_id)]
    if len(candidates) != 1:
        raise SkipPreview("No unique current open same-repository PR for this head")
    pr = candidates[0]
    positive_id(pr["number"])
    associated = run.get("pull_requests", [])
    if associated:
        require(
            any(item["number"] == pr["number"] for item in associated),
            "PR not associated with run",
        )
    # A newer run for this same head supersedes an older completion, even while
    # queued or failed. Never make an older result look like the latest result.
    query = urlencode(
        {"event": "pull_request", "head_sha": run["head_sha"], "branch": branch}
    )
    runs = api.pages(f"actions/workflows/ci.yml/runs?{query}", "workflow_runs")
    if any(item["id"] > run_id for item in runs):
        raise SkipPreview("A newer CI run supersedes this result")
    return run, pr, repo_id


def current_pr(pr, run, repo_id):
    head = pr.get("head", {})
    base = pr.get("base", {})
    return (
        pr.get("state") == "open"
        and not pr.get("merged", False)
        and head.get("sha") == run["head_sha"]
        and head.get("ref") == run["head_branch"]
        and (head.get("repo") or {}).get("id") == repo_id
        and (base.get("repo") or {}).get("id") == repo_id
    )


def ensure_current(api, event, initial_run, initial_pr):
    run, pr, _ = resolve_preview(api, event)
    require(
        run["id"] == initial_run["id"] and pr["number"] == initial_pr["number"],
        "PR identity changed",
    )


def select_artifact(api, run, repo_id):
    name = f"managed-js-preview-{run['id']}-{run['run_attempt']}"
    artifacts = api.pages(f"actions/runs/{run['id']}/artifacts", "artifacts")
    matches = [artifact for artifact in artifacts if artifact["name"] == name]
    require(
        len(matches) == 1,
        "Expected exactly one image artifact for this run attempt; "
        "re-run all CI jobs, not only failed jobs",
    )
    artifact = matches[0]
    positive_id(artifact["id"])
    require(not artifact["expired"], "Image artifact expired")
    require(
        0 < artifact["size_in_bytes"] <= MAX_ARCHIVE_BYTES, "Unexpected artifact size"
    )
    require(
        DIGEST.fullmatch(artifact.get("digest") or ""),
        "Missing artifact service digest",
    )
    provenance = artifact["workflow_run"]
    for key, expected in {
        "id": run["id"],
        "repository_id": repo_id,
        "head_repository_id": repo_id,
        "head_sha": run["head_sha"],
        "head_branch": run["head_branch"],
    }.items():
        require(provenance.get(key) == expected, f"Artifact provenance mismatch: {key}")
    return artifact


def unpack_image(archive, destination, artifact):
    require(
        archive.stat().st_size == artifact["size_in_bytes"], "Artifact size mismatch"
    )
    digest = hashlib.sha256()
    with archive.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    require(
        "sha256:" + digest.hexdigest() == artifact["digest"], "Artifact digest mismatch"
    )
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        require(len(members) == 1, "Artifact must contain image.tar only")
        member = members[0]
        mode = stat.S_IFMT(member.external_attr >> 16)
        require(
            member.filename == "image.tar" and mode in (0, stat.S_IFREG),
            "Unsafe artifact member",
        )
        require(
            0 < member.file_size <= MAX_ARCHIVE_BYTES,
            "Image archive too large or empty",
        )
        require(not member.flag_bits & 1, "Encrypted artifacts are not supported")
        # Do not extract arbitrary filenames, permissions, links or code into
        # the trusted checkout. The inner Docker archive is never executed.
        with bundle.open(member) as source, destination.open("xb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)


def image_tags(run, pr):
    sha = run["head_sha"]
    require(SHA.fullmatch(sha), "Invalid source SHA")
    number = positive_id(pr["number"])
    branch = run["head_branch"]
    slug = re.sub(r"[^a-z0-9_.-]+", "-", branch.lower()).strip(".-_")[:48] or "branch"
    branch_hash = hashlib.sha256(branch.encode()).hexdigest()[:12]
    return [f"sha-{sha}", f"pr-{number}-{sha}", f"branch-{slug}-{branch_hash}-{sha}"]


def copy_image(image, tags, source, directory, actor, token, before_write):
    require(
        re.fullmatch(r"ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+/managed-js", image),
        "Unexpected image destination",
    )
    auth = directory / "auth.json"
    encoded = base64.b64encode(f"{actor}:{token}".encode()).decode()
    descriptor = os.open(auth, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump({"auths": {"ghcr.io": {"auth": encoded}}}, output)
    # The registry client never gets GitHub API credentials in its environment.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in ("GH_TOKEN", "GITHUB_TOKEN")
    }
    digest_path = directory / "image.digest"
    try:
        before_write()
        subprocess.run(
            [
                "skopeo",
                "copy",
                "--authfile",
                str(auth),
                "--format",
                "v2s2",
                "--digestfile",
                str(digest_path),
                f"docker-archive:{source}:weechat-mcp:managed-js-nix",
                f"docker://{image}:{tags[0]}",
            ],
            check=True,
            timeout=600,
            env=environment,
        )
        digest = digest_path.read_text().strip()
        require(DIGEST.fullmatch(digest), "Invalid registry image digest")
        for tag in tags[1:]:
            before_write()
            subprocess.run(
                [
                    "skopeo",
                    "copy",
                    "--authfile",
                    str(auth),
                    "--preserve-digests",
                    f"docker://{image}@{digest}",
                    f"docker://{image}:{tag}",
                ],
                check=True,
                timeout=300,
                env=environment,
            )
        # Verify every published reference resolves to the exact manifest before
        # claiming success in the comment. This also catches unexpected rewrites.
        for reference in [f"{image}@{digest}", *(f"{image}:{tag}" for tag in tags)]:
            manifest = subprocess.run(
                [
                    "skopeo",
                    "inspect",
                    "--raw",
                    "--authfile",
                    str(auth),
                    f"docker://{reference}",
                ],
                check=True,
                capture_output=True,
                timeout=60,
                env=environment,
            ).stdout
            require(
                "sha256:" + hashlib.sha256(manifest).hexdigest() == digest,
                "Registry digest verification failed",
            )
        return digest
    finally:
        auth.unlink(missing_ok=True)


def preview_comment(repository, run, image, tags, digest, artifact):
    require(DIGEST.fullmatch(digest), "Invalid comment digest")
    links = "\n".join(f"- `{image}:{tag}`" for tag in tags)
    return (
        f"{MARKER}\n<!-- managed-js-preview-run:{run['id']}:{run['run_attempt']} -->\n"
        "### Managed JavaScript preview image\n\n"
        "The tested Nix image is available for **linux/amd64**. This is an unreviewed PR preview, not a release.\n\n"
        f"**Source commit:** `{run['head_sha']}`\n\n"
        f"**Image digest:** `{digest}`\n\n{links}\n\n"
        f"```sh\ndocker pull {image}@{digest}\n```\n\n"
        f"[CI run](https://github.com/{repository}/actions/runs/{run['id']}/attempts/{run['run_attempt']}) · "
        f"[Source](https://github.com/{repository}/commit/{run['head_sha']})\n\n"
        f"Artifact ID: `{artifact['id']}`; artifact SHA-256: `{artifact['digest']}`.\n\n"
        "Tags include the full source SHA; no `latest`, release, or stable tag is changed. "
        "The digest-pinned pull above identifies the exact image. GHCR package visibility is unchanged; "
        "private packages require your own authorized registry login.\n"
    )


def update_comment(api, pr, run, body, before_write):
    comments = api.pages(f"issues/{pr['number']}/comments")
    own = [
        comment
        for comment in comments
        if (
            comment.get("user", {}).get("login") == "github-actions[bot]"
            and comment.get("user", {}).get("type") == "Bot"
            and comment.get("body", "").startswith(MARKER + "\n")
        )
    ]
    require(
        len(own) <= 1, "Multiple preview comments exist; refusing to create another"
    )
    if own:
        comment = own[0]
        previous = RUN_MARKER.search(comment["body"])
        if previous and tuple(map(int, previous.groups())) > (
            run["id"],
            run["run_attempt"],
        ):
            raise SkipPreview("A newer image comment already exists")
        if comment["body"] == body:
            return
        before_write()
        api.request(
            f"issues/comments/{positive_id(comment['id'])}", "PATCH", {"body": body}
        )
    else:
        before_write()
        api.request(f"issues/{pr['number']}/comments", "POST", {"body": body})


def publish(api, event, directory, actor, token):
    run, pr, repo_id = resolve_preview(api, event)
    artifact = select_artifact(api, run, repo_id)
    archive = directory / "artifact.zip"
    source = directory / "image.tar"
    api.download(artifact["id"], archive)
    unpack_image(archive, source, artifact)
    image = f"ghcr.io/{api.repository.lower()}/managed-js"
    tags = image_tags(run, pr)

    def before_write():
        ensure_current(api, event, run, pr)

    digest = copy_image(image, tags, source, directory, actor, token, before_write)
    body = preview_comment(api.repository, run, image, tags, digest, artifact)
    update_comment(api, pr, run, body, before_write)
    print(f"Published {image}@{digest} for PR #{pr['number']}")


def main():
    require(
        os.environ.get("GITHUB_EVENT_NAME") == "workflow_run",
        "Only workflow_run is supported",
    )
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    api = GitHub(os.environ["GITHUB_REPOSITORY"])
    try:
        with tempfile.TemporaryDirectory(
            prefix="managed-js-preview-", dir=os.environ["RUNNER_TEMP"]
        ) as temporary:
            publish(
                api,
                event,
                Path(temporary),
                os.environ["GITHUB_ACTOR"],
                os.environ["GH_TOKEN"],
            )
    except SkipPreview as error:
        print(f"Preview skipped: {error}")


if __name__ == "__main__":
    main()
