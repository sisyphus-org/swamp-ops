import concurrent.futures
import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from plugins.linear_source_route import OPS_BROKER_SOURCE_SCHEMA
from plugins.linear_source_route.ops_route import (
    OperationsRouteError,
    _load_completed,
    build_operations_task_body,
    delivery_key,
    parse_operations_request,
    validate_operations_request,
)
from plugins.linear_source_route.route import SourceContext
from plugins.ops_broker import OPS_BROKER_SCHEMA, default_runner
from plugins.ops_broker import github_git_askpass
from plugins.ops_broker.broker import (
    BrokerError,
    build_command,
    execute_request,
    validate_request,
)


SHA = "1" * 40
BASE_SHA = "2" * 40
REPOSITORY = "sisyphus-org/swamp-ops"
POLICY = {
    "peers": {
        "owner": {
            "operations": [
                "github.publish_branch",
                "github.upsert_pull_request",
            ]
        }
    },
    "github": {
        "repositories": [REPOSITORY],
        "pullRequestBases": {REPOSITORY: ["main"]},
    },
}


def github_ref_payload(argv):
    branch = argv[-1].rsplit("/", 1)[-1]
    sha = BASE_SHA if branch == "main" else SHA
    return [{"ref": f"refs/heads/{branch}", "object": {"sha": sha}}]


