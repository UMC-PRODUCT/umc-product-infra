from __future__ import annotations

import ast
import configparser
import re
import subprocess
import sys
import unittest
from pathlib import Path
from urllib.parse import quote

import yaml

try:
    from ansible.parsing.dataloader import DataLoader
    from ansible.template import Templar, trust_as_template
except ImportError:
    Templar = None


ROOT = Path(__file__).resolve().parents[2]

requires_ansible = unittest.skipIf(
    Templar is None, "Install ansible/requirements.txt to verify the Ansible runtime"
)


def trusted(value: object) -> object:
    # Ansible은 role 파일의 문자열을 template으로 신뢰한다. YAML 직접 로드 값도 같게 맞춘다.
    if isinstance(value, dict):
        return {key: trusted(item) for key, item in value.items()}
    if isinstance(value, list):
        return [trusted(item) for item in value]
    return trust_as_template(value) if isinstance(value, str) else value


def render(template: object, variables: dict[str, object]) -> object:
    return Templar(loader=DataLoader(), variables=variables).template(trusted(template))


def passes(conditions: str | list[str], variables: dict[str, object]) -> bool:
    """Evaluate an assert `that` list or a `failed_when` expression the way Ansible does."""
    templar = Templar(loader=DataLoader(), variables=variables)
    if isinstance(conditions, str):
        conditions = [conditions]
    return all(templar.evaluate_conditional(trust_as_template(condition)) for condition in conditions)


def load_tasks(relative_path: str) -> list[dict[str, object]]:
    with (ROOT / relative_path).open(encoding="utf-8") as stream:
        tasks = yaml.safe_load(stream)
    if not isinstance(tasks, list):
        raise AssertionError(f"{relative_path} must contain an Ansible task list")
    return tasks


def command_argv(task: dict[str, object]) -> list[str]:
    command = task.get("ansible.builtin.command")
    if not isinstance(command, dict):
        return []
    argv = command.get("argv")
    return argv if isinstance(argv, list) else []


def leaf_tasks(tasks: list[dict[str, object]], no_log: bool | None = None) -> list[dict[str, object]]:
    """Tasks inside block/rescue/always, flattened. A block's no_log applies to its children."""
    found: list[dict[str, object]] = []
    for task in tasks:
        sections = [section for section in ("block", "rescue", "always") if section in task]
        if not sections:
            found.append(task if no_log is None or "no_log" in task else {**task, "no_log": no_log})
            continue
        inherited = task.get("no_log", no_log)
        for section in sections:
            found += leaf_tasks(task[section], inherited)
    return found


def all_tasks() -> list[tuple[str, dict[str, object]]]:
    """Every task and handler in the roles and playbooks, with its source path."""
    found: list[tuple[str, dict[str, object]]] = []
    paths = sorted((ROOT / "ansible" / "roles").glob("*/tasks/*.yml"))
    paths += sorted((ROOT / "ansible" / "roles").glob("*/handlers/*.yml"))
    for path in paths:
        relative = str(path.relative_to(ROOT))
        found += [(relative, task) for task in leaf_tasks(load_tasks(relative))]
    for path in sorted((ROOT / "ansible" / "playbooks").glob("*.yml")):
        relative = str(path.relative_to(ROOT))
        for play in yaml.safe_load(path.read_text(encoding="utf-8")):
            for section in ("pre_tasks", "tasks", "post_tasks", "handlers"):
                found += [(relative, task) for task in leaf_tasks(play.get(section, []))]
    return found


# 기본 운영 경로와 CI의 공개 예제 경로를 구분해 검증이 실제 inventory에 의존하지 않게 한다.
class AnsibleInventoryContractTests(unittest.TestCase):
    def test_default_inventory_is_the_current_cafe24_inventory(self) -> None:
        config = configparser.ConfigParser(interpolation=None)
        config.read(ROOT / "ansible" / "ansible.cfg", encoding="utf-8")
        self.assertEqual(config["defaults"]["inventory"], "inventories/idc-new/hosts.yml")

    def test_repo_local_collections_are_searched_first(self) -> None:
        config = configparser.ConfigParser(interpolation=None)
        config.read(ROOT / "ansible" / "ansible.cfg", encoding="utf-8")
        paths = config["defaults"]["collections_path"].split(":")
        self.assertEqual(paths[0], "collections")
        self.assertIn("~/.ansible/collections", paths)
        self.assertTrue((ROOT / "ansible" / "collections" / "requirements.yml").is_file())
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("ansible/collections/ansible_collections/", ignored)

    def test_validation_uses_the_remaining_safe_example_inventory(self) -> None:
        inventories = ROOT / "ansible" / "inventories"
        self.assertFalse((inventories / "idc" / "hosts.example.yml").exists())
        self.assertTrue((inventories / "idc-new" / "hosts.example.yml").is_file())
        validation = (ROOT / "scripts" / "validate.sh").read_text(encoding="utf-8")
        self.assertIn("-i inventories/idc-new/hosts.example.yml", validation)


# 설치 스크립트도 선택한 K3s release와 checksum에 고정돼야 재실행 결과가 흔들리지 않는다.
class K3sInstallerContractTests(unittest.TestCase):
    def test_installer_is_pinned_to_the_selected_release_tag(self) -> None:
        with (
            ROOT / "ansible" / "roles" / "k3s" / "defaults" / "main.yml"
        ).open(encoding="utf-8") as stream:
            defaults = yaml.safe_load(stream)

        version = defaults["k3s_version"]
        expected_url = (
            "https://raw.githubusercontent.com/k3s-io/k3s/"
            f"{quote(version, safe='')}/install.sh"
        )
        self.assertEqual(defaults["k3s_install_script_url"], expected_url)
        self.assertRegex(
            defaults["k3s_install_script_checksum"], r"^sha256:[0-9a-f]{64}$"
        )
        self.assertRegex(defaults["k3s_binary_checksum"], r"^[0-9a-f]{64}$")

    @requires_ansible
    def test_installer_and_binary_checksums_fail_closed(self) -> None:
        tasks = load_tasks("ansible/roles/k3s/tasks/main.yml")
        names = [task["name"] for task in tasks]
        pin_guard = tasks[names.index("Require pinned SHA-256 values for the K3s installer and binary")]
        download = tasks[names.index("Download the K3s installer")]
        install = tasks[names.index("Install or upgrade the pinned K3s version")]
        binary_guard = tasks[names.index("Require the pinned K3s binary")]
        start = tasks[names.index("Ensure K3s starts at boot")]
        self.assertLess(names.index(pin_guard["name"]), names.index(download["name"]))
        self.assertEqual(download["ansible.builtin.get_url"]["checksum"], "{{ k3s_install_script_checksum }}")
        self.assertLess(names.index(install["name"]), names.index(binary_guard["name"]))
        self.assertLess(names.index(binary_guard["name"]), names.index(start["name"]))
        inspect = next(task for task in tasks
                       if task.get("ansible.builtin.stat", {}).get("path") == "/usr/local/bin/k3s")
        self.assertEqual(inspect["ansible.builtin.stat"]["checksum_algorithm"], "sha256")

        pinned = "sha256:" + "a" * 64
        for installer, binary, accepted in (
            (pinned, "b" * 64, True),
            ("", "b" * 64, False),
            (pinned, "", False),
            ("a" * 64, "b" * 64, False),
            (pinned, "sha256:" + "b" * 64, False),
            (pinned.upper(), "b" * 64, False),
        ):
            with self.subTest(installer=installer, binary=binary):
                self.assertIs(passes(pin_guard["ansible.builtin.assert"]["that"], {
                    "k3s_install_script_checksum": installer, "k3s_binary_checksum": binary,
                }), accepted)

        for checksum, accepted in (("b" * 64, True), ("c" * 64, False)):
            self.assertIs(passes(binary_guard["ansible.builtin.assert"]["that"], {
                "k3s_binary": {"stat": {"exists": True, "checksum": checksum}},
                "k3s_binary_checksum": "b" * 64,
            }), accepted)


