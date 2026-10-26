# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline preview publisher policy, provenance, tagging and comment regressions."""

import copy
import hashlib
import io
import json
import stat
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from tools import publish_managed_js_preview as publisher

REPOSITORY = "r33drichards/weechat-mcp"
SOURCE_SHA = "a" * 40
MANIFEST = b'{"schemaVersion":2,"fixture":true}'
IMAGE_DIGEST = "sha256:" + hashlib.sha256(MANIFEST).hexdigest()
ROOT = Path(__file__).resolve().parent


class FakeGitHub:
    repository = REPOSITORY

    def __init__(self):
        self.repo: dict[str, Any] = {
            "id": 10,
            "full_name": REPOSITORY,
            "owner": {"login": "r33drichards"},
        }
        self.workflow: dict[str, Any] = {"id": 20, "path": publisher.WORKFLOW}
        self.ci_run: dict[str, Any] = {
            "id": 30,
            "workflow_id": 20,
            "path": publisher.WORKFLOW,
            "run_attempt": 1,
            "head_sha": SOURCE_SHA,
            "head_branch": "feature/preview",
            "event": "pull_request",
            "status": "completed",
            "conclusion": "success",
            "repository": {"id": 10},
            "head_repository": {"id": 10},
            "pull_requests": [{"number": 14}],
        }
        self.event: dict[str, Any] = {
            "action": "completed",
            "repository": {"id": 10},
            "workflow_run": copy.deepcopy(self.ci_run),
        }
        self.pr: dict[str, Any] = {
            "number": 14,
            "state": "open",
            "merged": False,
            "head": {"repo": {"id": 10}, "sha": SOURCE_SHA, "ref": "feature/preview"},
            "base": {"repo": {"id": 10}},
        }
        self.prs = [self.pr]
        self.runs = [self.ci_run]
        self.comments = []
        self.writes = []
        self.downloads = []
        self.archive = self.zip_bytes()
        self.artifact: dict[str, Any] = {
            "id": 40,
            "name": "managed-js-preview-30-1",
            "expired": False,
            "size_in_bytes": len(self.archive),
            "digest": "sha256:" + hashlib.sha256(self.archive).hexdigest(),
            "workflow_run": {
                "id": 30,
                "repository_id": 10,
                "head_repository_id": 10,
                "head_sha": SOURCE_SHA,
                "head_branch": "feature/preview",
            },
        }
        self.artifacts = [self.artifact]

    @staticmethod
    def zip_bytes(name="image.tar", mode=stat.S_IFREG | 0o644, extra=False):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as bundle:
            member = zipfile.ZipInfo(name)
            member.external_attr = mode << 16
            bundle.writestr(member, b"not executed: image archive bytes")
            if extra:
                bundle.writestr("publisher.py", b"raise RuntimeError('never execute')")
        return output.getvalue()

    def request(self, route, method="GET", body=None):
        if method != "GET":
            assert body is not None
            self.writes.append((route, method, body))
            if method == "POST":
                self.comments.append(
                    {
                        "id": 50,
                        "body": body["body"],
                        "user": {"login": "github-actions[bot]", "type": "Bot"},
                    }
                )
            elif method == "PATCH":
                self.comments[0]["body"] = body["body"]
            return {}
        return copy.deepcopy(
            {
                "": self.repo,
                "actions/workflows/ci.yml": self.workflow,
                "actions/runs/30": self.ci_run,
            }[route]
        )

    def pages(self, route, key=None):
        if route.startswith("pulls?"):
            return copy.deepcopy(self.prs)
        if route.startswith("actions/workflows/ci.yml/runs?"):
            return copy.deepcopy(self.runs)
        if route == "actions/runs/30/artifacts":
            return copy.deepcopy(self.artifacts)
        if route == "issues/14/comments":
            return copy.deepcopy(self.comments)
        raise AssertionError(route)

    def download(self, artifact_id, destination):
        self.downloads.append(artifact_id)
        destination.write_bytes(self.archive)


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()

    def resolve(self):
        return publisher.resolve_preview(self.api, self.api.event)

    def test_current_same_repository_pr_accepted(self):
        run, pr, repo_id = self.resolve()
        self.assertEqual((run["id"], pr["number"], repo_id), (30, 14, 10))

    def test_missing_run_pr_list_uses_server_verified_current_head(self):
        self.api.ci_run["pull_requests"] = []
        self.assertEqual(self.resolve()[1]["number"], 14)

    def test_unrelated_workflow_or_repository_rejected(self):
        for object_name, field, value in (
            ("ci_run", "workflow_id", 21),
            ("ci_run", "path", ".github/workflows/fake.yml"),
            ("repo", "full_name", "other/repository"),
            ("workflow", "path", "other.yml"),
            ("ci_run", "repository", {"id": 99}),
        ):
            with self.subTest(object_name=object_name, field=field):
                self.api = FakeGitHub()
                getattr(self.api, object_name)[field] = value
                with self.assertRaises(ValueError):
                    self.resolve()

    def test_wrong_event_repository_rejected(self):
        self.api.event["repository"]["id"] = 99
        with self.assertRaises(ValueError):
            self.resolve()

    def test_fork_run_skipped(self):
        self.api.ci_run["head_repository"] = {"id": 99}
        with self.assertRaises(publisher.SkipPreview):
            self.resolve()

    def test_unsuccessful_and_non_pr_runs_skipped(self):
        for key, value in (
            ("event", "push"),
            ("event", "pull_request_target"),
            ("status", "in_progress"),
            ("conclusion", "failure"),
            ("conclusion", "cancelled"),
        ):
            with self.subTest(key=key, value=value):
                self.api = FakeGitHub()
                self.api.ci_run[key] = value
                with self.assertRaises(publisher.SkipPreview):
                    self.resolve()

    def test_old_attempt_and_changed_run_head_skipped(self):
        for key, value in (
            ("run_attempt", 2),
            ("head_sha", "b" * 40),
            ("head_branch", "different"),
        ):
            with self.subTest(key=key):
                self.api = FakeGitHub()
                self.api.ci_run[key] = value
                with self.assertRaises(publisher.SkipPreview):
                    self.resolve()

    def test_closed_merged_forked_or_advanced_pr_skipped(self):
        changes = [
            {"state": "closed"},
            {"merged": True},
            {"head": {"repo": {"id": 10}, "sha": "b" * 40, "ref": "feature/preview"}},
            {"head": {"repo": {"id": 99}, "sha": SOURCE_SHA, "ref": "feature/preview"}},
            {"base": {"repo": {"id": 99}}},
        ]
        for change in changes:
            with self.subTest(change=change):
                self.api = FakeGitHub()
                self.api.pr.update(change)
                with self.assertRaises(publisher.SkipPreview):
                    self.resolve()

    def test_ambiguous_or_unassociated_pr_rejected(self):
        self.api.prs.append(copy.deepcopy(self.api.pr))
        with self.assertRaises(publisher.SkipPreview):
            self.resolve()
        self.api.prs.pop()
        self.api.ci_run["pull_requests"] = [{"number": 12}]
        with self.assertRaises(ValueError):
            self.resolve()

    def test_newer_queued_run_supersedes_completed_old_run(self):
        self.api.runs.append({"id": 31, "status": "queued"})
        with self.assertRaises(publisher.SkipPreview):
            self.resolve()

    def test_artifact_requires_exact_attempt_name_and_unique_result(self):
        for artifacts in (
            [],
            [self.api.artifact] * 2,
            [{**self.api.artifact, "name": "managed-js-preview-30-2"}],
        ):
            with self.subTest(artifacts=artifacts):
                self.api.artifacts = artifacts
                with self.assertRaises(ValueError):
                    publisher.select_artifact(self.api, self.api.ci_run, 10)

    def test_artifact_provenance_all_fields_fail_closed(self):
        for key in self.api.artifact["workflow_run"]:
            with self.subTest(key=key):
                self.api = FakeGitHub()
                self.api.artifact["workflow_run"][key] = "wrong"
                with self.assertRaises(ValueError):
                    publisher.select_artifact(self.api, self.api.ci_run, 10)

    def test_missing_digest_expired_and_oversized_artifacts_rejected(self):
        for key, value in (
            ("digest", None),
            ("expired", True),
            ("size_in_bytes", publisher.MAX_ARCHIVE_BYTES + 1),
        ):
            with self.subTest(key=key):
                self.api = FakeGitHub()
                self.api.artifact[key] = value
                with self.assertRaises(ValueError):
                    publisher.select_artifact(self.api, self.api.ci_run, 10)