class GithubPublishTests(unittest.TestCase):
    def test_source_replay_rejects_publication_result_with_wrong_head(self):
        source = SourceContext(
            session_id="20260828_120000_abcdef12",
            profile="default",
            platform="telegram",
            chat_id="442308262",
            user_id="442308262",
            chat_type="dm",
            thread_id="455313",
        )
        request = {
            "request_id": "e7ba0358-034b-4f79-9b28-42c9212e7716",
            "integration": "github",
            "operation": "publish_branch",
            "arguments": {
                "repository": REPOSITORY,
                "branch": "SIS-82",
                "head_sha": SHA,
                "base": "main",
                "base_sha": BASE_SHA,
            },
            "mode": "apply",
        }
        command = parse_operations_request(request, source=source).command
        key = delivery_key(command["idempotency_key"], source)
        task = {
            "idempotency_key": key,
            "session_id": source.session_id,
            "body": build_operations_task_body(command),
            "result": json.dumps(
                {
                    "schema_version": "operations-result.v1",
                    "command_id": command["command_id"],
                    "idempotency_key": command["idempotency_key"],
                    "source_profile": "default",
                    "caller": "owner",
                    "request_id": request["request_id"],
                    "integration": "github",
                    "operation": "github.publish_branch",
                    "mode": "apply",
                    "status": "ok",
                    "result": {
                        **request["arguments"],
                        "head_sha": "2" * 40,
                        "changed": True,
                    },
                    "verified": True,
                }
            ),
        }
        with self.assertRaisesRegex(OperationsRouteError, "publication result"):
            _load_completed(task, command, key, source)

    def test_git_push_uses_profile_token_only_through_fixed_askpass(self):
        with tempfile.TemporaryDirectory(
            dir="/Users/hermes/workspaces/runtime"
        ) as temporary:
            workspace = Path(temporary) / "runtime"
            workspace.mkdir()
            object_directory = Path(temporary) / "objects"
            object_directory.mkdir()
            completed = [
                mock.Mock(returncode=0, stdout=str(object_directory) + "\n", stderr=""),
                mock.Mock(returncode=0, stdout="", stderr=""),
                mock.Mock(returncode=0, stdout="", stderr=""),
            ]
            with mock.patch.dict(
                os.environ,
                {"GH_TOKEN": "secret-fixture", "SWAMP_API_KEY": "must-not-propagate"},
            ), mock.patch(
                "plugins.ops_broker.subprocess.run", side_effect=completed
            ) as run:
                result = default_runner(
                    [
                        "git",
                        "push",
                        f"https://github.com/{REPOSITORY}.git",
                        f"{SHA}:refs/heads/SIS-82",
                    ],
                    cwd=workspace,
                    timeout=60,
                )
            argv = run.call_args.args[0]
            env = run.call_args.kwargs["env"]
        self.assertEqual(
            argv,
            [
                "git",
                "-c",
                "credential.helper=",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "push.followTags=false",
                "-c",
                "push.recurseSubmodules=no",
                "push",
                "--no-verify",
                "--no-follow-tags",
                "--recurse-submodules=no",
                "--porcelain",
                "--",
                f"https://github.com/{REPOSITORY}.git",
                f"{SHA}:refs/heads/SIS-82",
            ],
        )
        self.assertEqual(
            set(env),
            {
                "GH_TOKEN",
                "GIT_ASKPASS",
                "GIT_ASKPASS_ALLOWED_URL",
                "GIT_TERMINAL_PROMPT",
                "GIT_CONFIG_NOSYSTEM",
                "GIT_CONFIG_GLOBAL",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "LC_ALL",
                "PATH",
            },
        )
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertTrue(env["GIT_ASKPASS"].endswith("github_git_askpass.py"))
        self.assertNotIn("SWAMP_API_KEY", env)
        self.assertNotIn("secret-fixture", json.dumps(argv))
        self.assertEqual(result["returncode"], 0)

    def test_git_askpass_releases_token_only_for_github_prompts(self):
        with mock.patch.dict(os.environ, {"GH_TOKEN": "secret-fixture"}), mock.patch(
            "sys.argv", ["github_git_askpass.py", "Password for https://attacker.invalid"]
        ), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(github_git_askpass.main(), 1)
        self.assertEqual(output.getvalue(), "")

        with mock.patch.dict(
            os.environ,
            {
                "GH_TOKEN": "secret-fixture",
                "GIT_ASKPASS_ALLOWED_URL": f"https://github.com/{REPOSITORY}.git",
            },
        ), mock.patch(
            "sys.argv", ["github_git_askpass.py", "Password for https://github.com"]
        ), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(github_git_askpass.main(), 0)
        self.assertEqual(output.getvalue(), "secret-fixture\n")

        with mock.patch.dict(
            os.environ,
            {
                "GH_TOKEN": "secret-fixture",
                "GIT_ASKPASS_ALLOWED_URL": f"https://github.com/{REPOSITORY}.git",
            },
        ), mock.patch(
            "sys.argv",
            [
                "github_git_askpass.py",
                "Password for https://github.com:443@evil.invalid/repo.git",
            ],
        ), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(github_git_askpass.main(), 1)
        self.assertEqual(output.getvalue(), "")

    def test_git_push_runs_from_config_free_ephemeral_bare_repository(self):
        with tempfile.TemporaryDirectory(
            dir="/Users/hermes/workspaces/runtime"
        ) as temporary:
            workspace = Path(temporary) / "runtime"
            workspace.mkdir()
            object_directory = Path(temporary) / "objects"
            object_directory.mkdir()
            completed = [
                mock.Mock(returncode=0, stdout=str(object_directory) + "\n", stderr=""),
                mock.Mock(returncode=0, stdout="", stderr=""),
                mock.Mock(returncode=0, stdout="", stderr=""),
            ]
            with mock.patch.dict(
                os.environ, {"GH_TOKEN": "secret-fixture"}
            ), mock.patch(
                "plugins.ops_broker.subprocess.run", side_effect=completed
            ) as run:
                default_runner(
                    [
                        "git",
                        "push",
                        f"https://github.com/{REPOSITORY}.git",
                        f"{SHA}:refs/heads/SIS-82",
                    ],
                    cwd=workspace,
                    timeout=60,
                )

            self.assertEqual(
                run.call_args_list[0].args[0],
                ["git", "rev-parse", "--git-path", "objects"],
            )
            self.assertEqual(run.call_args_list[0].kwargs["cwd"], workspace)
            self.assertEqual(
                run.call_args_list[1].args[0][:3], ["git", "init", "--bare"]
            )
            push_call = run.call_args_list[2]
            self.assertNotEqual(push_call.kwargs["cwd"], workspace)
            self.assertEqual(
                push_call.kwargs["env"]["GIT_ALTERNATE_OBJECT_DIRECTORIES"],
                str(object_directory),
            )
            self.assertNotIn("GIT_CONFIG", push_call.kwargs["env"])

    def test_git_push_cannot_see_repository_local_url_rewrite(self):
        original_run = __import__("subprocess").run
        with tempfile.TemporaryDirectory(
            dir="/Users/hermes/workspaces/runtime"
        ) as temporary:
            workspace = Path(temporary) / "runtime"
            workspace.mkdir()
            original_run(["git", "init", "-q"], cwd=workspace, check=True)
            original_run(
                [
                    "git",
                    "config",
                    "url.file:///tmp/alternate.invalid.insteadOf",
                    f"https://github.com/{REPOSITORY}.git",
                ],
                cwd=workspace,
                check=True,
            )

            def guarded_run(argv, **kwargs):
                if "push" in argv:
                    visible = original_run(
                        ["git", "config", "--get-regexp", r"^url\."],
                        cwd=kwargs["cwd"],
                        env=kwargs["env"],
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(visible.returncode, 1, visible.stdout)
                    return mock.Mock(returncode=0, stdout="", stderr="")
                return original_run(argv, **kwargs)

            with mock.patch.dict(
                os.environ, {"GH_TOKEN": "secret-fixture"}
            ), mock.patch("plugins.ops_broker.subprocess.run", side_effect=guarded_run):
                result = default_runner(
                    [
                        "git",
                        "push",
                        f"https://github.com/{REPOSITORY}.git",
                        f"{SHA}:refs/heads/SIS-82",
                    ],
                    cwd=workspace,
                    timeout=60,
                )
        self.assertEqual(result["returncode"], 0)

    def test_ancestry_check_ignores_repository_replace_refs(self):
        original_run = __import__("subprocess").run
        clean = {
            "LC_ALL": "C",
            "PATH": os.environ.get("PATH", ""),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        }
        with tempfile.TemporaryDirectory(
            dir="/Users/hermes/workspaces/runtime"
        ) as temporary:
            repository = Path(temporary) / "repository"
            repository.mkdir()
            original_run(["git", "init", "-q"], cwd=repository, check=True)
            original_run(
                ["git", "config", "user.name", "Fixture"], cwd=repository, check=True
            )
            original_run(
                ["git", "config", "user.email", "fixture@example.invalid"],
                cwd=repository,
                check=True,
            )
            (repository / "base.txt").write_text("base\n")
            original_run(["git", "add", "base.txt"], cwd=repository, check=True)
            original_run(["git", "commit", "-qm", "base"], cwd=repository, check=True)
            base = original_run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            original_run(
                ["git", "checkout", "--orphan", "unrelated"],
                cwd=repository,
                capture_output=True,
                check=True,
            )
            original_run(
                ["git", "rm", "-rf", "."],
                cwd=repository,
                capture_output=True,
                check=True,
            )
            (repository / "head.txt").write_text("head\n")
            original_run(["git", "add", "head.txt"], cwd=repository, check=True)
            original_run(["git", "commit", "-qm", "head"], cwd=repository, check=True)
            head = original_run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            original_run(
                ["git", "replace", "--graft", head, base], cwd=repository, check=True
            )
            poisoned = original_run(
                ["git", "merge-base", "--is-ancestor", base, head],
                cwd=repository,
                env=clean,
                check=False,
            )
            self.assertEqual(poisoned.returncode, 0)

            verified = default_runner(
                ["git", "merge-base", "--is-ancestor", base, head],
                cwd=repository,
                timeout=60,
            )
        self.assertNotEqual(verified["returncode"], 0)

    def test_subprocesses_receive_only_their_integration_credential(self):
        completed = mock.Mock(returncode=0, stdout="{}", stderr="")
        with mock.patch.dict(
            os.environ,
            {"GH_TOKEN": "github-fixture", "SWAMP_API_KEY": "swamp-fixture"},
        ), mock.patch(
            "plugins.ops_broker.subprocess.run", return_value=completed
        ) as run:
            default_runner(
                ["gh", "api", "repos/sisyphus-org/swamp-ops"],
                cwd=Path("/reviewed/runtime"),
                timeout=60,
            )
            gh_env = run.call_args.kwargs["env"]
            default_runner(
                ["git", "rev-parse", "HEAD"],
                cwd=Path("/reviewed/runtime"),
                timeout=60,
            )
            git_env = run.call_args.kwargs["env"]
            default_runner(
                ["swamp", "auth", "whoami"],
                cwd=Path("/reviewed/runtime"),
                timeout=60,
            )
            swamp_env = run.call_args.kwargs["env"]

        self.assertEqual(gh_env.get("GH_TOKEN"), "github-fixture")
        self.assertNotIn("SWAMP_API_KEY", gh_env)
        self.assertNotIn("GH_TOKEN", git_env)
        self.assertNotIn("SWAMP_API_KEY", git_env)
        self.assertEqual(swamp_env.get("SWAMP_API_KEY"), "swamp-fixture")
        self.assertNotIn("GH_TOKEN", swamp_env)
        self.assertIn("/Users/hermes/.local/bin", swamp_env["PATH"].split(":"))
        self.assertIn("/Users/hermes/.hermes/bin", swamp_env["PATH"].split(":"))

    def test_public_schemas_and_policy_expose_publication_only_to_owner(self):
        for schema in (OPS_BROKER_SOURCE_SCHEMA, OPS_BROKER_SCHEMA):
            operations = schema["parameters"]["properties"]["operation"]["enum"]
            self.assertIn("publish_branch", operations)
            self.assertIn("upsert_pull_request", operations)
            self.assertEqual(
                schema["parameters"]["properties"]["arguments"]["maxProperties"],
                7,
            )
        policy = json.loads(
            (
                Path(__file__).parents[1]
                / "plugins"
                / "ops_broker"
                / "policy.json"
            ).read_text()
        )
        allowed = policy["peers"]["owner"]["operations"]
        self.assertIn("github.publish_branch", allowed)
        self.assertIn("github.upsert_pull_request", allowed)
        for caller in ("swe", "books", "crypto-analyst", "ideas"):
            allowed = policy["peers"][caller]["operations"]
            self.assertNotIn("github.publish_branch", allowed)
            self.assertNotIn("github.upsert_pull_request", allowed)
        self.assertEqual(
            policy["github"]["pullRequestBases"],
            {REPOSITORY: ["main"]},
        )

    def test_source_accepts_only_apply_mode_for_github_publication(self):
        for operation, arguments in (
            (
                "publish_branch",
                {
                    "repository": REPOSITORY,
                    "branch": "SIS-82",
                    "head_sha": SHA,
                    "base": "main",
                    "base_sha": BASE_SHA,
                },
            ),
            (
                "upsert_pull_request",
                {
                    "repository": REPOSITORY,
                    "branch": "SIS-82",
                    "head_sha": SHA,
                    "base": "main",
                    "base_sha": BASE_SHA,
                    "title": "SIS-82 Make scope recovery actionable",
                    "body": "https://linear.app/sisyphusx/issue/SIS-82/example",
                },
            ),
        ):
            payload = {
                "request_id": "e7ba0358-034b-4f79-9b28-42c9212e7716",
                "integration": "github",
                "operation": operation,
                "arguments": arguments,
                "mode": "apply",
            }
            with self.subTest(operation=operation):
                self.assertEqual(validate_operations_request(payload), payload)
                with self.assertRaisesRegex(OperationsRouteError, "mode=apply"):
                    validate_operations_request({**payload, "mode": "plan"})

    def test_source_rejects_malformed_publication_arguments_before_queue(self):
        valid = {
            "request_id": "e7ba0358-034b-4f79-9b28-42c9212e7716",
            "integration": "github",
            "operation": "upsert_pull_request",
            "arguments": {
                "repository": REPOSITORY,
                "branch": "SIS-82",
                "head_sha": SHA,
                "base": "main",
                "base_sha": BASE_SHA,
                "title": "SIS-82 Make scope recovery actionable",
                "body": "https://linear.app/sisyphusx/issue/SIS-82/example",
            },
            "mode": "apply",
        }
        invalid_arguments = (
            {**valid["arguments"], "repository": "attacker/target"},
            {**valid["arguments"], "branch": "feature/free-form"},
            {**valid["arguments"], "head_sha": "1" * 39},
            {**valid["arguments"], "title": "Wrong title"},
            {**valid["arguments"], "title": "SIS-82"},
            {**valid["arguments"], "title": "SIS-82:"},
            {**valid["arguments"], "title": "SIS-82 "},
            {**valid["arguments"], "title": "SIS-820 Wrong ticket"},
            {**valid["arguments"], "body": "No ticket link"},
            {
                **valid["arguments"],
                "body": "https://linear.app/sisyphusx/issue/SIS-82/",
            },
            {
                **valid["arguments"],
                "body": "https://linear.app/sisyphusx/issue/SIS-82/example?embedded=1",
            },
            {
                **valid["arguments"],
                "body": "https://linear.app/sisyphusx/issue/SIS-82/example.evil",
            },
            {
                **valid["arguments"],
                "body": "https://linear.app/sisyphusx/issue/SIS-82/example@evil.com",
            },
            {
                **valid["arguments"],
                "body": "https://linear.app/sisyphusx/issue/SIS-82/example;param",
            },
            {
                **valid["arguments"],
                "body": (
                    "https://attacker.invalid/?next="
                    "https://linear.app/sisyphusx/issue/SIS-82/example"
                ),
            },
            {
                **valid["arguments"],
                "body": (
                    "https://linear.app/sisyphusx/issue/SIS-82/example\n"
                    "GH_TOKEN=" + "A" * 24
                ),
            },
            {**valid["arguments"], "shell": "git push --force"},
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments), self.assertRaises(
                OperationsRouteError
            ):
                validate_operations_request({**valid, "arguments": arguments})

    def test_source_rejects_publication_from_non_owner_profile_before_queue(self):
        request = {
            "request_id": "e7ba0358-034b-4f79-9b28-42c9212e7716",
            "integration": "github",
            "operation": "publish_branch",
            "arguments": {
                "repository": REPOSITORY,
                "branch": "SIS-82",
                "head_sha": SHA,
                "base": "main",
                "base_sha": BASE_SHA,
            },
            "mode": "apply",
        }
        source = SourceContext(
            session_id="20260828_120000_abcdef12",
            profile="swe",
            platform="telegram",
            chat_id="442308262",
            user_id="442308262",
            chat_type="dm",
            thread_id="455313",
        )
        with self.assertRaisesRegex(OperationsRouteError, "authenticated owner"):
            parse_operations_request(request, source=source)

    def test_executor_rejects_wrong_ticket_title_before_provider_access(self):
        request = validate_request(
            {
                "request_id": "3c8fc5f1-fd81-4733-aa25-661a6903f480",
                "integration": "github",
                "operation": "upsert_pull_request",
                "arguments": {
                    "repository": REPOSITORY,
                    "branch": "SIS-82",
                    "head_sha": SHA,
                    "base": "main",
                    "base_sha": BASE_SHA,
                    "title": "SIS-820 Wrong ticket",
                    "body": "https://linear.app/sisyphusx/issue/SIS-82/example",
                },
                "mode": "apply",
            }
        )
        with self.assertRaisesRegex(ValueError, "exact SIS-N branch"), mock.patch(
            "plugins.ops_broker.broker._remote_branch_head"
        ) as provider:
            execute_request(
                request,
                caller="owner",
                policy=POLICY,
                runner=lambda *_args, **_kwargs: self.fail("runner must not execute"),
                workspace=Path("/reviewed/runtime"),
            )
        provider.assert_not_called()

    def test_executor_rejects_credential_shaped_pr_body_before_provider_access(self):
        request = validate_request(
            {
                "request_id": "3c8fc5f1-fd81-4733-aa25-661a6903f480",
                "integration": "github",
                "operation": "upsert_pull_request",
                "arguments": {
                    "repository": REPOSITORY,
                    "branch": "SIS-82",
                    "head_sha": SHA,
                    "base": "main",
                    "base_sha": BASE_SHA,
                    "title": "SIS-82 Make scope recovery actionable",
                    "body": (
                        "https://linear.app/sisyphusx/issue/SIS-82/example\n"
                        "GH_TOKEN=" + "A" * 24
                    ),
                },
                "mode": "apply",
            }
        )
        with self.assertRaisesRegex(ValueError, "credential-shaped"), mock.patch(
            "plugins.ops_broker.broker._remote_branch_head"
        ) as provider:
            execute_request(
                request,
                caller="owner",
                policy=POLICY,
                runner=lambda *_args, **_kwargs: self.fail("runner must not execute"),
                workspace=Path("/reviewed/runtime"),
            )
        provider.assert_not_called()

    def test_executor_rejects_empty_ticket_titles_and_url_continuations(self):
        valid = {
            "request_id": "3c8fc5f1-fd81-4733-aa25-661a6903f480",
            "integration": "github",
            "operation": "upsert_pull_request",
            "arguments": {
                "repository": REPOSITORY,
                "branch": "SIS-82",
                "head_sha": SHA,
                "base": "main",
                "base_sha": BASE_SHA,
                "title": "SIS-82 Valid title",
                "body": "https://linear.app/sisyphusx/issue/SIS-82/example",
            },
            "mode": "apply",
        }
        invalid_fields = (
            {"title": "SIS-82:"},
            {"title": "SIS-82 "},
            {"body": "https://linear.app/sisyphusx/issue/SIS-82/example?embedded=1"},
            {"body": "https://linear.app/sisyphusx/issue/SIS-82/example.evil"},
            {"body": "https://linear.app/sisyphusx/issue/SIS-82/example@evil.com"},
            {"body": "https://linear.app/sisyphusx/issue/SIS-82/example;param"},
        )
        for changed in invalid_fields:
            request = validate_request(
                {
                    **valid,
                    "arguments": {**valid["arguments"], **changed},
                }
            )
            with self.subTest(changed=changed), self.assertRaises(BrokerError):
                execute_request(
                    request,
                    caller="owner",
                    policy=POLICY,
                    runner=lambda *_args, **_kwargs: self.fail(
                        "runner must not execute"
                    ),
                    workspace=Path("/reviewed/runtime"),
                )

    def test_publish_branch_pushes_exact_local_head_and_verifies_remote_ref(self):
        request = validate_request(
            {
                "request_id": "e7ba0358-034b-4f79-9b28-42c9212e7716",
                "integration": "github",
                "operation": "publish_branch",
                "arguments": {
                    "repository": REPOSITORY,
                    "branch": "SIS-82",
                    "head_sha": SHA,
                    "base": "main",
                    "base_sha": BASE_SHA,
                },
                "mode": "apply",
            }
        )
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv in (
                ["git", "remote", "get-url", "origin"],
                ["git", "remote", "get-url", "--push", "origin"],
            ):
                return {
                    "returncode": 0,
                    "stdout": "https://github.com/sisyphus-org/swamp-ops.git\n",
                    "stderr": "",
                }
            if argv == ["git", "rev-parse", "refs/heads/SIS-82^{commit}"]:
                return {"returncode": 0, "stdout": SHA + "\n", "stderr": ""}
            if argv == [
                "git", "rev-parse", "refs/remotes/origin/main^{commit}"
            ]:
                return {"returncode": 0, "stdout": BASE_SHA + "\n", "stderr": ""}
            if argv == ["git", "merge-base", "--is-ancestor", BASE_SHA, SHA]:
                return {"returncode": 0, "stdout": "", "stderr": ""}
            if "matching-refs" in argv[-1]:
                if argv[-1].endswith("/main"):
                    payload = github_ref_payload(argv)
                else:
                    matching = [
                        item
                        for item, _ in calls
                        if item[-1] == argv[-1]
                    ]
                    payload = [] if len(matching) == 1 else github_ref_payload(argv)
                return {
                    "returncode": 0,
                    "stdout": json.dumps(payload),
                    "stderr": "",
                }
            if argv == [
                "git",
                "push",
                f"https://github.com/{REPOSITORY}.git",
                f"{SHA}:refs/heads/SIS-82",
            ]:
                return {"returncode": 0, "stdout": "", "stderr": ""}
            self.fail(f"unexpected argv: {argv}")

        response = execute_request(
            request,
            caller="owner",
            policy=POLICY,
            runner=runner,
            workspace=Path("/reviewed/runtime"),
        )

        self.assertEqual(
            response["result"],
            {
                "repository": REPOSITORY,
                "branch": "SIS-82",
                "head_sha": SHA,
                "base": "main",
                "base_sha": BASE_SHA,
                "changed": True,
            },
        )
        self.assertIn(
            (
                [
                    "git",
                    "push",
                    f"https://github.com/{REPOSITORY}.git",
                    f"{SHA}:refs/heads/SIS-82",
                ],
                {"cwd": Path("/reviewed/runtime"), "timeout": 60},
            ),
            calls,
        )

    def test_publish_branch_rejects_stale_remote_base_before_push(self):
        request = validate_request(
            {
                "request_id": "e7ba0358-034b-4f79-9b28-42c9212e7716",
                "integration": "github",
                "operation": "publish_branch",
                "arguments": {
                    "repository": REPOSITORY,
                    "branch": "SIS-82",
                    "head_sha": SHA,
                    "base": "main",
                    "base_sha": BASE_SHA,
                },
                "mode": "apply",
            }
        )
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if argv in (
                ["git", "remote", "get-url", "origin"],
                ["git", "remote", "get-url", "--push", "origin"],
            ):
                payload = f"https://github.com/{REPOSITORY}.git\n"
            elif argv == ["git", "rev-parse", "refs/heads/SIS-82^{commit}"]:
                payload = SHA + "\n"
            elif argv == ["git", "rev-parse", "refs/remotes/origin/main^{commit}"]:
                payload = BASE_SHA + "\n"
            elif argv == ["git", "merge-base", "--is-ancestor", BASE_SHA, SHA]:
                return {"returncode": 0, "stdout": "", "stderr": ""}
            elif argv[-1] == f"repos/{REPOSITORY}/git/matching-refs/heads/main":
                payload = json.dumps(
                    [{"ref": "refs/heads/main", "object": {"sha": "3" * 40}}]
                )
            else:
                self.fail(f"unexpected argv: {argv}")
            return {"returncode": 0, "stdout": payload, "stderr": ""}

        with self.assertRaisesRegex(BrokerError, "remote base"):
            execute_request(
                request,
                caller="owner",
                policy=POLICY,
                runner=runner,
                workspace=Path("/reviewed/runtime"),
            )
        self.assertFalse(any(argv[:2] == ["git", "push"] for argv in calls))

    def test_pull_request_checks_uses_only_fixed_gh_api_graphql(self):
        command = build_command(
            "github.pull_request_checks",
            {"repository": REPOSITORY, "pull_request": 42},
            {"github": {"repositories": [REPOSITORY]}},
        )
        self.assertEqual(command[:3], ["gh", "api", "graphql"])
        self.assertNotIn("pr", command)
        self.assertNotIn("checks", command)

    def test_publish_branch_reuses_exact_remote_head_without_push(self):
        request = validate_request(
            {
                "request_id": "e7ba0358-034b-4f79-9b28-42c9212e7716",
                "integration": "github",
                "operation": "publish_branch",
                "arguments": {
                    "repository": REPOSITORY,
                    "branch": "SIS-82",
                    "head_sha": SHA,
                    "base": "main",
                    "base_sha": BASE_SHA,
                },
                "mode": "apply",
            }
        )
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if argv in (
                ["git", "remote", "get-url", "origin"],
                ["git", "remote", "get-url", "--push", "origin"],
            ):
                payload = "https://github.com/sisyphus-org/swamp-ops.git\n"
            elif argv == ["git", "rev-parse", "refs/heads/SIS-82^{commit}"]:
                payload = SHA + "\n"
            elif argv == [
                "git", "rev-parse", "refs/remotes/origin/main^{commit}"
            ]:
                payload = BASE_SHA + "\n"
            elif argv == ["git", "merge-base", "--is-ancestor", BASE_SHA, SHA]:
                return {"returncode": 0, "stdout": "", "stderr": ""}
            elif "matching-refs" in argv[-1]:
                payload = json.dumps(github_ref_payload(argv))
            else:
                self.fail(f"unexpected push: {argv}")
            return {"returncode": 0, "stdout": payload, "stderr": ""}

        response = execute_request(
            request,
            caller="owner",
            policy=POLICY,
            runner=runner,
            workspace=Path("/reviewed/runtime"),
        )
        self.assertFalse(response["result"]["changed"])
        self.assertFalse(any(argv[:2] == ["git", "push"] for argv in calls))

    def test_upsert_pull_request_creates_exact_pr_and_reads_it_back(self):
        body = (
            "Implements the change.\n\n"
            "https://linear.app/sisyphusx/issue/SIS-82/example"
        )
        request = validate_request(
            {
                "request_id": "3c8fc5f1-fd81-4733-aa25-661a6903f480",
                "integration": "github",
                "operation": "upsert_pull_request",
                "arguments": {
                    "repository": REPOSITORY,
                    "branch": "SIS-82",
                    "head_sha": SHA,
                    "base": "main",
                    "base_sha": BASE_SHA,
                    "title": "SIS-82 Make scope recovery actionable",
                    "body": body,
                },
                "mode": "apply",
            }
        )
        calls = []
        pr = {
            "number": 42,
            "html_url": f"https://github.com/{REPOSITORY}/pull/42",
            "state": "open",
            "draft": False,
            "title": "SIS-82 Make scope recovery actionable",
            "body": body,
            "head": {
                "ref": "SIS-82",
                "sha": SHA,
                "repo": {"full_name": REPOSITORY},
            },
            "base": {
                "ref": "main",
                "sha": BASE_SHA,
                "repo": {"full_name": REPOSITORY},
            },
        }

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            if "matching-refs" in argv[-1]:
                payload = github_ref_payload(argv)
            elif argv == [
                "gh",
                "api",
                f"repos/{REPOSITORY}/pulls?state=open&head=sisyphus-org:SIS-82",
            ]:
                payload = []
            elif argv == [
                "gh",
                "api",
                "-X",
                "POST",
                f"repos/{REPOSITORY}/pulls",
                "-f",
                "head=SIS-82",
                "-f",
                "base=main",
                "-f",
                "title=SIS-82 Make scope recovery actionable",
                "-f",
                f"body={body}",
                "-F",
                "draft=false",
            ]:
                payload = pr
            elif argv == ["gh", "api", f"repos/{REPOSITORY}/pulls/42"]:
                payload = pr
            else:
                self.fail(f"unexpected argv: {argv}")
            return {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""}

        response = execute_request(
            request,
            caller="owner",
            policy=POLICY,
            runner=runner,
            workspace=Path("/reviewed/runtime"),
        )

        self.assertEqual(
            response["result"],
            {
                "repository": REPOSITORY,
                "number": 42,
                "url": f"https://github.com/{REPOSITORY}/pull/42",
                "branch": "SIS-82",
                "head_sha": SHA,
                "base": "main",
                "base_sha": BASE_SHA,
                "title": "SIS-82 Make scope recovery actionable",
                "changed": True,
            },
        )

    def test_upsert_pull_request_reuses_exact_open_pr_without_second_write(self):
        body = "https://linear.app/sisyphusx/issue/SIS-82/example"
        request = validate_request(
            {
                "request_id": "3c8fc5f1-fd81-4733-aa25-661a6903f480",
                "integration": "github",
                "operation": "upsert_pull_request",
                "arguments": {
                    "repository": REPOSITORY,
                    "branch": "SIS-82",
                    "head_sha": SHA,
                    "base": "main",
                    "base_sha": BASE_SHA,
                    "title": "SIS-82 Make scope recovery actionable",
                    "body": body,
                },
                "mode": "apply",
            }
        )
        pr = {
            "number": 42,
            "html_url": f"https://github.com/{REPOSITORY}/pull/42",
            "state": "open",
            "draft": False,
            "title": request["arguments"]["title"],
            "body": body,
            "head": {
                "ref": "SIS-82",
                "sha": SHA,
                "repo": {"full_name": REPOSITORY},
            },
            "base": {
                "ref": "main",
                "sha": BASE_SHA,
                "repo": {"full_name": REPOSITORY},
            },
        }
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if "matching-refs" in argv[-1]:
                payload = github_ref_payload(argv)
            elif "?state=open" in argv[-1]:
                payload = [pr]
            elif argv[-1] == f"repos/{REPOSITORY}/pulls/42":
                payload = pr
            else:
                self.fail(f"unexpected mutation: {argv}")
            return {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""}

        response = execute_request(
            request,
            caller="owner",
            policy=POLICY,
            runner=runner,
            workspace=Path("/reviewed/runtime"),
        )
        self.assertFalse(response["result"]["changed"])
        self.assertFalse(any("POST" in argv or "PATCH" in argv for argv in calls))

    def test_upsert_pull_request_updates_title_and_body_then_reads_back(self):
        body = "https://linear.app/sisyphusx/issue/SIS-82/example\nUpdated"
        title = "SIS-82 Updated title"
        request = validate_request(
            {
                "request_id": "3c8fc5f1-fd81-4733-aa25-661a6903f480",
                "integration": "github",
                "operation": "upsert_pull_request",
                "arguments": {
                    "repository": REPOSITORY,
                    "branch": "SIS-82",
                    "head_sha": SHA,
                    "base": "main",
                    "base_sha": BASE_SHA,
                    "title": title,
                    "body": body,
                },
                "mode": "apply",
            }
        )
        existing = {
            "number": 42,
            "html_url": f"https://github.com/{REPOSITORY}/pull/42",
            "state": "open",
            "draft": False,
            "title": "SIS-82 Old title",
            "body": "old",
            "head": {
                "ref": "SIS-82",
                "sha": SHA,
                "repo": {"full_name": REPOSITORY},
            },
            "base": {
                "ref": "legacy",
                "sha": BASE_SHA,
                "repo": {"full_name": REPOSITORY},
            },
        }
        updated = {
            **existing,
            "title": title,
            "body": body,
            "base": {
                "ref": "main",
                "sha": BASE_SHA,
                "repo": {"full_name": REPOSITORY},
            },
        }
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if "matching-refs" in argv[-1]:
                payload = github_ref_payload(argv)
            elif "?state=open" in argv[-1]:
                payload = [existing]
            elif "PATCH" in argv:
                payload = updated
            elif argv[-1] == f"repos/{REPOSITORY}/pulls/42":
                payload = updated
            else:
                self.fail(f"unexpected argv: {argv}")
            return {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""}

        response = execute_request(
            request,
            caller="owner",
            policy=POLICY,
            runner=runner,
            workspace=Path("/reviewed/runtime"),
        )
        self.assertTrue(response["result"]["changed"])
        patch_call = next(argv for argv in calls if "PATCH" in argv)
        self.assertIn(f"title={title}", patch_call)
        self.assertIn(f"body={body}", patch_call)

    def test_publish_branch_reconciles_ambiguous_push_failure_by_exact_readback(self):
        request = validate_request(
            {
                "request_id": "e7ba0358-034b-4f79-9b28-42c9212e7716",
                "integration": "github",
                "operation": "publish_branch",
                "arguments": {
                    "repository": REPOSITORY,
                    "branch": "SIS-82",
                    "head_sha": SHA,
                    "base": "main",
                    "base_sha": BASE_SHA,
                },
                "mode": "apply",
            }
        )
        reads = 0

        def runner(argv, **kwargs):
            nonlocal reads
            if argv[:4] == ["git", "remote", "get-url", "origin"] or argv[:5] == [
                "git", "remote", "get-url", "--push", "origin"
            ]:
                return {
                    "returncode": 0,
                    "stdout": f"https://github.com/{REPOSITORY}.git\n",
                    "stderr": "",
                }
            if argv == ["git", "rev-parse", "refs/heads/SIS-82^{commit}"]:
                return {"returncode": 0, "stdout": SHA + "\n", "stderr": ""}
            if argv == [
                "git", "rev-parse", "refs/remotes/origin/main^{commit}"
            ]:
                return {"returncode": 0, "stdout": BASE_SHA + "\n", "stderr": ""}
            if argv == ["git", "merge-base", "--is-ancestor", BASE_SHA, SHA]:
                return {"returncode": 0, "stdout": "", "stderr": ""}
            if argv[-1].endswith("/main"):
                return {
                    "returncode": 0,
                    "stdout": json.dumps(github_ref_payload(argv)),
                    "stderr": "",
                }
            if "matching-refs" in argv[-1]:
                reads += 1
                payload = [] if reads == 1 else [
                    {"ref": "refs/heads/SIS-82", "object": {"sha": SHA}}
                ]
                return {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""}
            if argv[:2] == ["git", "push"]:
                return {"returncode": 1, "stdout": "", "stderr": "ambiguous"}
            self.fail(f"unexpected argv: {argv}")

        response = execute_request(
            request,
            caller="owner",
            policy=POLICY,
            runner=runner,
            workspace=Path("/reviewed/runtime"),
        )
        self.assertTrue(response["result"]["changed"])

    def test_upsert_pr_reconciles_ambiguous_create_failure_without_second_post(self):
        body = "https://linear.app/sisyphusx/issue/SIS-82/example"
        title = "SIS-82 Make scope recovery actionable"
        request = validate_request(
            {
                "request_id": "3c8fc5f1-fd81-4733-aa25-661a6903f480",
                "integration": "github",
                "operation": "upsert_pull_request",
                "arguments": {
                    "repository": REPOSITORY,
                    "branch": "SIS-82",
                    "head_sha": SHA,
                    "base": "main",
                    "base_sha": BASE_SHA,
                    "title": title,
                    "body": body,
                },
                "mode": "apply",
            }
        )
        pr = {
            "number": 42,
            "html_url": f"https://github.com/{REPOSITORY}/pull/42",
            "state": "open",
            "draft": False,
            "title": title,
            "body": body,
            "head": {
                "ref": "SIS-82",
                "sha": SHA,
                "repo": {"full_name": REPOSITORY},
            },
            "base": {
                "ref": "main",
                "sha": BASE_SHA,
                "repo": {"full_name": REPOSITORY},
            },
        }
        lookups = 0
        posts = 0

        def runner(argv, **kwargs):
            nonlocal lookups, posts
            if "matching-refs" in argv[-1]:
                payload = github_ref_payload(argv)
            elif "?state=open" in argv[-1]:
                lookups += 1
                payload = [] if lookups == 1 else [pr]
            elif "POST" in argv:
                posts += 1
                return {"returncode": 1, "stdout": "", "stderr": "ambiguous"}
            elif argv[-1] == f"repos/{REPOSITORY}/pulls/42":
                payload = pr
            else:
                self.fail(f"unexpected argv: {argv}")
            return {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""}

        response = execute_request(
            request,
            caller="owner",
            policy=POLICY,
            runner=runner,
            workspace=Path("/reviewed/runtime"),
        )
        self.assertTrue(response["result"]["changed"])
        self.assertEqual(posts, 1)

    def test_upsert_pr_rejects_same_owner_fork_head_identity(self):
        body = "https://linear.app/sisyphusx/issue/SIS-82/example"
        request = validate_request(
            {
                "request_id": "3c8fc5f1-fd81-4733-aa25-661a6903f480",
                "integration": "github",
                "operation": "upsert_pull_request",
                "arguments": {
                    "repository": REPOSITORY,
                    "branch": "SIS-82",
                    "head_sha": SHA,
                    "base": "main",
                    "base_sha": BASE_SHA,
                    "title": "SIS-82 Make scope recovery actionable",
                    "body": body,
                },
                "mode": "apply",
            }
        )
        fork_pr = {
            "number": 42,
            "html_url": f"https://github.com/{REPOSITORY}/pull/42",
            "state": "open",
            "draft": False,
            "title": request["arguments"]["title"],
            "body": body,
            "head": {
                "ref": "SIS-82",
                "sha": SHA,
                "repo": {"full_name": "sisyphus-org/fork"},
            },
            "base": {
                "ref": "main",
                "sha": BASE_SHA,
                "repo": {"full_name": REPOSITORY},
            },
        }

        def runner(argv, **kwargs):
            if "matching-refs" in argv[-1]:
                payload = github_ref_payload(argv)
            elif "?state=open" in argv[-1]:
                payload = [fork_pr]
            else:
                self.fail(f"unexpected mutation: {argv}")
            return {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""}

        with self.assertRaisesRegex(BrokerError, "lookup is invalid"):
            execute_request(
                request,
                caller="owner",
                policy=POLICY,
                runner=runner,
                workspace=Path("/reviewed/runtime"),
            )

    def test_upsert_pr_rejects_wrong_base_sha_on_final_readback(self):
        body = "https://linear.app/sisyphusx/issue/SIS-82/example"
        request = validate_request(
            {
                "request_id": "3c8fc5f1-fd81-4733-aa25-661a6903f480",
                "integration": "github",
                "operation": "upsert_pull_request",
                "arguments": {
                    "repository": REPOSITORY,
                    "branch": "SIS-82",
                    "head_sha": SHA,
                    "base": "main",
                    "base_sha": BASE_SHA,
                    "title": "SIS-82 Make scope recovery actionable",
                    "body": body,
                },
                "mode": "apply",
            }
        )
        pr = {
            "number": 42,
            "html_url": f"https://github.com/{REPOSITORY}/pull/42",
            "state": "open",
            "draft": False,
            "title": request["arguments"]["title"],
            "body": body,
            "head": {
                "ref": "SIS-82",
                "sha": SHA,
                "repo": {"full_name": REPOSITORY},
            },
            "base": {
                "ref": "main",
                "sha": "3" * 40,
                "repo": {"full_name": REPOSITORY},
            },
        }

        def runner(argv, **kwargs):
            if "matching-refs" in argv[-1]:
                payload = github_ref_payload(argv)
            elif "?state=open" in argv[-1]:
                payload = [pr]
            elif argv[-1] == f"repos/{REPOSITORY}/pulls/42":
                payload = pr
            else:
                self.fail(f"unexpected mutation: {argv}")
            return {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""}

        with self.assertRaisesRegex(BrokerError, "exact read-back"):
            execute_request(
                request,
                caller="owner",
                policy=POLICY,
                runner=runner,
                workspace=Path("/reviewed/runtime"),
            )

    def test_upsert_pr_reconciles_ambiguous_patch_failure_by_exact_readback(self):
        body = "https://linear.app/sisyphusx/issue/SIS-82/example\nUpdated"
        title = "SIS-82 Updated title"
        request = validate_request(
            {
                "request_id": "3c8fc5f1-fd81-4733-aa25-661a6903f480",
                "integration": "github",
                "operation": "upsert_pull_request",
                "arguments": {
                    "repository": REPOSITORY,
                    "branch": "SIS-82",
                    "head_sha": SHA,
                    "base": "main",
                    "base_sha": BASE_SHA,
                    "title": title,
                    "body": body,
                },
                "mode": "apply",
            }
        )
        existing = {
            "number": 42,
            "html_url": f"https://github.com/{REPOSITORY}/pull/42",
            "state": "open",
            "draft": False,
            "title": "SIS-82 Old title",
            "body": "old",
            "head": {
                "ref": "SIS-82",
                "sha": SHA,
                "repo": {"full_name": REPOSITORY},
            },
            "base": {
                "ref": "main",
                "sha": BASE_SHA,
                "repo": {"full_name": REPOSITORY},
            },
        }
        updated = {**existing, "title": title, "body": body}
        patches = 0

        def runner(argv, **kwargs):
            nonlocal patches
            if "matching-refs" in argv[-1]:
                payload = github_ref_payload(argv)
            elif "?state=open" in argv[-1]:
                payload = [existing]
            elif "PATCH" in argv:
                patches += 1
                return {"returncode": 1, "stdout": "", "stderr": "ambiguous"}
            elif argv[-1] == f"repos/{REPOSITORY}/pulls/42":
                payload = updated
            else:
                self.fail(f"unexpected argv: {argv}")
            return {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""}

        response = execute_request(
            request,
            caller="owner",
            policy=POLICY,
            runner=runner,
            workspace=Path("/reviewed/runtime"),
        )
        self.assertTrue(response["result"]["changed"])
        self.assertEqual(patches, 1)

    def test_concurrent_semantic_pr_upserts_share_one_locked_mutation(self):
        body = "https://linear.app/sisyphusx/issue/SIS-82/example"
        title = "SIS-82 Make scope recovery actionable"
        base_request = {
            "integration": "github",
            "operation": "upsert_pull_request",
            "arguments": {
                "repository": REPOSITORY,
                "branch": "SIS-82",
                "head_sha": SHA,
                "base": "main",
                "base_sha": BASE_SHA,
                "title": title,
                "body": body,
            },
            "mode": "apply",
        }
        requests = [
            validate_request({**base_request, "request_id": request_id})
            for request_id in (
                "3c8fc5f1-fd81-4733-aa25-661a6903f480",
                "e7ba0358-034b-4f79-9b28-42c9212e7716",
            )
        ]
        pr = {
            "number": 42,
            "html_url": f"https://github.com/{REPOSITORY}/pull/42",
            "state": "open",
            "draft": False,
            "title": title,
            "body": body,
            "head": {
                "ref": "SIS-82",
                "sha": SHA,
                "repo": {"full_name": REPOSITORY},
            },
            "base": {
                "ref": "main",
                "sha": BASE_SHA,
                "repo": {"full_name": REPOSITORY},
            },
        }
        state_lock = threading.Lock()
        state = {"exists": False, "posts": 0}

        def runner(argv, **kwargs):
            if "matching-refs" in argv[-1]:
                payload = github_ref_payload(argv)
            elif "?state=open" in argv[-1]:
                with state_lock:
                    exists = state["exists"]
                if not exists:
                    time.sleep(0.03)
                payload = [pr] if exists else []
            elif "POST" in argv:
                with state_lock:
                    state["posts"] += 1
                    state["exists"] = True
                payload = pr
            elif argv[-1] == f"repos/{REPOSITORY}/pulls/42":
                payload = pr
            else:
                self.fail(f"unexpected argv: {argv}")
            return {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""}

        with tempfile.TemporaryDirectory() as temporary:
            audit_path = Path(temporary) / "audit.jsonl"
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(
                    pool.map(
                        lambda pair: execute_request(
                            pair[0],
                            caller=pair[1],
                            policy=POLICY,
                            runner=runner,
                            workspace=Path("/reviewed/runtime"),
                            audit_path=audit_path,
                        ),
                        zip(requests, ("owner", "owner"), strict=True),
                    )
                )
            journals = list((audit_path.parent / "github-publication").glob("*.json"))

        self.assertEqual(state["posts"], 1)
        self.assertEqual([result["status"] for result in results], ["ok", "ok"])
        self.assertEqual(len(journals), 1)


if __name__ == "__main__":
    unittest.main()