# Traefik 생성 → rollout → Service 검증 순서와 유한한 대기를 유지한다.
class K3sReadinessContractTests(unittest.TestCase):
    def test_traefik_creation_wait_precedes_the_bounded_rollout_wait(self) -> None:
        tasks = load_tasks("ansible/roles/k3s/tasks/main.yml")
        rollout_index = next(
            index
            for index, task in enumerate(tasks)
            if "rollout" in command_argv(task)
            and "deployment/traefik" in command_argv(task)
        )

        self.assertGreater(rollout_index, 0)
        creation_wait = tasks[rollout_index - 1]
        self.assertEqual(
            command_argv(creation_wait),
            [
                "/usr/local/bin/k3s",
                "kubectl",
                "wait",
                "--for=create",
                "deployment/traefik",
                "-n",
                "kube-system",
                "--timeout={{ k3s_ready_timeout }}",
            ],
        )
        self.assertFalse(creation_wait["changed_when"])
        self.assertIn(
            "--timeout={{ k3s_ready_timeout }}", command_argv(tasks[rollout_index])
        )

    def test_traefik_service_validation_is_bounded_and_follows_rollout(self) -> None:
        tasks = load_tasks("ansible/roles/k3s/tasks/main.yml")
        service_wait = next(
            task for task in tasks if task.get("register") == "k3s_traefik_service"
        )
        previous = tasks[tasks.index(service_wait) - 1]
        self.assertIn("rollout", command_argv(previous))
        self.assertIn("deployment/traefik", command_argv(previous))
        self.assertEqual(
            command_argv(service_wait),
            [
                "/usr/local/bin/k3s", "kubectl", "-n", "kube-system",
                "get", "svc", "traefik", "-o", "json",
            ],
        )
        self.assertFalse(service_wait["changed_when"])
        self.assertEqual(service_wait["retries"], 60)
        self.assertEqual(service_wait["delay"], 5)
        condition = " ".join(service_wait["until"].split())
        self.assertTrue(
            condition.startswith(
                "k3s_traefik_service.rc == 0 and "
                "k3s_traefik_service.stdout | trim | length > 0 and "
            )
        )
        for required in (
            "get('type') == 'LoadBalancer'",
            "get('allocateLoadBalancerNodePorts', true) is false",
            "| map(attribute='port') | list) == [443]",
            "| selectattr('nodePort', 'defined') | list | length) == 0",
        ):
            self.assertIn(required, condition)


# bootstrap 리소스 소유권과 Secret 표현을 고정하고 회전 시 controller 상태 확인을 생략하지 않는다.
class KubernetesApplyContractTests(unittest.TestCase):
    def test_bootstrap_namespace_apply_uses_server_side_ownership(self) -> None:
        for relative_path in (
            "ansible/roles/argocd/tasks/main.yml",
            "ansible/roles/external_secrets_bootstrap/tasks/main.yml",
        ):
            tasks = load_tasks(relative_path)
            namespace_applies = [
                task
                for task in tasks
                if "namespace" in str(task.get("name", "")).lower()
                and "apply" in command_argv(task)
            ]
            self.assertTrue(namespace_applies, relative_path)
            for task in namespace_applies:
                self.assertIn("--server-side=true", command_argv(task), task["name"])

    @requires_ansible
    def test_argocd_namespace_psa_version_follows_k3s_and_never_enforces(self) -> None:
        tasks = load_tasks("ansible/roles/argocd/tasks/main.yml")
        namespace_tasks = [task for task in tasks if "namespace" in task["name"].lower()
                           and command_argv(task)[2:3] in (["diff"], ["apply"])]
        self.assertEqual(
            [task["ansible.builtin.command"]["stdin"] for task in namespace_tasks],
            ["{{ argocd_namespace_manifest | to_json }}"] * 2,
        )
        self.assertNotIn("v1.36", (ROOT / "ansible/roles/argocd/tasks/main.yml").read_text(encoding="utf-8"))

        role_vars = trusted(yaml.safe_load((ROOT / "ansible/roles/argocd/vars/main.yml").read_text(encoding="utf-8")))
        k3s_version = yaml.safe_load(
            (ROOT / "ansible/roles/k3s/defaults/main.yml").read_text(encoding="utf-8")
        )["k3s_version"]
        expected_minor = ".".join(k3s_version.split(".")[:2])
        for version, minor in ((k3s_version, expected_minor), ("v1.37.0+k3s1", "v1.37")):
            with self.subTest(version=version):
                manifest = render("{{ argocd_namespace_manifest }}", {
                    **role_vars, "k3s_version": version, "argocd_namespace": "argocd",
                })
                labels = manifest["metadata"]["labels"]
                self.assertEqual(manifest["metadata"]["name"], "argocd")
                self.assertEqual(labels, {
                    "pod-security.kubernetes.io/audit": "restricted",
                    "pod-security.kubernetes.io/audit-version": minor,
                    "pod-security.kubernetes.io/warn": "restricted",
                    "pod-security.kubernetes.io/warn-version": minor,
                })

    def test_git_fixed_bootstrap_names_are_asserted(self) -> None:
        argocd_assert = load_tasks(
            "ansible/roles/argocd/tasks/main.yml"
        )[0]["ansible.builtin.assert"]["that"]
        self.assertIn("argocd_namespace == 'argocd'", argocd_assert)
        self.assertIn("argocd_helm_release_name == 'argocd'", argocd_assert)

        eso_assert = load_tasks(
            "ansible/roles/external_secrets_bootstrap/tasks/main.yml"
        )[0]["ansible.builtin.assert"]["that"]
        self.assertIn(
            "external_secrets_bootstrap_namespace == 'external-secrets'",
            eso_assert,
        )
        self.assertIn(
            "external_secrets_bootstrap_secret_name == 'aws-bootstrap'",
            eso_assert,
        )

    def test_secret_server_side_apply_uses_data_not_string_data(self) -> None:
        path = (
            ROOT
            / "ansible"
            / "roles"
            / "external_secrets_bootstrap"
            / "tasks"
            / "main.yml"
        )
        text = path.read_text(encoding="utf-8")

        self.assertNotIn("stringData:", text)
        self.assertEqual(len(re.findall(r"\| b64encode", text)), 4)

    def test_rotation_handler_checks_the_current_controller_state(self) -> None:
        handlers = load_tasks(
            "ansible/roles/external_secrets_bootstrap/handlers/main.yml"
        )
        controller_check = handlers[0]

        self.assertIn("--ignore-not-found", command_argv(controller_check))
        self.assertNotIn("failed_when", controller_check)
        self.assertIn("register", controller_check)