class GitHubClientTests(unittest.TestCase):
    def test_repository_lookup_has_no_trailing_slash(self):
        api = publisher.GitHub(REPOSITORY)
        self.assertIn(f"repos/{REPOSITORY}", api.command(""))
        self.assertNotIn(f"repos/{REPOSITORY}/", api.command(""))
        self.assertIn(
            f"repos/{REPOSITORY}/actions/runs/30", api.command("actions/runs/30")
        )

    def test_pagination_preserves_existing_query_and_collects_later_results(self):
        api = publisher.GitHub(REPOSITORY)
        with patch.object(
            api, "request", side_effect=[{"artifacts": [1] * 100}, {"artifacts": [2]}]
        ) as request:
            self.assertEqual(
                len(api.pages("actions/runs/30/artifacts?name=image", "artifacts")), 101
            )
            self.assertIn("&per_page=100&page=2", request.call_args.args[0])

    def test_mutation_body_is_stdin_data_not_a_shell_argument(self):
        api = publisher.GitHub(REPOSITORY)
        with patch.object(
            publisher.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, stdout="{}"),
        ) as command:
            body = {"body": "untrusted ' ; $(this is not shell code)"}
            api.request("issues/14/comments", "POST", body)
            self.assertEqual(json.loads(command.call_args.kwargs["input"]), body)
            self.assertNotIn("shell", command.call_args.kwargs)
            self.assertNotIn(body["body"], command.call_args.args[0])


