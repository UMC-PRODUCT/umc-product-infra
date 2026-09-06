#!/usr/bin/env python3
"""Issue initial dev/preview S3 and shared nonprod SES access keys safely."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from bootstrap_aws_access_keys import (
    AwsCli,
    BootstrapError,
    IssuedKey,
    atomic_write,
    command_succeeded,
    create_access_key,
    fsync_directory,
    install_signal_handlers,
    repo_root,
    require_outputs,
    stack_outputs,
    validate_iam_user_has_no_keys,
    validate_issued_key,
)


SEOUL_REGION = "ap-northeast-2"
ENV_KEYS = (
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "SES_ACCESS_KEY_ID",
    "SES_SECRET_ACCESS_KEY",
)
ENV_FILES = (".env.dev", ".env.preview")


def read_env_file(path: Path, root: Path, expected_name: str) -> str:
    expected_path = root / expected_name
    if path.resolve() != expected_path.resolve():
        raise BootstrapError(f"env file must be the repository-root {expected_name}")
    if path.is_symlink():
        raise BootstrapError(f"{expected_name} must not be a symlink")
    try:
        file_stat = path.stat(follow_symlinks=False)
    except FileNotFoundError as error:
        raise BootstrapError(f"{expected_name} does not exist") from error
    if not stat.S_ISREG(file_stat.st_mode):
        raise BootstrapError(f"{expected_name} must be a regular file")
    if file_stat.st_uid != os.getuid():
        raise BootstrapError(f"{expected_name} must be owned by the current user")
    if stat.S_IMODE(file_stat.st_mode) != 0o600:
        raise BootstrapError(f"{expected_name} mode must be 0600")

    relative_path = path.relative_to(root)
    if not command_succeeded(
        ["git", "check-ignore", "--quiet", "--", str(relative_path)], root
    ):
        raise BootstrapError(f"{expected_name} must be ignored by Git")
    if command_succeeded(
        ["git", "ls-files", "--error-unmatch", "--", str(relative_path)], root
    ):
        raise BootstrapError(f"{expected_name} must not be tracked by Git")

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
            raise BootstrapError(
                f"{expected_name} must contain exactly one {key} assignment"
            )
        if matches[0] != "":
            raise BootstrapError(
                f"{expected_name} {key} must be empty for initial bootstrap"
            )
    return contents


def validate_storage_stack(
    outputs: dict[str, str], environment: str, account_id: str, region: str
) -> None:
    expected_bucket = f"umc-product-{environment}-app-storage-{account_id}-{region}"
    expected_user = f"umc-product-{environment}-app-storage"
    if outputs["AppStorageEnvironment"] != environment:
        raise BootstrapError(f"{environment} app storage stack environment mismatch")
    if outputs["AppStorageBucketName"] != expected_bucket:
        raise BootstrapError(f"{environment} app storage bucket name mismatch")
    if outputs["AppStorageBucketRegion"] != region:
        raise BootstrapError(f"{environment} app storage stack region mismatch")
    if outputs["AppStorageBucketOwnerAccountId"] != account_id:
        raise BootstrapError(f"{environment} app storage stack account mismatch")
    if outputs["AppStorageUserName"] != expected_user:
        raise BootstrapError(f"{environment} app storage IAM user mismatch")


def validate_ses_stack(outputs: dict[str, str], region: str) -> None:
    if outputs["SesIdentityName"] != "university.neordinary.com":
        raise BootstrapError("SES identity mismatch")
    if outputs["MailFromDomain"] != "mail.university.neordinary.com":
        raise BootstrapError("SES custom MAIL FROM domain mismatch")
    if outputs["SesRegion"] != region:
        raise BootstrapError("SES stack region mismatch")
    if outputs["NonprodSenderEmailAddress"] != "no-reply-nonprod@university.neordinary.com":
        raise BootstrapError("nonprod SES sender address mismatch")
    if outputs["NonprodSesSenderUserName"] != "umc-product-nonprod-email-sender":
        raise BootstrapError("nonprod SES sender IAM user mismatch")


def validate_aws_resources(
    aws: AwsCli,
    dev_storage: dict[str, str],
    preview_storage: dict[str, str],
    ses: dict[str, str],
) -> None:
    for environment, outputs in (
        ("dev", dev_storage),
        ("preview", preview_storage),
    ):
        response = aws.json(
            ["s3api", "get-bucket-location", "--bucket", outputs["AppStorageBucketName"]]
        )
        if response.get("LocationConstraint") != SEOUL_REGION:
            raise BootstrapError(f"{environment} S3 bucket region mismatch")

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


def stage_env_file(path: Path, contents: str) -> Path:
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
        return temporary_path
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    finally:
        os.umask(previous_umask)
        if descriptor >= 0:
            os.close(descriptor)


@contextmanager
def block_termination_signals() -> Iterator[None]:
    signals = {signal.SIGINT, signal.SIGTERM}
    if not hasattr(signal, "pthread_sigmask"):
        yield
        return
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, signals)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def restore_env_files(originals: dict[Path, str], paths: list[Path]) -> bool:
    failed = False
    for path in reversed(paths):
        try:
            atomic_write(path, originals[path])
            fsync_directory(path.parent)
        except BaseException:
            failed = True
    return not failed


def rollback_attempted_users(aws: AwsCli, user_names: list[str]) -> bool:
    # 모든 사용자는 최초 preflight에서 access key 0개임을 확인한 전용 사용자다.
    # create 응답이 유실된 경우도 정리할 수 있도록 시도한 사용자의 key를 재조회한다.
    failed = False
    for user_name in reversed(user_names):
        try:
            response = aws.json(["iam", "list-access-keys", "--user-name", user_name])
            metadata = response.get("AccessKeyMetadata")
            if not isinstance(metadata, list):
                failed = True
                continue
            for item in metadata:
                if not isinstance(item, dict) or item.get("UserName") != user_name:
                    failed = True
                    continue
                access_key_id = item.get("AccessKeyId")
                if not isinstance(access_key_id, str) or not access_key_id:
                    failed = True
                    continue
                document = json.dumps(
                    {"UserName": user_name, "AccessKeyId": access_key_id}
                )
                aws.json(
                    ["iam", "delete-access-key", "--cli-input-json", document]
                )
        except BaseException:
            failed = True
    return not failed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--region", default=SEOUL_REGION)
    parser.add_argument("--expected-account-id", required=True)
    parser.add_argument(
        "--dev-storage-stack", default="umc-product-dev-app-storage-s3"
    )
    parser.add_argument(
        "--preview-storage-stack", default="umc-product-preview-app-storage-s3"
    )
    parser.add_argument("--ses-stack", default="umc-product-prod-email-ses")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.region != SEOUL_REGION:
        raise BootstrapError(f"region must be {SEOUL_REGION}")
    if not re.fullmatch(r"\d{12}", args.expected_account_id):
        raise BootstrapError("expected account ID must contain exactly 12 digits")

    root = repo_root()
    env_paths = {name: root / name for name in ENV_FILES}
    originals = {
        path: read_env_file(path, root, name) for name, path in env_paths.items()
    }

    aws = AwsCli(args.profile, args.region)
    caller = aws.json(["sts", "get-caller-identity"])
    caller_arn = str(caller.get("Arn", ""))
    if caller.get("Account") != args.expected_account_id:
        raise BootstrapError(
            "authenticated AWS account does not match the expected account"
        )
    if caller_arn.endswith(":root"):
        raise BootstrapError("root AWS credentials are not allowed")

    dev_storage = stack_outputs(aws, args.dev_storage_stack)
    preview_storage = stack_outputs(aws, args.preview_storage_stack)
    ses = stack_outputs(aws, args.ses_stack)
    storage_outputs = (
        "AppStorageEnvironment",
        "AppStorageBucketName",
        "AppStorageBucketRegion",
        "AppStorageBucketOwnerAccountId",
        "AppStorageUserName",
    )
    require_outputs(dev_storage, storage_outputs, args.dev_storage_stack)
    require_outputs(preview_storage, storage_outputs, args.preview_storage_stack)
    require_outputs(
        ses,
        (
            "SesIdentityName",
            "SesRegion",
            "MailFromDomain",
            "NonprodSenderEmailAddress",
            "NonprodSesSenderUserName",
        ),
        args.ses_stack,
    )
    validate_storage_stack(
        dev_storage, "dev", args.expected_account_id, args.region
    )
    validate_storage_stack(
        preview_storage, "preview", args.expected_account_id, args.region
    )
    validate_ses_stack(ses, args.region)
    validate_aws_resources(aws, dev_storage, preview_storage, ses)

    users = (
        ("dev-app-storage", dev_storage["AppStorageUserName"]),
        ("preview-app-storage", preview_storage["AppStorageUserName"]),
        ("nonprod-app-email", ses["NonprodSesSenderUserName"]),
    )
    for _label, user_name in users:
        validate_iam_user_has_no_keys(aws, user_name, args.expected_account_id)

    issued: list[IssuedKey] = []
    attempted_users: list[str] = []
    replaced_paths: list[Path] = []
    staged_paths: dict[Path, Path] = {}
    committed = False
    try:
        for label, user_name in users:
            with block_termination_signals():
                attempted_users.append(user_name)
                issued_key = create_access_key(aws, label, user_name)
                issued.append(issued_key)
            validate_issued_key(aws, issued_key, args.expected_account_id)

        by_label = {item.label: item for item in issued}
        shared_ses = by_label["nonprod-app-email"]
        values_by_path = {
            env_paths[".env.dev"]: {
                "S3_ACCESS_KEY_ID": by_label["dev-app-storage"].access_key_id,
                "S3_SECRET_ACCESS_KEY": by_label["dev-app-storage"].secret_access_key,
                "SES_ACCESS_KEY_ID": shared_ses.access_key_id,
                "SES_SECRET_ACCESS_KEY": shared_ses.secret_access_key,
            },
            env_paths[".env.preview"]: {
                "S3_ACCESS_KEY_ID": by_label["preview-app-storage"].access_key_id,
                "S3_SECRET_ACCESS_KEY": by_label[
                    "preview-app-storage"
                ].secret_access_key,
                "SES_ACCESS_KEY_ID": shared_ses.access_key_id,
                "SES_SECRET_ACCESS_KEY": shared_ses.secret_access_key,
            },
        }
        updated = {
            path: replace_env_assignments(originals[path], values)
            for path, values in values_by_path.items()
        }
        for path in (env_paths[".env.dev"], env_paths[".env.preview"]):
            staged_paths[path] = stage_env_file(path, updated[path])

        # AWS 작업 중 사람이 원장을 수정했다면 오래된 snapshot으로 덮어쓰지 않는다.
        for name, path in env_paths.items():
            if read_env_file(path, root, name) != originals[path]:
                raise BootstrapError(f"{name} changed during bootstrap")

        for path in (env_paths[".env.dev"], env_paths[".env.preview"]):
            with block_termination_signals():
                os.replace(staged_paths[path], path)
                replaced_paths.append(path)
                del staged_paths[path]
        fsync_directory(root)

        for name, path in env_paths.items():
            final_stat = path.stat(follow_symlinks=False)
            if stat.S_IMODE(final_stat.st_mode) != 0o600:
                raise BootstrapError(f"{name} was written but its mode is not 0600")
        committed = True
    except BaseException as error:
        if not committed:
            with block_termination_signals():
                files_restored = restore_env_files(originals, replaced_paths)
                keys_rolled_back = rollback_attempted_users(aws, attempted_users)
            if not files_restored or not keys_rolled_back:
                raise BootstrapError(
                    "bootstrap failed and automatic rollback was incomplete"
                ) from error
        raise
    finally:
        for path in staged_paths.values():
            path.unlink(missing_ok=True)

    print("Created and validated dedicated dev/preview S3 credentials.")
    print("Created and validated one shared nonprod SES credential.")
    print(
        "Updated S3 and SES fields in .env.dev and .env.preview; credential values were not displayed."
    )
    return 0


if __name__ == "__main__":
    install_signal_handlers()
    try:
        raise SystemExit(main())
    except BootstrapError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