# Kubernetes args 처리 후에도 PL/pgSQL dollar-quote 구분자가 보존돼야 초기화 SQL이 실행된다.
class PostgresBootstrapContractTests(unittest.TestCase):
    def test_plpgsql_dollar_quotes_survive_kubernetes_arg_expansion(self) -> None:
        for relative_path in (
            "manifests/postgres/prod/app-role-job.yaml",
            "manifests/postgres/prod/readonly-role-job.yaml",
            "manifests/postgres/dev/app-role-job.yaml",
            "manifests/postgres/preview/app-role-job.yaml",
        ):
            text = (ROOT / relative_path).read_text(encoding="utf-8")

            self.assertNotRegex(text, r"(?m)^\s*DO \$\$\s*$", relative_path)
            self.assertNotRegex(text, r"(?m)^\s*\$\$;\s*$", relative_path)
            self.assertRegex(text, r"(?m)^\s*DO \$role_check\$\s*$", relative_path)
            self.assertRegex(text, r"(?m)^\s*\$role_check\$;\s*$", relative_path)


# 개인 관리자 검증 전 접근을 차단하지 않고, 터널 전용 계정·회수·방화벽 제한이 함께 유지돼야 한다.
class PublicSshBootstrapContractTests(unittest.TestCase):
    def test_standard_bootstrap_prepares_personal_ssh_before_common(self) -> None:
        path = ROOT / "ansible" / "playbooks" / "bootstrap.yml"
        play = yaml.safe_load(path.read_text(encoding="utf-8"))[0]

        self.assertEqual(
            [role["role"] for role in play["roles"]],
            ["ssh_access", "common", "k3s", "argocd", "external_secrets_bootstrap"],
        )
        text = path.read_text(encoding="utf-8")
        self.assertIn("ssh_access_finalize | default(false) | bool", text)
        self.assertNotIn("common_public_tcp_ports", text)

    def test_access_only_playbook_does_not_reinstall_the_cluster(self) -> None:
        play = yaml.safe_load(
            (ROOT / "ansible/playbooks/ssh-access.yml").read_text(encoding="utf-8")
        )[0]
        self.assertEqual([role["role"] for role in play["roles"]], ["ssh_access"])
        tasks = play["tasks"]
        firewall = next(
            task for task in tasks
            if task.get("ansible.builtin.include_role", {}).get("name") == "common"
        )
        self.assertEqual(firewall["ansible.builtin.include_role"]["tasks_from"], "firewall.yml")
        self.assertEqual(
            [task["ansible.builtin.include_role"] for task in tasks
             if "ansible.builtin.include_role" in task],
            [{"name": "common", "tasks_from": "firewall.yml"}],
        )
        reset = next(task for task in tasks
                     if task.get("ansible.builtin.meta") == "reset_connection")
        reconnect = next(task for task in tasks if "ansible.builtin.wait_for_connection" in task)
        sudo = next(task for task in tasks if task.get("ansible.builtin.command") == "sudo -n true")
        private_ports = next(task for task in tasks if "ansible.builtin.wait_for" in task)
        self.assertLess(tasks.index(firewall), tasks.index(reset))
        self.assertLess(tasks.index(reset), tasks.index(reconnect))
        self.assertLess(tasks.index(reconnect), tasks.index(sudo))
        self.assertLess(tasks.index(sudo), tasks.index(private_ports))
        self.assertIs(sudo["become"], False)
        self.assertEqual(private_ports["loop"], [5432, 6443])
        self.assertEqual(private_ports["ansible.builtin.wait_for"]["host"], "{{ ssh_access_public_host }}")
        self.assertEqual(private_ports["ansible.builtin.wait_for"]["state"], "stopped")
        self.assertEqual(private_ports["delegate_to"], "localhost")

    def test_key_only_authentication_and_tunnel_only_sessions_are_explicit(self) -> None:
        template = (
            ROOT / "ansible/roles/ssh_access/templates/sshd.conf.j2"
        ).read_text(encoding="utf-8")
        for setting in (
            "AuthenticationMethods publickey",
            "PasswordAuthentication no",
            "KbdInteractiveAuthentication no",
            "PermitEmptyPasswords no",
            "AuthorizedKeysFile /etc/ssh/authorized_keys/%u",
            "AllowTcpForwarding local",
            "AllowStreamLocalForwarding no",
            "AllowAgentForwarding no",
            "PermitTunnel no",
            "PermitUserRC no",
            "GatewayPorts no",
            "PermitOpen none",
            "MaxSessions 0",
            "PermitTTY no",
            "PermitOpen {{ account.permit_open | join(' ') }}",
            "ClientAliveInterval 60",
            "ClientAliveCountMax 3",
        ):
            self.assertIn(setting, template)
        global_section = template.split("Match ", 1)[0]
        self.assertIn("ClientAliveInterval 60", global_section)
        self.assertIn("ClientAliveCountMax 3", global_section)
        self.assertIn("PermitRootLogin {{ 'no' if ssh_access_finalize else 'prohibit-password' }}", template)
        self.assertIn("AllowUsers {{ ssh_access_present", template)
        self.assertIn("MaxStartups ", template)

        tasks = load_tasks("ansible/roles/ssh_access/tasks/main.yml")
        keys = next(task["ansible.builtin.copy"] for task in tasks
                    if task.get("ansible.builtin.copy", {}).get("dest")
                    == "/etc/ssh/authorized_keys/{{ item.name }}")
        self.assertEqual((keys["owner"], keys["group"], keys["mode"]), ("root", "root", "0644"))
        self.assertEqual(keys["content"], "{{ item.public_keys | join('\n') }}\n")
        config = next(task["ansible.builtin.template"] for task in tasks
                      if "ansible.builtin.template" in task)
        self.assertEqual(config["validate"], "/usr/sbin/sshd -t -f %s")

    def test_finalization_requires_the_actual_public_personal_admin_and_sudo(self) -> None:
        tasks = load_tasks("ansible/roles/ssh_access/tasks/main.yml")
        guard = next(task for task in tasks if task.get("name")
                     == "Require actual public-key administrator access before finalization")
        conditions = guard["ansible.builtin.assert"]["that"]
        self.assertIn("ssh_access_connection.stdout.split()[2] == ssh_access_public_host", conditions)
        self.assertIn("ansible_host == ssh_access_public_host", conditions)
        self.assertIn("ansible_user != 'root'", conditions)
        self.assertTrue(any("selectattr('role', 'equalto', 'admin')" in expression for expression in conditions))
        self.assertEqual(guard["when"], "ssh_access_finalize")
        sudo = next(task for task in tasks if task.get("ansible.builtin.command") == "sudo -n true")
        mark = next(task for task in tasks
                    if task.get("ansible.builtin.set_fact", {}).get("ssh_access_public_verified") is True)
        self.assertLess(tasks.index(guard), tasks.index(sudo))
        self.assertLess(tasks.index(sudo), tasks.index(mark))
        self.assertEqual(mark["when"], "ssh_access_finalize")

    @requires_ansible
    def test_tunnel_accounts_get_nologin_and_the_stored_shell_is_asserted(self) -> None:
        role_vars = yaml.safe_load(
            (ROOT / "ansible/roles/ssh_access/vars/main.yml").read_text(encoding="utf-8")
        )
        tasks = load_tasks("ansible/roles/ssh_access/tasks/main.yml")
        user_index = next(index for index, task in enumerate(tasks) if "ansible.builtin.user" in task)
        user = tasks[user_index]["ansible.builtin.user"]
        guard_index = next(index for index, task in enumerate(tasks) if task.get("name")
                           == "Require each account's login shell to match its role")
        reread_index = next(index for index, task in enumerate(tasks[user_index:], user_index)
                            if "ansible.builtin.getent" in task)
        config_index = next(index for index, task in enumerate(tasks) if "ansible.builtin.template" in task)
        self.assertLess(user_index, reread_index)
        self.assertLess(reread_index, guard_index)
        self.assertLess(guard_index, config_index)
        guard = tasks[guard_index]
        self.assertEqual(guard["when"], "not ansible_check_mode")

        for role, shell, group in (
            ("admin", "/bin/bash", "umc-ssh-admins"),
            ("db_tunnel", "/usr/sbin/nologin", "umc-db-tunnel"),
        ):
            with self.subTest(role=role):
                item = {"name": "member", "role": role}
                self.assertEqual(render(user["shell"], {**role_vars, "item": item}), shell)
                self.assertEqual(render(user["groups"], {**role_vars, "item": item}), group)
                for stored, accepted in ((shell, True), ("/bin/sh", False)):
                    passwd = {"member": ["x", "1001", "1001", "", "/home/member", stored]}
                    self.assertIs(passes(guard["ansible.builtin.assert"]["that"], {
                        **role_vars, "item": item, "ansible_facts": {"getent_passwd": passwd},
                    }), accepted)

    @requires_ansible
    def test_sudo_and_tunnel_policy_follow_the_role_table(self) -> None:
        tasks = load_tasks("ansible/roles/ssh_access/tasks/main.yml")
        sudoers = next(task["ansible.builtin.copy"] for task in tasks
                       if task.get("name") == "Grant sudo only to the personal administrator group")
        keys_guard = next(task for task in tasks if task.get("name")
                          == "Require public keys and narrowly scoped tunnel destinations")
        template = (ROOT / "ansible/roles/ssh_access/templates/sshd.conf.j2").read_text(encoding="utf-8")
        role_vars = yaml.safe_load(
            (ROOT / "ansible/roles/ssh_access/vars/main.yml").read_text(encoding="utf-8")
        )
        table = role_vars["ssh_access_role_accounts"]
        self.assertIs(table["admin"]["tunnel_only"], False)
        self.assertIs(table["db_tunnel"]["tunnel_only"], True)

        renamed = {**table, "admin": {**table["admin"], "group": "umc-operators"},
                   "metrics_tunnel": {**table["db_tunnel"], "group": "umc-metrics-tunnel"}}
        variables = {"ssh_access_role_accounts": renamed}
        self.assertEqual(render(sudoers["content"], variables), "%umc-operators ALL=(ALL:ALL) NOPASSWD: ALL\n")
        self.assertEqual(render(sudoers["dest"], variables), "/etc/sudoers.d/umc-operators")

        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample member@example.com"
        metrics = {"name": "metrics1", "role": "metrics_tunnel", "public_keys": [key],
                   "permit_open": ["10.43.0.20:5432"]}
        admin = {"name": "admin1", "role": "admin", "public_keys": [key]}
        for account, accepted in (
            (metrics, True),
            ({**metrics, "permit_open": []}, False),
            ({**metrics, "permit_open": ["10.43.0.20:*"]}, False),
            (admin, True),
        ):
            with self.subTest(account=account):
                self.assertIs(passes(keys_guard["ansible.builtin.assert"]["that"],
                                     {**variables, "item": account}), accepted)

        config = render(template, {
            **variables, "ssh_access_present": [admin, metrics],
            "ssh_access_finalize": True, "ansible_user": "admin1",
        })
        blocks = {block.split("\n", 1)[0]: block for block in config.split("Match User ")[1:]}
        self.assertIn("MaxSessions 0", blocks["metrics1"])
        self.assertIn("PermitOpen 10.43.0.20:5432", blocks["metrics1"])
        self.assertNotIn("MaxSessions 0", blocks["admin1"])
        self.assertIn("PermitOpen any", blocks["admin1"])

    @requires_ansible
    def test_finalization_reports_legacy_root_keys_by_fingerprint_only(self) -> None:
        root_keys = "/root/.ssh/authorized_keys"
        tasks = load_tasks("ansible/roles/ssh_access/tasks/main.yml")
        touching = [task for task in tasks if root_keys in yaml.safe_dump(task)]
        self.assertEqual(
            [next(key for key in task if key.startswith("ansible.builtin.")) for task in touching],
            ["ansible.builtin.stat", "ansible.builtin.command", "ansible.builtin.debug"],
        )
        self.assertEqual(command_argv(touching[1]), ["/usr/bin/ssh-keygen", "-l", "-f", root_keys])
        self.assertIs(touching[1]["changed_when"], False)
        for task in touching:
            self.assertTrue(str(task["when"]).startswith("ssh_access_finalize"), task["name"])

        report = touching[2]
        output = [
            "256 SHA256:PIU7kEeV/eO59t8CMmQf/WCvXhy2b2xnBmdoCAdNVEI old-admin@example.com (ED25519)",
            "3072 SHA256:68XPXYydcw02MAhzdQqGX46sIUWB4wQhjiPyRRGcBms provider-console (RSA)",
        ]
        for registered, expected in (
            ({"stdout_lines": output}, ["SHA256:PIU7kEeV/eO59t8CMmQf/WCvXhy2b2xnBmdoCAdNVEI",
                                        "SHA256:68XPXYydcw02MAhzdQqGX46sIUWB4wQhjiPyRRGcBms"]),
            ({"skipped": True}, []),
        ):
            with self.subTest(registered=registered):
                fingerprints = render(report["vars"]["ssh_access_root_fingerprints"], {
                    "ssh_access_root_key_fingerprints": registered,
                })
                self.assertEqual(fingerprints, expected)
                message = render(report["ansible.builtin.debug"]["msg"], {
                    "ssh_access_root_fingerprints": fingerprints,
                })
                self.assertNotIn("example.com", message)
                self.assertNotIn("provider-console", message)
                for fingerprint in expected:
                    self.assertIn(fingerprint, message)
                self.assertEqual("No legacy keys" in message, not expected)

    def test_explicit_account_retirement_terminates_existing_tunnels(self) -> None:
        tasks = load_tasks("ansible/roles/ssh_access/tasks/revoke.yml")
        revoke_keys = next(task for task in tasks if "ansible.builtin.file" in task)
        kill = next(task for task in tasks if "pkill" in command_argv(task))
        remove = next(task for task in tasks if "ansible.builtin.user" in task)
        self.assertEqual(revoke_keys["ansible.builtin.file"]["state"], "absent")
        self.assertIn("-u", command_argv(kill))
        self.assertLess(tasks.index(revoke_keys), tasks.index(kill))
        self.assertLess(tasks.index(kill), tasks.index(remove))
        self.assertEqual(remove["ansible.builtin.user"]["state"], "absent")
        self.assertFalse(remove["ansible.builtin.user"]["remove"])
        self.assertFalse(remove["ansible.builtin.user"]["force"])

    def test_firewall_limits_ssh_and_removes_undeclared_allow_rules(self) -> None:
        tasks = load_tasks("ansible/roles/common/tasks/firewall.yml")
        defaults = yaml.safe_load(
            (ROOT / "ansible/roles/common/defaults/main.yml").read_text(encoding="utf-8")
        )
        self.assertNotIn("common_public_tcp_ports", defaults)
        k3s_defaults = yaml.safe_load(
            (ROOT / "ansible/roles/k3s/defaults/main.yml").read_text(encoding="utf-8")
        )
        for name in ("k3s_cluster_cidr", "k3s_service_cidr"):
            self.assertEqual(defaults[name], k3s_defaults[name])
        guard = tasks[0]["ansible.builtin.assert"]["that"]
        self.assertTrue(any("ssh_access_public_verified" in condition for condition in guard))
        initial = next(task for task in tasks if task.get("name")
                       == "Initialize the desired inbound firewall rules")
        facts = initial["ansible.builtin.set_fact"]
        self.assertEqual(facts["common_expected_public_ufw_rules"],
                         ["ufw limit 22/tcp", "ufw allow 443/tcp"])
        limits = [index for index, task in enumerate(tasks)
                  if command_argv(task)[:3] == ["ufw", "limit", "22/tcp"]]
        cleanup_index = next(index for index, task in enumerate(tasks)
                             if task.get("when") == "item not in common_expected_inbound_ufw_rules")
        self.assertEqual(len(limits), 2)
        self.assertLess(limits[0], cleanup_index)
        self.assertLess(cleanup_index, limits[1])
        verification = next(task for task in tasks if task.get("name")
                            == "Verify the declared inbound UFW rules")
        self.assertEqual(len(verification["ansible.builtin.assert"]["that"]), 2)
        common = load_tasks("ansible/roles/common/tasks/main.yml")
        self.assertTrue(any(task.get("ansible.builtin.import_tasks") == "firewall.yml" for task in common))
        self.assertFalse(any(
            task.get("ansible.builtin.template", {}).get("src") == "ssh-hardening.conf.j2"
            for task in common
        ))

    @requires_ansible
    def test_firewall_cleanup_renders_real_ansible_loop_and_delete_argv(self) -> None:
        tasks = load_tasks("ansible/roles/common/tasks/firewall.yml")
        cleanup = next(task for task in tasks
                       if task.get("when") == "item not in common_expected_inbound_ufw_rules")
        expected = [
            "ufw limit 22/tcp", "ufw allow 443/tcp",
            "ufw allow from 10.42.0.0/16", "ufw allow from 10.43.0.0/16",
        ]
        combine = next(task for task in tasks if task.get("name")
                       == "Combine the desired inbound UFW rules")
        for finalize in (False, True):
            declared = render(combine["ansible.builtin.set_fact"]["common_expected_inbound_ufw_rules"], {
                "common_expected_public_ufw_rules": expected[:2],
                "common_expected_internal_ufw_rules": expected[2:],
                "ssh_access_finalize": finalize,
            })
            self.assertEqual(declared, expected)
        stale = ["ufw allow 5432/tcp", "ufw allow 22/tcp"]
        configured = [rule + " comment 'managed rule'" for rule in stale + expected]
        configured += ["Added user rules (see 'ufw status' for running firewall):",
                       "ufw allow out 443/tcp", "ufw limit out 25/tcp"]
        variables = {
            "common_configured_ufw_rules_before_cleanup": {"stdout_lines": configured},
            "common_expected_inbound_ufw_rules": expected,
        }
        loop = render(cleanup["loop"], variables)
        self.assertEqual(loop, stale + expected)
        self.assertEqual([rule for rule in loop if rule not in expected], stale)

        for rule in stale:
            with self.subTest(rule=rule):
                argv = render(cleanup["ansible.builtin.command"]["argv"], {**variables, "item": rule})
                self.assertEqual(argv, ["ufw", "--force", "delete"] + rule.split()[1:])

    def test_one_cidr_validator_runs_before_remote_contact_and_firewall_mutation(self) -> None:
        validators = [
            (path, task) for path, task in all_tasks()
            if "import ipaddress" in str(command_argv(task))
        ]
        self.assertEqual(
            [path for path, _ in validators],
            ["ansible/roles/common/tasks/network_preflight.yml"],
        )
        guard = validators[0][1]
        argv = command_argv(guard)
        self.assertEqual(argv[:2], ["{{ ansible_playbook_python }}", "-c"])
        self.assertEqual(guard["delegate_to"], "localhost")
        self.assertIs(guard["become"], False)
        self.assertIs(guard["changed_when"], False)
        self.assertIs(guard["check_mode"], False)

        tasks = load_tasks("ansible/roles/common/tasks/firewall.yml")
        include = next(index for index, task in enumerate(tasks)
                       if task.get("ansible.builtin.import_tasks") == "network_preflight.yml")
        first_mutation = next(index for index, task in enumerate(tasks)
                              if command_argv(task)[:1] == ["ufw"])
        self.assertLess(include, first_mutation)

        play = yaml.safe_load((ROOT / "ansible/playbooks/bootstrap.yml").read_text(encoding="utf-8"))[0]
        pre_tasks = play["pre_tasks"]
        preflight = next(index for index, task in enumerate(pre_tasks)
                         if task.get("ansible.builtin.import_role")
                         == {"name": "common", "tasks_from": "network_preflight.yml"})
        gather = next(index for index, task in enumerate(pre_tasks) if "ansible.builtin.setup" in task)
        self.assertIs(play["gather_facts"], False)
        self.assertLess(preflight, gather)
        self.assertIn("preflight", pre_tasks[preflight]["tags"])
        for cluster, service, valid in (
            ("10.42.0.0/16", "10.43.0.0/16", True),
            ("172.16.0.0/16", "192.168.0.0/16", True),
            ("0.0.0.0/0", "10.43.0.0/16", False),
            ("10.42.0.0/16", "0.0.0.0/0", False),
            ("1.255.226.0/24", "10.43.0.0/16", False),
            ("100.64.0.0/10", "10.43.0.0/16", False),
            ("127.0.0.0/8", "10.43.0.0/16", False),
            ("2001:db8::/64", "10.43.0.0/16", False),
            ("10.42.0.1/16", "10.43.0.0/16", False),
            ("10.42.0.0/255.255.0.0", "10.43.0.0/16", False),
            ("10.0.0.0/8", "10.43.0.0/16", False),
            ("10.43.0.0/16", "10.43.0.0/16", False),
            ("invalid", "10.43.0.0/16", False),
        ):
            with self.subTest(cluster=cluster, service=service):
                result = subprocess.run(
                    [sys.executable, "-c", argv[2], cluster, service],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode == 0, valid, result.stderr)

    def test_firewall_check_mode_reads_state_but_does_not_require_unapplied_changes(self) -> None:
        tasks = load_tasks("ansible/roles/common/tasks/firewall.yml")
        for task in tasks:
            if task.get("ansible.builtin.command") in ("ufw show added", "ufw status", "ufw status verbose"):
                self.assertIs(task["check_mode"], False)
                self.assertIs(task["changed_when"], False)
            if task.get("name") in ("Verify the declared inbound UFW rules", "Verify the required K3s firewall boundaries"):
                self.assertEqual(task["when"], "not ansible_check_mode")
        self.assertNotIn("when", tasks[0])

    def test_firewall_port_patterns_distinguish_allow_from_limit_on_ipv4_and_ipv6(self) -> None:
        tasks = load_tasks("ansible/roles/common/tasks/firewall.yml")
        guard = next(task for task in tasks if task.get("name")
                     == "Verify the required K3s firewall boundaries")
        patterns = []
        for expression in guard["ansible.builtin.assert"]["that"]:
            match = re.search(r"select\('match', ('(?:\\.|[^'])*')\)", expression)
            if match:
                patterns.append(re.compile(ast.literal_eval(match.group(1))))
        self.assertEqual(len(patterns), 4)
        ssh_limit, ssh_allow, private_ports, https_allow = patterns
        for suffix in ("", " (v6)"):
            self.assertRegex(f"22/tcp{suffix} LIMIT IN Anywhere{suffix}", ssh_limit)
            self.assertNotRegex(f"22/tcp{suffix} ALLOW IN Anywhere{suffix}", ssh_limit)
            self.assertRegex(f"22/tcp{suffix} ALLOW IN Anywhere{suffix}", ssh_allow)
            self.assertRegex(f"443/tcp{suffix} ALLOW IN Anywhere{suffix}", https_allow)
            for port in (80, 5432, 6443):
                for action in ("ALLOW", "LIMIT"):
                    self.assertRegex(f"{port}/tcp{suffix} {action} IN Anywhere{suffix}", private_ports)
        self.assertNotRegex("8080/tcp ALLOW IN Anywhere", private_ports)

    def test_direct_edge_exposes_only_https_and_trusts_no_forwarded_proxy(self) -> None:
        common_tasks = (
            ROOT / "ansible/roles/common/tasks/firewall.yml"
        ).read_text(encoding="utf-8")
        traefik_config = yaml.safe_load(
            (ROOT / "ansible/roles/k3s/templates/traefik-config.yaml.j2").read_text(encoding="utf-8")
        )
        self.assertIn("umc-public-https", common_tasks)
        traefik_values = yaml.safe_load(traefik_config["spec"]["valuesContent"])
        self.assertIs(traefik_values["ports"]["web"]["expose"]["default"], False)
        self.assertIs(traefik_values["service"]["spec"]["allocateLoadBalancerNodePorts"], False)
        self.assertEqual(
            traefik_values["ports"]["websecure"]["forwardedHeaders"],
            {"insecure": False, "trustedIPs": []},
        )

    # 공격 트래픽을 받는 Traefik은 메모리 상한을 두고, CPU는 throttling 없이 쓰게 한다.
    def test_traefik_memory_is_bounded_without_cpu_limit(self) -> None:
        traefik_config = yaml.safe_load(
            (ROOT / "ansible/roles/k3s/templates/traefik-config.yaml.j2").read_text(encoding="utf-8")
        )
        traefik_values = yaml.safe_load(traefik_config["spec"]["valuesContent"])
        self.assertEqual(
            traefik_values["resources"],
            {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "512Mi"}},
        )

    def test_k3s_api_is_probed_from_the_controller_as_unreachable(self) -> None:
        tasks = load_tasks("ansible/roles/k3s/tasks/main.yml")
        probe = next(task for task in tasks if task.get("name")
                     == "Require the Kubernetes API to remain unreachable from the controller")
        self.assertEqual(probe["ansible.builtin.wait_for"]["host"], "{{ ssh_access_public_host }}")
        self.assertEqual(probe["ansible.builtin.wait_for"]["port"], 6443)
        self.assertEqual(probe["ansible.builtin.wait_for"]["state"], "stopped")
        self.assertEqual(probe["delegate_to"], "localhost")