class ArchiveTests(unittest.TestCase):
    def unpack(self, content, digest=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "artifact.zip"
            archive.write_bytes(content)
            artifact = {
                "size_in_bytes": len(content),
                "digest": digest or "sha256:" + hashlib.sha256(content).hexdigest(),
            }
            publisher.unpack_image(archive, root / "image.tar", artifact)
            return (root / "image.tar").read_bytes()

    def test_only_regular_image_bytes_written(self):
        self.assertIn(b"not executed", self.unpack(FakeGitHub.zip_bytes()))

    def test_modified_archive_rejected_by_service_digest(self):
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            self.unpack(FakeGitHub.zip_bytes(), "sha256:" + "0" * 64)

    def test_traversal_symlink_extra_and_directory_members_rejected(self):
        for options in (
            {"name": "../image.tar"},
            {"name": "/image.tar"},
            {"mode": stat.S_IFLNK | 0o777},
            {"extra": True},
            {"mode": stat.S_IFDIR | 0o755},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.unpack(FakeGitHub.zip_bytes(**options))


class TagsAndCommentTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()

    def body(self):
        return publisher.preview_comment(
            REPOSITORY,
            self.api.ci_run,
            "ghcr.io/" + REPOSITORY + "/managed-js",
            publisher.image_tags(self.api.ci_run, self.api.pr),
            IMAGE_DIGEST,
            self.api.artifact,
        )

    def test_branch_tags_are_safe_bounded_and_collision_resistant(self):
        tags_seen = set()
        for branch in (
            "feature/preview",
            "feature-preview",
            "Feature/Preview",
            "release/'$(touch /tmp/no)'",
            "ünicode/试验",
            "-._",
            "a" * 1024,
            "a" * 1023 + "b",
            "latest",
            "main",
        ):
            run = {**self.api.ci_run, "head_branch": branch}
            tags = publisher.image_tags(run, self.api.pr)
            self.assertEqual(len(tags), 3)
            for tag in tags:
                self.assertRegex(tag, r"^[a-z0-9][a-z0-9_.-]{0,127}$")
                self.assertTrue(tag.endswith(SOURCE_SHA))
                self.assertNotIn(tag, ("latest", "stable", "main"))
            self.assertNotIn(tags[2], tags_seen)
            tags_seen.add(tags[2])
        self.assertTrue(
            publisher.image_tags(self.api.ci_run, self.api.pr)[2].startswith(
                "branch-feature-preview-"
            )
        )

    def test_exact_digest_source_and_all_tags_in_comment(self):
        body = self.body()
        self.assertIn(
            f"docker pull ghcr.io/{REPOSITORY}/managed-js@{IMAGE_DIGEST}", body
        )
        self.assertIn(SOURCE_SHA, body)
        for tag in publisher.image_tags(self.api.ci_run, self.api.pr):
            self.assertIn(tag, body)
        self.assertIn("unreviewed PR preview", body)
        self.assertIn("private packages", body)

    def test_one_bot_comment_created_updated_and_idempotent(self):
        check = Mock()
        publisher.update_comment(
            self.api, self.api.pr, self.api.ci_run, self.body(), check
        )
        publisher.update_comment(
            self.api, self.api.pr, self.api.ci_run, self.body(), check
        )
        publisher.update_comment(
            self.api, self.api.pr, self.api.ci_run, self.body() + "updated", check
        )
        self.assertEqual(
            [method for _, method, _ in self.api.writes], ["POST", "PATCH"]
        )
        self.assertEqual(len(self.api.comments), 1)
        self.assertEqual(check.call_count, 2)

    def test_marker_in_human_or_other_bot_comment_is_not_owned(self):
        self.api.comments = [
            {"id": 49, "body": self.body(), "user": {"login": "human", "type": "User"}}
        ]
        publisher.update_comment(
            self.api, self.api.pr, self.api.ci_run, self.body(), Mock()
        )
        self.assertEqual(self.api.writes[0][1], "POST")

    def test_newer_comment_is_not_overwritten(self):
        body = self.body().replace("preview-run:30:1", "preview-run:31:1")
        self.api.comments = [
            {
                "id": 49,
                "body": body,
                "user": {"login": "github-actions[bot]", "type": "Bot"},
            }
        ]
        with self.assertRaises(publisher.SkipPreview):
            publisher.update_comment(
                self.api, self.api.pr, self.api.ci_run, self.body(), Mock()
            )
        self.assertFalse(self.api.writes)

    def test_closed_pr_rechecked_before_comment_mutation(self):
        self.api.pr["state"] = "closed"
        with self.assertRaises(publisher.SkipPreview):
            publisher.update_comment(
                self.api,
                self.api.pr,
                self.api.ci_run,
                self.body(),
                lambda: publisher.ensure_current(
                    self.api, self.api.event, self.api.ci_run, self.api.pr
                ),
            )
        self.assertFalse(self.api.writes)


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()

    def test_mocked_publish_reuses_downloaded_image_and_comments_once(self):
        def copy_image(image, tags, source, directory, actor, token, before_write):
            before_write()
            self.assertIn(b"not executed", source.read_bytes())
            self.assertEqual(image, f"ghcr.io/{REPOSITORY}/managed-js")
            return IMAGE_DIGEST

        with patch.object(publisher, "copy_image", side_effect=copy_image) as copier:
            for _ in range(2):
                with tempfile.TemporaryDirectory() as temporary:
                    publisher.publish(
                        self.api,
                        self.api.event,
                        Path(temporary),
                        "actor",
                        "synthetic-token",
                    )
        self.assertEqual(copier.call_count, 2)
        self.assertEqual(len(self.api.comments), 1)
        self.assertEqual(len(self.api.writes), 1)
        self.assertEqual(self.api.downloads, [40, 40])

    def test_stale_pr_after_download_never_pushes_or_comments(self):
        original_download = self.api.download

        def download(artifact_id, destination):
            original_download(artifact_id, destination)
            self.api.pr["head"]["sha"] = "b" * 40

        with (
            patch.object(self.api, "download", side_effect=download),
            tempfile.TemporaryDirectory() as temporary,
            patch.object(publisher.subprocess, "run") as command,
        ):
            with self.assertRaises(publisher.SkipPreview):
                publisher.publish(
                    self.api,
                    self.api.event,
                    Path(temporary),
                    "actor",
                    "synthetic-token",
                )
            self.assertFalse(command.called)
            self.assertFalse((Path(temporary) / "auth.json").exists())
        self.assertFalse(self.api.writes)

    def test_skopeo_copies_without_running_image_and_verifies_all_tags(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checks = Mock()
            tags = publisher.image_tags(self.api.ci_run, self.api.pr)

            def command(args, **kwargs):
                self.assertEqual(args[0], "skopeo")
                self.assertNotIn("GH_TOKEN", kwargs["env"])
                self.assertNotIn("GITHUB_TOKEN", kwargs["env"])
                auth = root / "auth.json"
                self.assertEqual(stat.S_IMODE(auth.stat().st_mode), 0o600)
                self.assertIn("ghcr.io", json.loads(auth.read_text())["auths"])
                if "--digestfile" in args:
                    (root / "image.digest").write_text(IMAGE_DIGEST)
                return subprocess.CompletedProcess(args, 0, stdout=MANIFEST)

            with patch.object(
                publisher.subprocess, "run", side_effect=command
            ) as runner:
                result = publisher.copy_image(
                    f"ghcr.io/{REPOSITORY}/managed-js",
                    tags,
                    root / "image.tar",
                    root,
                    "actor",
                    "synthetic-token",
                    checks,
                )
            self.assertEqual(result, IMAGE_DIGEST)
            self.assertEqual(checks.call_count, 3)
            self.assertEqual(runner.call_count, 7)
            self.assertFalse((root / "auth.json").exists())
            self.assertIn("docker-archive:", runner.call_args_list[0].args[0][-2])
            self.assertIn("--preserve-digests", runner.call_args_list[1].args[0])

    def test_registry_digest_mismatch_prevents_success_and_cleans_auth(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def command(args, **kwargs):
                (root / "image.digest").write_text(IMAGE_DIGEST)
                return subprocess.CompletedProcess(
                    args, 0, stdout=b"different manifest"
                )

            with (
                patch.object(publisher.subprocess, "run", side_effect=command),
                self.assertRaisesRegex(ValueError, "digest verification"),
            ):
                publisher.copy_image(
                    f"ghcr.io/{REPOSITORY}/managed-js",
                    ["sha-" + SOURCE_SHA],
                    root / "image.tar",
                    root,
                    "actor",
                    "synthetic-token",
                    Mock(),
                )
            self.assertFalse((root / "auth.json").exists())

    def test_workflows_keep_privileged_and_unprivileged_execution_separate(self):
        producer = (ROOT / ".github/workflows/ci.yml").read_text()
        trusted = (
            ROOT / ".github/workflows/publish-managed-js-preview.yml"
        ).read_text()
        self.assertNotIn("packages: write", producer)
        self.assertNotIn("pull-requests: write", producer)
        self.assertIn("github.event.pull_request.head.sha || github.sha", producer)
        self.assertIn("python3 docker_managed_js_test.py --skip-build", producer)
        self.assertIn("head.repo.full_name == github.repository", producer)
        self.assertIn("ref: ${{ github.sha }}", trusted)
        self.assertNotIn("ref: ${{ github.event.workflow_run.head_sha }}", trusted)
        self.assertIn("persist-credentials: false", trusted)
        self.assertNotIn("docker run", trusted)
        self.assertNotIn("docker load", trusted)
        self.assertNotIn("secrets.", trusted)


if __name__ == "__main__":
    unittest.main()
