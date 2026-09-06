from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import bootstrap_route53_access_keys as bootstrap  # noqa: E402


ACCOUNT_ID = "123456789012"
STACK_NAME = "umc-product-route53-dns"


def empty_env_text() -> str:
    return "# unrelated values are preserved\n" + "\n".join(
        f"{key}=" for key in bootstrap.ENV_KEYS
    ) + "\nUNCHANGED=value\n"


def valid_outputs() -> dict[str, str]:
    return {
        "Route53HostedZoneId": "Z0123456789ABCDEF",
        "DnsZoneName": bootstrap.DNS_ZONE_NAME,
        "CertManagerRoute53UserName": bootstrap.CERT_MANAGER_USER,
        "ExternalDNSRoute53UserName": bootstrap.EXTERNAL_DNS_USER,
    }


def cli_args(root: Path, *, region: str = bootstrap.SEOUL_REGION) -> argparse.Namespace:
    return argparse.Namespace(
        profile="test-profile",
        region=region,
        expected_account_id=ACCOUNT_ID,
        env_file=root / ".env.prod",
        route53_stack=STACK_NAME,
    )


def git_safety_check(command: list[str], _cwd: Path) -> bool:
    if command[1] == "check-ignore":
        return True
    if command[1] == "ls-files":
        return False
    raise AssertionError(f"unexpected command: {command}")


class FakeAws:
    def __init__(
        self,
        *,
        account_id: str = ACCOUNT_ID,
        caller_arn: str | None = None,
        stack_status: str = "CREATE_COMPLETE",
        outputs: dict[str, str] | None = None,
        existing_user: str | None = None,
        bad_user_arn: str | None = None,
        malformed_create_user: str | None = None,
    ) -> None:
        self.account_id = account_id
        self.caller_arn = caller_arn or (
            f"arn:aws:iam::{account_id}:user/operator"
        )
        self.stack_status = stack_status
        self.outputs = outputs or valid_outputs()
        self.bad_user_arn = bad_user_arn
        self.malformed_create_user = malformed_create_user
        self.events: list[tuple[str, str]] = []
        self.keys: dict[str, list[dict[str, str]]] = {
            bootstrap.CERT_MANAGER_USER: [],
            bootstrap.EXTERNAL_DNS_USER: [],
        }
        if existing_user is not None:
            self.keys[existing_user].append(
                {
                    "UserName": existing_user,
                    "AccessKeyId": "AKIAEXISTING",
                    "Status": "Active",
                }
            )
        self.secrets: dict[str, str] = {}

    def json(self, arguments: list[str]) -> dict[str, object]:
        operation = (arguments[0], arguments[1])
        self.events.append(operation)

        if operation == ("sts", "get-caller-identity"):
            return {"Account": self.account_id, "Arn": self.caller_arn}
        if operation == ("cloudformation", "describe-stacks"):
            return {
                "Stacks": [
                    {
                        "StackStatus": self.stack_status,
                        "Outputs": [
                            {"OutputKey": key, "OutputValue": value}
                            for key, value in self.outputs.items()
                        ],
                    }
                ]
            }
        if operation == ("iam", "get-user"):
            user_name = arguments[arguments.index("--user-name") + 1]
            arn = f"arn:aws:iam::{ACCOUNT_ID}:user/{user_name}"
            if user_name == self.bad_user_arn:
                arn += "-wrong"
            return {"User": {"Arn": arn}}
        if operation == ("iam", "list-access-keys"):
            user_name = arguments[arguments.index("--user-name") + 1]
            return {"AccessKeyMetadata": [*self.keys[user_name]]}
        if operation == ("iam", "create-access-key"):
            user_name = arguments[arguments.index("--user-name") + 1]
            suffix = "CERT" if user_name == bootstrap.CERT_MANAGER_USER else "DNS"
            access_key_id = f"AKIA{suffix}"
            secret = f"secret-{suffix.lower()}"
            self.keys[user_name].append(
                {
                    "UserName": user_name,
                    "AccessKeyId": access_key_id,
                    "Status": "Active",
                }
            )
            self.secrets[access_key_id] = secret
            if user_name == self.malformed_create_user:
                return {}
            return {
                "AccessKey": {
                    "UserName": user_name,
                    "AccessKeyId": access_key_id,
                    "SecretAccessKey": secret,
                    "Status": "Active",
                }
            }
        if operation == ("iam", "delete-access-key"):
            document = json.loads(arguments[arguments.index("--cli-input-json") + 1])
            user_name = document["UserName"]
            access_key_id = document["AccessKeyId"]
            self.keys[user_name] = [
                item
                for item in self.keys[user_name]
                if item["AccessKeyId"] != access_key_id
            ]
            self.secrets.pop(access_key_id, None)
            return {}
        raise AssertionError(f"unexpected AWS operation: {arguments}")

    def identity_with_key(self, issued_key: bootstrap.IssuedKey) -> dict[str, str]:
        self.events.append(("sts", "issued-key-identity"))
        return {
            "Account": ACCOUNT_ID,
            "Arn": f"arn:aws:iam::{ACCOUNT_ID}:user/{issued_key.user_name}",
        }