# 자동 업데이트는 보안 pocket만 받고 재부팅·메일·서비스 자동 재시작을 하지 않는다.
class AutomaticUpdateContractTests(unittest.TestCase):
    def test_apt_configuration_clears_defaults_and_allows_only_security(self) -> None:
        config = (ROOT / "ansible/roles/common/files/unattended-upgrades.conf").read_text(encoding="utf-8")
        self.assertIn("#clear Unattended-Upgrade::Allowed-Origins;", config)
        self.assertIn("#clear Unattended-Upgrade::Origins-Pattern;", config)
        origins = re.search(r"Unattended-Upgrade::Allowed-Origins \{(.*?)\};", config, re.S).group(1)
        entries = re.findall(r'"([^"]+)";', origins)
        self.assertTrue(entries)
        self.assertTrue(all(entry.endswith("-security") for entry in entries), entries)
        self.assertIn('Unattended-Upgrade::Automatic-Reboot "false";', config)
        self.assertIn('Unattended-Upgrade::Mail "";', config)
        needrestart = (ROOT / "ansible/roles/common/files/needrestart-list-only.conf").read_text(encoding="utf-8")
        self.assertIn("$nrconf{restart} = 'l';", needrestart)

    def test_timers_start_only_after_the_installed_configuration_is_validated(self) -> None:
        main = load_tasks("ansible/roles/common/tasks/main.yml")
        needrestart = next(index for index, task in enumerate(main)
                           if task.get("ansible.builtin.copy", {}).get("src") == "needrestart-list-only.conf")
        install = next(index for index, task in enumerate(main) if "ansible.builtin.apt" in task)
        imported = next(index for index, task in enumerate(main)
                        if task.get("ansible.builtin.import_tasks") == "automatic_updates.yml")
        self.assertLess(needrestart, install)
        self.assertLess(install, imported)
        defaults = yaml.safe_load((ROOT / "ansible/roles/common/defaults/main.yml").read_text(encoding="utf-8"))
        self.assertIn("unattended-upgrades", defaults["common_packages"])

        tasks = load_tasks("ansible/roles/common/tasks/automatic_updates.yml")
        apt_conf = next(index for index, task in enumerate(tasks)
                        if task.get("ansible.builtin.copy", {}).get("src") == "unattended-upgrades.conf")
        guard = next(index for index, task in enumerate(tasks) if "ansible.builtin.assert" in task)
        timers = next(index for index, task in enumerate(tasks) if "ansible.builtin.systemd_service" in task)
        self.assertLess(apt_conf, guard)
        self.assertEqual(timers, len(tasks) - 1)
        self.assertEqual(tasks[timers]["loop"], ["apt-daily.timer", "apt-daily-upgrade.timer"])
        copy = tasks[apt_conf]["ansible.builtin.copy"]
        # 50unattended-upgrades보다 뒤에 읽혀야 #clear가 기본 목록을 지운다.
        self.assertGreater(Path(copy["dest"]).name, "50unattended-upgrades")
        self.assertEqual(copy["validate"], "/usr/bin/apt-config -c %s dump")

    @requires_ansible
    def test_effective_configuration_guard_rejects_wider_origins_reboot_or_mail(self) -> None:
        tasks = load_tasks("ansible/roles/common/tasks/automatic_updates.yml")
        guard = next(task for task in tasks if "ansible.builtin.assert" in task)
        self.assertEqual(guard["when"], "not ansible_check_mode")
        effective = [
            'APT::Periodic::Unattended-Upgrade "1";',
            'Unattended-Upgrade::Allowed-Origins "";',
            'Unattended-Upgrade::Allowed-Origins:: "${distro_id}:${distro_codename}-security";',
            'Unattended-Upgrade::Automatic-Reboot "false";',
            'Unattended-Upgrade::Mail "";',
        ]
        for lines, accepted in (
            (effective, True),
            (effective + ['Unattended-Upgrade::Allowed-Origins:: "${distro_id}:${distro_codename}-updates";'], False),
            (effective + ['Unattended-Upgrade::Origins-Pattern:: "origin=Debian,codename=${distro_codename}";'], False),
            ([line.replace('"false"', '"true"') for line in effective], False),
            ([line.replace('Mail ""', 'Mail "root"') for line in effective], False),
            ([line for line in effective if "Allowed-Origins::" not in line], False),
        ):
            with self.subTest(lines=lines):
                registered = {"common_apt_config": {"stdout_lines": lines}}
                origins = render(guard["vars"]["common_unattended_origins"], registered)
                self.assertIs(passes(guard["ansible.builtin.assert"]["that"], {
                    **registered, "common_unattended_origins": origins,
                }), accepted)

    def test_k3s_is_not_an_apt_package(self) -> None:
        for path in sorted((ROOT / "ansible/roles/k3s/tasks").glob("*.yml")):
            tasks = load_tasks(str(path.relative_to(ROOT)))
            self.assertFalse(any("ansible.builtin.apt" in task for task in tasks), path)


