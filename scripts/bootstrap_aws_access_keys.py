#!/usr/bin/env python3
"""Issue initial prod IAM access keys and write them to .env.prod without disclosure."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SEOUL_REGION = "ap-northeast-2"
ENV_KEYS = (
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "SES_ACCESS_KEY_ID",
    "SES_SECRET_ACCESS_KEY",
    "BACKUP_AWS_ACCESS_KEY_ID",
    "BACKUP_AWS_SECRET_ACCESS_KEY",
    "BACKUP_AWS_REGION",
    "BACKUP_S3_BUCKET",
    "BACKUP_S3_EXPECTED_BUCKET_OWNER",
)


class BootstrapError(RuntimeError):
    """Expected bootstrap failure safe to report without command output."""


@dataclass(frozen=True)
class IssuedKey:
    label: str
    user_name: str
    access_key_id: str
    secret_access_key: str


class AwsCli:
    def __init__(self, profile: str, region: str) -> None:
        self.profile = profile
        self.region = region

    @staticmethod
    def _environment() -> dict[str, str]:
        environment = os.environ.copy()
        environment["AWS_PAGER"] = ""
        environment["AWS_CLI_AUTO_PROMPT"] = "off"
        return environment

    def json(
        self,
        arguments: list[str],
    ) -> dict[str, Any]:
        command = [
            "aws",
            "--profile",
            self.profile,
            "--region",
            self.region,
            "--no-cli-pager",
            "--output",
            "json",
            *arguments,
        ]
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            shell=False,
            env=self._environment(),
        )
        if result.returncode != 0:
            operation = " ".join(arguments[:2])
            raise BootstrapError(f"AWS {operation} failed")
        try:
            parsed = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as error:
            raise BootstrapError("AWS CLI returned invalid JSON") from error
        if not isinstance(parsed, dict):
            raise BootstrapError("AWS CLI returned an unexpected response")
        return parsed

    def identity_with_key(self, issued_key: IssuedKey) -> dict[str, Any]:
        environment = self._environment()
        for name in (
            "AWS_PROFILE",
            "AWS_DEFAULT_PROFILE",
            "AWS_SESSION_TOKEN",
            "AWS_SECURITY_TOKEN",
            "AWS_ROLE_ARN",
            "AWS_WEB_IDENTITY_TOKEN_FILE",
            "AWS_CONTAINER_CREDENTIALS_FULL_URI",
            "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
            "AWS_LOGIN_CACHE_DIRECTORY",
        ):
            environment.pop(name, None)
        environment["AWS_ACCESS_KEY_ID"] = issued_key.access_key_id
        environment["AWS_SECRET_ACCESS_KEY"] = issued_key.secret_access_key
        environment["AWS_EC2_METADATA_DISABLED"] = "true"
        # default profile의 aws login 세션보다 방금 발급한 환경변수 자격증명을 확실히 우선한다.
        environment["AWS_CONFIG_FILE"] = os.devnull
        environment["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
        result = subprocess.run(
            [
                "aws",
                "--region",
                self.region,
                "--no-cli-pager",
                "--output",
                "json",
                "sts",
                "get-caller-identity",
            ],
            text=True,
            capture_output=True,
            check=False,
            shell=False,
            env=environment,
        )
        if result.returncode != 0:
            raise BootstrapError(f"{issued_key.label} credential validation failed")
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise BootstrapError(
                f"{issued_key.label} identity response was invalid"
            ) from error
        if not isinstance(parsed, dict):
            raise BootstrapError(f"{issued_key.label} identity response was unexpected")
        return parsed

    def delete_access_key(self, issued_key: IssuedKey) -> None:
        # macOS AWS CLI는 file:///dev/stdin의 cli-input-json을 읽지 못하므로
        # secret이 아닌 AccessKeyId만 JSON 인자로 전달한다.
        document = json.dumps(
            {
                "UserName": issued_key.user_name,
                "AccessKeyId": issued_key.access_key_id,
            }
        )
        self.json(
            ["iam", "delete-access-key", "--cli-input-json", document],
        )


def command_succeeded(command: list[str], cwd: Path) -> bool:
    result = subprocess.run(
        command,
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        shell=False,
    )
    return result.returncode == 0


def repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        shell=False,
    )
    if result.returncode != 0:
        raise BootstrapError("run this script inside the umc-infra Git repository")
    return Path(result.stdout.strip()).resolve()


def read_env_file(path: Path, root: Path) -> str:
    expected_path = root / ".env.prod"
    if path.resolve() != expected_path.resolve():
        raise BootstrapError("env file must be the repository-root .env.prod")
    if path.is_symlink():
        raise BootstrapError(".env.prod must not be a symlink")
    try:
        file_stat = path.stat(follow_symlinks=False)
    except FileNotFoundError as error:
        raise BootstrapError(".env.prod does not exist") from error
    if not stat.S_ISREG(file_stat.st_mode):
        raise BootstrapError(".env.prod must be a regular file")
    if file_stat.st_uid != os.getuid():
        raise BootstrapError(".env.prod must be owned by the current user")
    if stat.S_IMODE(file_stat.st_mode) != 0o600:
        raise BootstrapError(".env.prod mode must be 0600")
    relative_path = path.relative_to(root)
    if not command_succeeded(
        ["git", "check-ignore", "--quiet", "--", str(relative_path)], root
    ):
        raise BootstrapError(".env.prod must be ignored by Git")
    if command_succeeded(
        ["git", "ls-files", "--error-unmatch", "--", str(relative_path)], root
    ):
        raise BootstrapError(".env.prod must not be tracked by Git")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8", newline="") as stream:
            contents = stream.read()
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise

    for key in ENV_KEYS:
        matches = re.findall(rf"^{re.escape(key)}=(.*)$", contents, flags=re.MULTILINE)
        if len(matches) != 1:
            raise BootstrapError(f".env.prod must contain exactly one {key} assignment")
        if matches[0] != "":
            raise BootstrapError(f".env.prod {key} must be empty for initial bootstrap")
    return contents


def stack_outputs(aws: AwsCli, stack_name: str) -> dict[str, str]:
    response = aws.json(
        ["cloudformation", "describe-stacks", "--stack-name", stack_name]
    )
    stacks = response.get("Stacks")
    if not isinstance(stacks, list) or len(stacks) != 1:
        raise BootstrapError(f"CloudFormation stack {stack_name} was not found")
    stack = stacks[0]
    if not isinstance(stack, dict) or stack.get("StackStatus") not in {
        "CREATE_COMPLETE",
        "UPDATE_COMPLETE",
    }:
        raise BootstrapError(f"CloudFormation stack {stack_name} is not complete")
    raw_outputs = stack.get("Outputs", [])
    if not isinstance(raw_outputs, list):
        raise BootstrapError(f"CloudFormation stack {stack_name} outputs are invalid")
    outputs: dict[str, str] = {}
    for item in raw_outputs:
        if isinstance(item, dict) and isinstance(item.get("OutputKey"), str):
            outputs[item["OutputKey"]] = str(item.get("OutputValue", ""))
    return outputs


def require_outputs(
    outputs: dict[str, str], names: tuple[str, ...], stack_name: str
) -> None:
    missing = [name for name in names if not outputs.get(name)]
    if missing:
        raise BootstrapError(
            f"CloudFormation stack {stack_name} is missing required outputs"
        )


def validate_stack_contracts(
    storage: dict[str, str],
    ses: dict[str, str],
    backup: dict[str, str],
    account_id: str,
    region: str,
) -> None:
    if storage["AppStorageBucketRegion"] != region:
        raise BootstrapError("app storage stack region mismatch")
    if storage["AppStorageBucketOwnerAccountId"] != account_id:
        raise BootstrapError("app storage stack account mismatch")
    expected_storage_bucket = f"umc-product-prod-app-storage-{account_id}-{region}"
    if storage["AppStorageBucketName"] != expected_storage_bucket:
        raise BootstrapError("app storage bucket name mismatch")
    if storage["AppStorageUserName"] != "umc-product-prod-app-storage":
        raise BootstrapError("app storage IAM user mismatch")

    if ses["SesRegion"] != region:
        raise BootstrapError("SES stack region mismatch")
    if ses["SesIdentityName"] != "university.neordinary.com":
        raise BootstrapError("SES identity mismatch")
    if ses["MailFromDomain"] != "mail.university.neordinary.com":
        raise BootstrapError("SES custom MAIL FROM domain mismatch")
    if ses["SenderEmailAddress"] != "no-reply@university.neordinary.com":
        raise BootstrapError("SES sender address mismatch")
    if ses["SesSenderUserName"] != "umc-product-prod-email-sender":
        raise BootstrapError("SES sender IAM user mismatch")

    if backup["BackupBucketRegion"] != region:
        raise BootstrapError("backup stack region mismatch")
    if backup["BackupBucketOwnerAccountId"] != account_id:
        raise BootstrapError("backup stack account mismatch")
    expected_backup_bucket = f"umc-product-prod-postgres-backup-{account_id}-{region}"
    if backup["BackupBucketName"] != expected_backup_bucket:
        raise BootstrapError("backup bucket name mismatch")
    if backup["BackupWriterUserName"] != "umc-product-postgres-backup-writer":
        raise BootstrapError("backup writer IAM user mismatch")


def validate_iam_user_has_no_keys(aws: AwsCli, user_name: str, account_id: str) -> None:
    response = aws.json(["iam", "get-user", "--user-name", user_name])
    user = response.get("User")
    expected_arn = f"arn:aws:iam::{account_id}:user/{user_name}"
    if not isinstance(user, dict) or user.get("Arn") != expected_arn:
        raise BootstrapError(f"IAM user contract mismatch for {user_name}")
    key_response = aws.json(["iam", "list-access-keys", "--user-name", user_name])
    metadata = key_response.get("AccessKeyMetadata")
    if not isinstance(metadata, list):
        raise BootstrapError(f"IAM access-key metadata is invalid for {user_name}")
    if metadata:
        raise BootstrapError(f"IAM user {user_name} already has an access key")


def validate_aws_resources(
    aws: AwsCli,
    storage: dict[str, str],
    ses: dict[str, str],
    backup: dict[str, str],
) -> None:
    for bucket_name in (storage["AppStorageBucketName"], backup["BackupBucketName"]):
        response = aws.json(["s3api", "get-bucket-location", "--bucket", bucket_name])
        if response.get("LocationConstraint") != SEOUL_REGION:
            raise BootstrapError("S3 bucket region mismatch")
    identity = aws.json(
        ["sesv2", "get-email-identity", "--email-identity", ses["SesIdentityName"]]
    )
    if identity.get("IdentityType") != "DOMAIN":
        raise BootstrapError("SES domain identity contract mismatch")
    mail_from = identity.get("MailFromAttributes")
    if not isinstance(mail_from, dict) or (
        mail_from.get("MailFromDomain") != "mail.university.neordinary.com"
        or mail_from.get("BehaviorOnMxFailure") != "REJECT_MESSAGE"
    ):
        raise BootstrapError("SES custom MAIL FROM contract mismatch")


def create_access_key(aws: AwsCli, label: str, user_name: str) -> IssuedKey:
    response = aws.json(["iam", "create-access-key", "--user-name", user_name])
    access_key = response.get("AccessKey")
    if not isinstance(access_key, dict):
        raise BootstrapError(f"{label} access-key response was invalid")
    key_id = access_key.get("AccessKeyId")
    secret = access_key.get("SecretAccessKey")
    if (
        access_key.get("UserName") != user_name
        or access_key.get("Status") != "Active"
        or not isinstance(key_id, str)
        or not isinstance(secret, str)
        or not key_id
        or not secret
    ):
        raise BootstrapError(f"{label} access-key response was incomplete")
    return IssuedKey(label, user_name, key_id, secret)


def validate_issued_key(aws: AwsCli, issued_key: IssuedKey, account_id: str) -> None:
    expected_arn = f"arn:aws:iam::{account_id}:user/{issued_key.user_name}"
    delays = (1, 2, 3, 5, 8)
    for attempt in range(len(delays) + 1):
        try:
            identity = aws.identity_with_key(issued_key)
        except BootstrapError:
            if attempt == len(delays):
                raise
            time.sleep(delays[attempt])
            continue
        if identity.get("Account") != account_id or identity.get("Arn") != expected_arn:
            raise BootstrapError(f"{issued_key.label} credential identity mismatch")
        return


def replace_env_assignments(contents: str, values: dict[str, str]) -> str:
    updated = contents
    for key in ENV_KEYS:
        value = values[key]
        if "\n" in value or "\r" in value:
            raise BootstrapError(f"generated value for {key} contains a newline")
        updated, count = re.subn(
            rf"^{re.escape(key)}=$",
            lambda _match, replacement=f"{key}={value}": replacement,
            updated,
            count=1,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise BootstrapError(f"failed to update {key}")
    return updated


def atomic_write(path: Path, contents: str) -> None:
    descriptor = -1
    temporary_path: Path | None = None
    previous_umask = os.umask(0o077)
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f"{path.name}.tmp.", dir=path.parent
        )
        temporary_path = Path(raw_path)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            descriptor = -1
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        os.umask(previous_umask)
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def install_signal_handlers() -> None:
    def interrupt(_signum: int, _frame: Any) -> None:
        raise InterruptedError("bootstrap interrupted")

    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--region", default=SEOUL_REGION)
    parser.add_argument("--expected-account-id", required=True)
    parser.add_argument("--env-file", default=".env.prod", type=Path)
    parser.add_argument("--storage-stack", default="umc-product-prod-app-storage-s3")
    parser.add_argument("--ses-stack", default="umc-product-prod-email-ses")
    parser.add_argument("--backup-stack", default="umc-product-postgres-backup-s3")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.region != SEOUL_REGION:
        raise BootstrapError(f"region must be {SEOUL_REGION}")
    if not re.fullmatch(r"\d{12}", args.expected_account_id):
        raise BootstrapError("expected account ID must contain exactly 12 digits")

    root = repo_root()
    env_path = args.env_file if args.env_file.is_absolute() else root / args.env_file
    original_contents = read_env_file(env_path, root)
    aws = AwsCli(args.profile, args.region)

    caller = aws.json(["sts", "get-caller-identity"])
    caller_arn = str(caller.get("Arn", ""))
    if caller.get("Account") != args.expected_account_id:
        raise BootstrapError(
            "authenticated AWS account does not match the expected account"
        )
    if caller_arn.endswith(":root"):
        raise BootstrapError("root AWS credentials are not allowed")

    storage = stack_outputs(aws, args.storage_stack)
    ses = stack_outputs(aws, args.ses_stack)
    backup = stack_outputs(aws, args.backup_stack)
    require_outputs(
        storage,
        (
            "AppStorageBucketName",
            "AppStorageBucketRegion",
            "AppStorageBucketOwnerAccountId",
            "AppStorageUserName",
        ),
        args.storage_stack,
    )
    require_outputs(
        ses,
        (
            "SesIdentityName",
            "SesSenderUserName",
            "SesRegion",
            "SenderEmailAddress",
            "MailFromDomain",
        ),
        args.ses_stack,
    )
    require_outputs(
        backup,
        (
            "BackupBucketName",
            "BackupBucketRegion",
            "BackupBucketOwnerAccountId",
            "BackupWriterUserName",
        ),
        args.backup_stack,
    )
    validate_stack_contracts(
        storage, ses, backup, args.expected_account_id, args.region
    )
    validate_aws_resources(aws, storage, ses, backup)

    users = (
        ("app-storage", storage["AppStorageUserName"]),
        ("app-email", ses["SesSenderUserName"]),
        ("backup-s3", backup["BackupWriterUserName"]),
    )
    for _label, user_name in users:
        validate_iam_user_has_no_keys(aws, user_name, args.expected_account_id)

    issued: list[IssuedKey] = []
    committed = False
    try:
        for label, user_name in users:
            issued_key = create_access_key(aws, label, user_name)
            issued.append(issued_key)
            validate_issued_key(aws, issued_key, args.expected_account_id)

        by_label = {item.label: item for item in issued}
        values = {
            "S3_ACCESS_KEY_ID": by_label["app-storage"].access_key_id,
            "S3_SECRET_ACCESS_KEY": by_label["app-storage"].secret_access_key,
            "SES_ACCESS_KEY_ID": by_label["app-email"].access_key_id,
            "SES_SECRET_ACCESS_KEY": by_label["app-email"].secret_access_key,
            "BACKUP_AWS_ACCESS_KEY_ID": by_label["backup-s3"].access_key_id,
            "BACKUP_AWS_SECRET_ACCESS_KEY": by_label["backup-s3"].secret_access_key,
            "BACKUP_AWS_REGION": backup["BackupBucketRegion"],
            "BACKUP_S3_BUCKET": backup["BackupBucketName"],
            "BACKUP_S3_EXPECTED_BUCKET_OWNER": backup["BackupBucketOwnerAccountId"],
        }
        updated_contents = replace_env_assignments(original_contents, values)
        atomic_write(env_path, updated_contents)
        committed = True
        fsync_directory(env_path.parent)
    except BaseException:
        if not committed:
            rollback_failed = False
            for issued_key in reversed(issued):
                try:
                    aws.delete_access_key(issued_key)
                except BootstrapError:
                    rollback_failed = True
            if rollback_failed:
                raise BootstrapError(
                    "bootstrap failed and at least one newly issued key could not be rolled back"
                )
        raise

    final_stat = env_path.stat(follow_symlinks=False)
    if stat.S_IMODE(final_stat.st_mode) != 0o600:
        raise BootstrapError(".env.prod was written but its mode is not 0600")
    print(
        "Created and validated dedicated app-storage, app-email, and backup-s3 credentials."
    )
    print(
        "Updated all 9 AWS fields in .env.prod; credential values were not displayed."
    )
    return 0


if __name__ == "__main__":
    install_signal_handlers()
    try:
        raise SystemExit(main())
    except BootstrapError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