class EnvFileContractTests(unittest.TestCase):
    def make_env(self, root: Path, contents: str | None = None) -> Path:
        path = root / ".env.prod"
        path.write_text(contents or empty_env_text(), encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_accepts_only_safe_empty_repo_root_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.make_env(root)
            with mock.patch.object(
                bootstrap, "command_succeeded", side_effect=git_safety_check
            ):
                contents = bootstrap.read_env_file(path, root)

            self.assertEqual(contents, empty_env_text())

    def test_rejects_wrong_path_symlink_nonregular_or_wrong_mode(self) -> None:
        cases = ("wrong-path", "symlink", "nonregular", "mode")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = self.make_env(root)
                candidate = path
                if case == "wrong-path":
                    candidate = root / "copy.env"
                    candidate.write_text(empty_env_text(), encoding="utf-8")
                    candidate.chmod(0o600)
                elif case == "symlink":
                    target = root / "target.env"
                    path.replace(target)
                    path.symlink_to(target)
                elif case == "nonregular":
                    path.unlink()
                    path.mkdir()
                else:
                    path.chmod(0o640)

                with mock.patch.object(
                    bootstrap, "command_succeeded", side_effect=git_safety_check
                ):
                    with self.assertRaises(bootstrap.BootstrapError):
                        bootstrap.read_env_file(candidate, root)

    def test_rejects_not_ignored_tracked_nonempty_missing_or_duplicate(self) -> None:
        cases = ("not-ignored", "tracked", "nonempty", "missing", "duplicate")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                contents = empty_env_text()
                if case == "nonempty":
                    contents = contents.replace(
                        "CERT_MANAGER_ROUTE53_ACCESS_KEY_ID=",
                        "CERT_MANAGER_ROUTE53_ACCESS_KEY_ID=already-set",
                    )
                elif case == "missing":
                    contents = contents.replace(
                        "CERT_MANAGER_ROUTE53_ACCESS_KEY_ID=\n", ""
                    )
                elif case == "duplicate":
                    contents += "CERT_MANAGER_ROUTE53_ACCESS_KEY_ID=\n"
                path = self.make_env(root, contents)

                def check(command: list[str], _cwd: Path) -> bool:
                    if command[1] == "check-ignore":
                        return case != "not-ignored"
                    if command[1] == "ls-files":
                        return case == "tracked"
                    raise AssertionError(f"unexpected command: {command}")

                with mock.patch.object(
                    bootstrap, "command_succeeded", side_effect=check
                ):
                    with self.assertRaises(bootstrap.BootstrapError):
                        bootstrap.read_env_file(path, root)


class StackContractTests(unittest.TestCase):
    def test_uses_delegated_zone(self) -> None:
        self.assertEqual(bootstrap.DNS_ZONE_NAME, "university.neordinary.com")

    def test_accepts_expected_output_contract(self) -> None:
        bootstrap.validate_stack_contract(valid_outputs(), STACK_NAME)

    def test_rejects_missing_or_mismatched_output_contract(self) -> None:
        invalid_values = {
            "Route53HostedZoneId": "hostedzone/Z012345",
            "DnsZoneName": "example.com",
            "CertManagerRoute53UserName": "wrong-cert-user",
            "ExternalDNSRoute53UserName": "wrong-dns-user",
        }
        for key, value in invalid_values.items():
            with self.subTest(key=key):
                outputs = valid_outputs()
                outputs[key] = value
                with self.assertRaises(bootstrap.BootstrapError):
                    bootstrap.validate_stack_contract(outputs, STACK_NAME)

        for key in bootstrap.STACK_OUTPUTS:
            with self.subTest(missing=key):
                outputs = valid_outputs()
                del outputs[key]
                with self.assertRaises(bootstrap.BootstrapError):
                    bootstrap.validate_stack_contract(outputs, STACK_NAME)


class BootstrapFlowTests(unittest.TestCase):
    def run_main(
        self,
        root: Path,
        aws: FakeAws,
        *,
        region: str = bootstrap.SEOUL_REGION,
    ) -> tuple[int, str]:
        output = io.StringIO()
        with (
            mock.patch.object(bootstrap, "parse_args", return_value=cli_args(root, region=region)),
            mock.patch.object(bootstrap, "repo_root", return_value=root),
            mock.patch.object(
                bootstrap, "command_succeeded", side_effect=git_safety_check
            ),
            mock.patch.object(bootstrap, "AwsCli", return_value=aws),
            contextlib.redirect_stdout(output),
        ):
            result = bootstrap.main()
        return result, output.getvalue()

    @staticmethod
    def make_env(root: Path) -> Path:
        path = root / ".env.prod"
        path.write_text(empty_env_text(), encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_success_preflights_both_users_and_writes_without_disclosure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.make_env(root)
            aws = FakeAws()

            result, output = self.run_main(root, aws)

            self.assertEqual(result, 0)
            self.assertLess(
                max(
                    index
                    for index, event in enumerate(aws.events)
                    if event == ("iam", "list-access-keys")
                ),
                min(
                    index
                    for index, event in enumerate(aws.events)
                    if event == ("iam", "create-access-key")
                ),
            )
            contents = path.read_text(encoding="utf-8")
            expected_values = {
                "CERT_MANAGER_ROUTE53_ACCESS_KEY_ID": "AKIACERT",
                "CERT_MANAGER_ROUTE53_SECRET_ACCESS_KEY": "secret-cert",
                "EXTERNAL_DNS_ROUTE53_ACCESS_KEY_ID": "AKIADNS",
                "EXTERNAL_DNS_ROUTE53_SECRET_ACCESS_KEY": "secret-dns",
            }
            for key, value in expected_values.items():
                self.assertIn(f"{key}={value}\n", contents)
                self.assertNotIn(value, output)
            self.assertIn("UNCHANGED=value\n", contents)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(
                aws.events.count(("sts", "issued-key-identity")), 2
            )

    def test_rejects_wrong_account_root_identity_or_incomplete_stack(self) -> None:
        cases = {
            "wrong-account": FakeAws(account_id="999999999999"),
            "root": FakeAws(caller_arn=f"arn:aws:iam::{ACCOUNT_ID}:root"),
            "incomplete-stack": FakeAws(stack_status="UPDATE_IN_PROGRESS"),
        }
        for case, aws in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.make_env(root)
                with self.assertRaises(bootstrap.BootstrapError):
                    self.run_main(root, aws)
                self.assertNotIn(("iam", "create-access-key"), aws.events)

    def test_rejects_non_seoul_region_before_local_or_aws_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                mock.patch.object(
                    bootstrap,
                    "parse_args",
                    return_value=cli_args(root, region="us-east-1"),
                ),
                mock.patch.object(bootstrap, "repo_root") as find_root,
            ):
                with self.assertRaises(bootstrap.BootstrapError):
                    bootstrap.main()
            find_root.assert_not_called()

    def test_existing_key_or_user_arn_mismatch_stops_before_issuance(self) -> None:
        cases = {
            "existing-key": FakeAws(existing_user=bootstrap.CERT_MANAGER_USER),
            "bad-user-arn": FakeAws(bad_user_arn=bootstrap.CERT_MANAGER_USER),
        }
        for case, aws in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.make_env(root)
                with self.assertRaises(bootstrap.BootstrapError):
                    self.run_main(root, aws)
                self.assertNotIn(("iam", "create-access-key"), aws.events)

    def test_precommit_write_failure_rolls_back_both_new_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.make_env(root)
            original = path.read_text(encoding="utf-8")
            aws = FakeAws()

            with mock.patch.object(
                bootstrap, "atomic_write", side_effect=OSError("test write failure")
            ):
                with self.assertRaises(OSError):
                    self.run_main(root, aws)

            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(aws.keys[bootstrap.CERT_MANAGER_USER], [])
            self.assertEqual(aws.keys[bootstrap.EXTERNAL_DNS_USER], [])
            self.assertEqual(
                aws.events.count(("iam", "delete-access-key")), 2
            )

    def test_lost_create_response_is_found_and_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.make_env(root)
            original = path.read_text(encoding="utf-8")
            aws = FakeAws(malformed_create_user=bootstrap.EXTERNAL_DNS_USER)

            with self.assertRaises(bootstrap.BootstrapError):
                self.run_main(root, aws)

            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(aws.keys[bootstrap.CERT_MANAGER_USER], [])
            self.assertEqual(aws.keys[bootstrap.EXTERNAL_DNS_USER], [])
            self.assertEqual(
                aws.events.count(("iam", "delete-access-key")), 2
            )


if __name__ == "__main__":
    unittest.main()