# 호스트 journal이 단일 노드 디스크를 잠식하지 않게 크기를 제한하고 바로 반영한다.
class JournaldContractTests(unittest.TestCase):
    def test_journal_size_drop_in_restarts_journald(self) -> None:
        drop_in = configparser.ConfigParser()
        drop_in.optionxform = str
        drop_in.read(ROOT / "ansible/roles/common/files/journald-size.conf", encoding="utf-8")
        self.assertEqual(dict(drop_in["Journal"]), {"SystemMaxUse": "1G"})
        tasks = load_tasks("ansible/roles/common/tasks/main.yml")
        copy = next(task for task in tasks
                    if task.get("ansible.builtin.copy", {}).get("src") == "journald-size.conf")
        self.assertTrue(copy["ansible.builtin.copy"]["dest"].startswith("/etc/systemd/journald.conf.d/"))
        self.assertTrue(copy["ansible.builtin.copy"]["dest"].endswith(".conf"))
        handlers = load_tasks("ansible/roles/common/handlers/main.yml")
        handler = next(task for task in handlers if task["name"] == copy["notify"])
        self.assertEqual(handler["ansible.builtin.systemd_service"],
                         {"name": "systemd-journald", "state": "restarted"})


# 자격증명·sudo·SSH 인증·K3s 보호 경계를 지키는 task가 리팩터링 중 빠지지 않게 한다.
class SecurityCriticalTaskContractTests(unittest.TestCase):
    def test_task_walk_reaches_block_rescue_and_always_children(self) -> None:
        nested = [{"name": "outer", "no_log": True, "block": [
            {"name": "body", "ansible.builtin.command": "true"},
            {"name": "inner", "block": [{"name": "deep", "no_log": False}],
             "rescue": [{"name": "recover"}], "always": [{"name": "cleanup"}]},
        ]}, {"name": "plain"}]
        self.assertEqual(
            [(task["name"], task.get("no_log")) for task in leaf_tasks(nested)],
            [("body", True), ("deep", False), ("recover", True), ("cleanup", True), ("plain", None)],
        )

    def test_every_task_touching_effective_credentials_hides_its_output(self) -> None:
        credential = re.compile(r"\b\w*_effective_\w*(?:key|secret|token|password)\w*\b")
        touching = [(path, task) for path, task in all_tasks()
                    if credential.search(yaml.safe_dump(task))]
        self.assertGreaterEqual(len(touching), 6)
        for path, task in touching:
            with self.subTest(path=path, task=task["name"]):
                self.assertIs(task.get("no_log"), True)

    def test_sudoers_files_are_validated_with_visudo(self) -> None:
        sudoers = []
        for path, task in all_tasks():
            for module in ("ansible.builtin.copy", "ansible.builtin.template", "ansible.builtin.lineinfile"):
                arguments = task.get(module)
                if isinstance(arguments, dict) and str(arguments.get("dest", arguments.get("path", ""))).startswith("/etc/sudoers"):
                    sudoers.append((path, task["name"], arguments))
        self.assertTrue(sudoers)
        for path, name, arguments in sudoers:
            with self.subTest(path=path, task=name):
                self.assertEqual(arguments["validate"], "/usr/sbin/visudo -cf %s")
                self.assertEqual(arguments["mode"], "0440")

    @requires_ansible
    def test_shadowed_ssh_policy_is_rejected_before_reload(self) -> None:
        tasks = load_tasks("ansible/roles/ssh_access/tasks/main.yml")
        names = [task.get("name") for task in tasks]
        guard = tasks[names.index("Reject shadowed authentication or tunnel policy before reload")]
        effective = tasks[names.index("Read effective authentication and per-user policy")]
        flush = next(index for index, task in enumerate(tasks) if task.get("ansible.builtin.meta") == "flush_handlers")
        self.assertLess(tasks.index(effective), tasks.index(guard))
        self.assertLess(tasks.index(guard), flush)
        self.assertEqual(guard["loop"], "{{ ssh_access_effective.results }}")
        self.assertEqual(guard["when"], "not ansible_check_mode")
        self.assertEqual(command_argv(effective)[:3], ["/usr/sbin/sshd", "-T", "-C"])

        role_vars = yaml.safe_load(
            (ROOT / "ansible/roles/ssh_access/vars/main.yml").read_text(encoding="utf-8")
        )
        shared = [
            "passwordauthentication no", "kbdinteractiveauthentication no",
            "authenticationmethods publickey", "authorizedkeysfile /etc/ssh/authorized_keys/%u",
            "permitrootlogin no", "clientaliveinterval 60", "clientalivecountmax 3",
        ]
        admin = {"name": "admin1", "role": "admin"}
        tunnel = {"name": "backend1", "role": "db_tunnel", "permit_open": ["10.43.0.10:5432"]}
        tunnel_lines = shared + ["allowtcpforwarding local", "maxsessions 0", "permitopen 10.43.0.10:5432"]
        cases = [
            (admin, shared + ["maxsessions 10", "permitopen any"], True, True),
            (tunnel, tunnel_lines, True, True),
            (admin, [line for line in shared if line != "permitrootlogin no"], False, True),
            (tunnel, [line.replace("maxsessions 0", "maxsessions 10") for line in tunnel_lines], True, False),
            (tunnel, [line.replace("10.43.0.10", "any") for line in tunnel_lines], True, False),
        ]
        # 유효 정책에서 필수 줄이 하나라도 빠지면 거부해야 한다.
        cases += [(admin, [line for line in shared if line != missing], True, False) for missing in shared]
        cases += [(tunnel, [line for line in tunnel_lines if line != missing], True, False)
                  for missing in tunnel_lines]
        for account, lines, finalize, accepted in cases:
            with self.subTest(role=account["role"], finalize=finalize, lines=lines):
                self.assertIs(passes(guard["ansible.builtin.assert"]["that"], {
                    **role_vars, "ssh_access_finalize": finalize,
                    "item": {"item": account, "stdout_lines": lines},
                }), accepted)

    @requires_ansible
    def test_k3s_secret_encryption_and_root_only_kubeconfig_are_enforced(self) -> None:
        tasks = load_tasks("ansible/roles/k3s/tasks/main.yml")
        encryption = next(task for task in tasks
                          if task.get("ansible.builtin.command") == "/usr/local/bin/k3s secrets-encrypt status")
        self.assertEqual(encryption["register"], "k3s_encryption_status")
        for rc, stdout, failed in (
            (0, "Encryption Status: Enabled\nCurrent Rotation Stage: start", False),
            (0, "Encryption Status: Disabled", True),
            (1, "Encryption Status: Enabled", True),
            (0, "", True),
        ):
            with self.subTest(rc=rc, stdout=stdout):
                self.assertIs(passes(encryption["failed_when"], {
                    "k3s_encryption_status": {"rc": rc, "stdout": stdout},
                }), failed)

        kubeconfig = next(task for task in tasks if task.get("name") == "Require a root-only administrator kubeconfig")
        root_only = {"exists": True, "pw_name": "root", "mode": "0600"}
        for stat, accepted in (
            (root_only, True),
            ({**root_only, "exists": False}, False),
            ({**root_only, "pw_name": "ubuntu"}, False),
            ({**root_only, "mode": "0644"}, False),
            ({**root_only, "mode": "0640"}, False),
        ):
            with self.subTest(stat=stat):
                self.assertIs(passes(kubeconfig["ansible.builtin.assert"]["that"], {
                    "k3s_kubeconfig": {"stat": stat},
                }), accepted)
        config = (ROOT / "ansible/roles/k3s/templates/config.yaml.j2").read_text(encoding="utf-8")
        self.assertIn("write-kubeconfig-mode: '0600'", config)
        self.assertIn("secrets-encryption: true", config)


