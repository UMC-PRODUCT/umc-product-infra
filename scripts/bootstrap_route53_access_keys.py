#!/usr/bin/env python3
"""Issue initial Route 53 IAM access keys and write them to .env.prod safely."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import stat
import sys
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
DNS_ZONE_NAME = "university.neordinary.com"
CERT_MANAGER_USER = "umc-cert-manager-route53"
EXTERNAL_DNS_USER = "umc-external-dns-route53"
ENV_KEYS = (
    "CERT_MANAGER_ROUTE53_ACCESS_KEY_ID",
    "CERT_MANAGER_ROUTE53_SECRET_ACCESS_KEY",
    "EXTERNAL_DNS_ROUTE53_ACCESS_KEY_ID",
    "EXTERNAL_DNS_ROUTE53_SECRET_ACCESS_KEY",
)
STACK_OUTPUTS = (
    "Route53HostedZoneId",
    "DnsZoneName",
    "CertManagerRoute53UserName",
    "ExternalDNSRoute53UserName",
)


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


def validate_stack_contract(outputs: dict[str, str], stack_name: str) -> None:
    require_outputs(outputs, STACK_OUTPUTS, stack_name)
    if outputs["DnsZoneName"] != DNS_ZONE_NAME:
        raise BootstrapError("Route 53 DNS zone name mismatch")
    if not re.fullmatch(r"Z[A-Z0-9]+", outputs["Route53HostedZoneId"]):
        raise BootstrapError("Route 53 hosted zone ID is invalid")
    if outputs["CertManagerRoute53UserName"] != CERT_MANAGER_USER:
        raise BootstrapError("cert-manager Route 53 IAM user mismatch")
    if outputs["ExternalDNSRoute53UserName"] != EXTERNAL_DNS_USER:
        raise BootstrapError("ExternalDNS Route 53 IAM user mismatch")


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


def rollback_attempted_users(aws: AwsCli, user_names: list[str]) -> bool:
    # Preflight established that these dedicated users had zero keys. Re-listing also
    # covers a create response that was lost after AWS accepted the request.
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
    parser.add_argument("--env-file", default=".env.prod", type=Path)
    parser.add_argument("--route53-stack", default="umc-product-route53-dns")
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
    caller_arn = caller.get("Arn")
    if caller.get("Account") != args.expected_account_id:
        raise BootstrapError(
            "authenticated AWS account does not match the expected account"
        )
    if not isinstance(caller_arn, str) or not caller_arn:
        raise BootstrapError("authenticated AWS identity is invalid")
    if caller_arn.endswith(":root"):
        raise BootstrapError("root AWS credentials are not allowed")

    outputs = stack_outputs(aws, args.route53_stack)
    validate_stack_contract(outputs, args.route53_stack)

    users = (
        ("cert-manager-route53", outputs["CertManagerRoute53UserName"]),
        ("external-dns-route53", outputs["ExternalDNSRoute53UserName"]),
    )
    for _label, user_name in users:
        validate_iam_user_has_no_keys(aws, user_name, args.expected_account_id)

    issued: list[IssuedKey] = []
    attempted_users: list[str] = []
    committed = False
    try:
        for label, user_name in users:
            with block_termination_signals():
                attempted_users.append(user_name)
                issued_key = create_access_key(aws, label, user_name)
                issued.append(issued_key)
            validate_issued_key(aws, issued_key, args.expected_account_id)

        by_label = {item.label: item for item in issued}
        values = {
            "CERT_MANAGER_ROUTE53_ACCESS_KEY_ID": by_label[
                "cert-manager-route53"
            ].access_key_id,
            "CERT_MANAGER_ROUTE53_SECRET_ACCESS_KEY": by_label[
                "cert-manager-route53"
            ].secret_access_key,
            "EXTERNAL_DNS_ROUTE53_ACCESS_KEY_ID": by_label[
                "external-dns-route53"
            ].access_key_id,
            "EXTERNAL_DNS_ROUTE53_SECRET_ACCESS_KEY": by_label[
                "external-dns-route53"
            ].secret_access_key,
        }
        updated_contents = replace_env_assignments(original_contents, values)

        # Do not overwrite edits made while AWS credentials were being issued.
        if read_env_file(env_path, root) != original_contents:
            raise BootstrapError(".env.prod changed during bootstrap")

        with block_termination_signals():
            atomic_write(env_path, updated_contents)
            committed = True
        fsync_directory(env_path.parent)
    except BaseException as error:
        if not committed:
            with block_termination_signals():
                keys_rolled_back = rollback_attempted_users(aws, attempted_users)
            if not keys_rolled_back:
                raise BootstrapError(
                    "bootstrap failed and at least one newly issued key could not be rolled back"
                ) from error
        raise

    final_stat = env_path.stat(follow_symlinks=False)
    if not stat.S_ISREG(final_stat.st_mode):
        raise BootstrapError(".env.prod was written but is not a regular file")
    if stat.S_IMODE(final_stat.st_mode) != 0o600:
        raise BootstrapError(".env.prod was written but its mode is not 0600")

    print("Created and validated dedicated cert-manager and ExternalDNS credentials.")
    print("Updated four Route 53 fields in .env.prod; credential values were not displayed.")
    return 0


if __name__ == "__main__":
    install_signal_handlers()
    try:
        raise SystemExit(main())
    except BootstrapError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