# 서버 상태를 바꾸는 kubectl 명령이 재실행 결과에서 변경 없음으로 숨지 않게 한다.
class ChangeReportingContractTests(unittest.TestCase):
    def test_mutating_kubectl_commands_never_report_unchanged(self) -> None:
        mutations = [
            (path, task) for path, task in all_tasks()
            if command_argv(task)[1:2] == ["kubectl"]
            and (command_argv(task)[2:3] in (["apply"], ["annotate"])
                 or command_argv(task)[2:4] == ["rollout", "restart"])
        ]
        self.assertTrue(any("annotate" in command_argv(task) for _, task in mutations))
        for path, task in mutations:
            with self.subTest(path=path, task=task["name"]):
                self.assertIsNot(task.get("changed_when"), False)


# 저장소 정체성 검사가 Git ignore 규칙을 존중해 로컬 비공개 파일을 훑지 않게 한다.
# k3s role을 로드하지 않고 실행될 수 있는 role은 읽는 k3s 값을 직접 갖되, k3s role과 같은 값이어야 한다.
class SharedK3sDefaultsContractTests(unittest.TestCase):
    def test_every_role_reading_k3s_values_copies_the_k3s_defaults(self) -> None:
        roles = ROOT / "ansible" / "roles"
        k3s_defaults = yaml.safe_load((roles / "k3s/defaults/main.yml").read_text(encoding="utf-8"))
        checked = set()
        for role in sorted(path for path in roles.iterdir() if path.name != "k3s"):
            defaults_path = role / "defaults" / "main.yml"
            defaults = (
                yaml.safe_load(defaults_path.read_text(encoding="utf-8"))
                if defaults_path.exists() else {}
            ) or {}
            referenced = {
                name
                for path in role.rglob("*")
                if path.suffix in {".yml", ".yaml", ".j2"}
                and path.parent.name not in {"defaults", "files"}
                for name in re.findall(r"\bk3s_[a-z0-9_]+", path.read_text(encoding="utf-8"))
            }
            copied = {name for name in defaults if name.startswith("k3s_")}
            for name in sorted(referenced | copied):
                checked.add((role.name, name))
                with self.subTest(role=role.name, variable=name):
                    self.assertIn(name, defaults, f"add {name} to {role.name} defaults")
                    self.assertEqual(defaults[name], k3s_defaults[name])
        self.assertTrue({
            ("common", "k3s_cluster_cidr"),
            ("argocd", "k3s_kubeconfig_path"),
            ("argocd", "k3s_version"),
        } <= checked)


class RepositoryValidationContractTests(unittest.TestCase):
    def test_repository_identity_scan_respects_gitignore(self) -> None:
        validator = (ROOT / "scripts" / "contracts" / "repository.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('"--exclude-standard"', validator)


if __name__ == "__main__":
    unittest.main()
