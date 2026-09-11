from __future__ import annotations

import ast
import re
import subprocess
import sys
import unittest
from pathlib import Path
from urllib.parse import quote

import yaml


ROOT = Path(__file__).resolve().parents[2]


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


class PublicSshBootstrapContractTests(unittest.TestCase):
    def test_standard_bootstrap_prepares_personal_ssh_before_common(self) -> None:
        path = ROOT / "ansible" / "playbooks" / "bootstrap.yml"
        play = yaml.safe_load(path.read_text(encoding="utf-8"))[0]

        self.assertEqual(
            [role["role"] for role in play["roles"]],
            ["ssh_access", "common", "k3s", "argocd", "external_secrets_bootstrap"],
        )
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("tailscale_hostname", text)
        self.assertNotIn("/usr/bin/tailscale", text)
        self.assertIn("ssh_access_finalize | default(false) | bool", text)
        self.assertIn("common_public_tcp_ports == [22, 443]", text)

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
        retire = next(
            task for task in tasks
            if task.get("ansible.builtin.include_role", {}).get("tasks_from") == "retire-vpn.yml"
        )
        self.assertEqual(retire["when"], "ssh_access_finalize | bool")
        self.assertTrue(any(
            "ansible.builtin.wait_for_connection" in task
            for task in tasks[tasks.index(firewall) + 1:tasks.index(retire)]
        ))
        self.assertTrue(any(
            "ansible.builtin.wait_for_connection" in task
            for task in tasks[tasks.index(retire) + 1:]
        ))

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
        ):
            self.assertIn(setting, template)
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
        self.assertEqual(defaults["common_public_tcp_ports"], [22, 443])
        k3s_defaults = yaml.safe_load(
            (ROOT / "ansible/roles/k3s/defaults/main.yml").read_text(encoding="utf-8")
        )
        for name in ("k3s_cluster_cidr", "k3s_service_cidr"):
            self.assertEqual(defaults[name], k3s_defaults[name])
        guard = tasks[0]["ansible.builtin.assert"]["that"]
        self.assertIn("common_public_tcp_ports == [22, 443]", guard)
        self.assertTrue(any("ssh_access_public_verified" in condition for condition in guard))
        initial = next(task for task in tasks if task.get("name")
                       == "Initialize the desired inbound firewall rules")
        facts = initial["ansible.builtin.set_fact"]
        self.assertEqual(facts["common_expected_public_ufw_rules"],
                         ["ufw limit 22/tcp", "ufw allow 443/tcp"])
        self.assertIn("common_legacy_tailscale_interface.stat.exists",
                      facts["common_expected_legacy_ufw_rules"])
        self.assertIn("not (ssh_access_finalize", facts["common_expected_legacy_ufw_rules"])
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

    def test_firewall_cleanup_renders_real_ansible_loop_and_delete_argv(self) -> None:
        try:
            from ansible.parsing.dataloader import DataLoader
            from ansible.template import Templar, trust_as_template
        except ImportError:
            self.skipTest("Install ansible/requirements.txt to verify the Ansible runtime")

        tasks = load_tasks("ansible/roles/common/tasks/firewall.yml")
        cleanup = next(task for task in tasks
                       if task.get("when") == "item not in common_expected_inbound_ufw_rules")
        expected = [
            "ufw limit 22/tcp", "ufw allow 443/tcp",
            "ufw allow from 10.42.0.0/16", "ufw allow from 10.43.0.0/16",
        ]
        stale = ["ufw allow in on tailscale0", "ufw allow 22/tcp"]
        configured = [rule + " comment 'managed rule'" for rule in stale + expected]
        configured += ["Added user rules (see 'ufw status' for running firewall):",
                       "ufw allow out 443/tcp", "ufw limit out 25/tcp"]
        variables = {
            "common_configured_ufw_rules_before_cleanup": {"stdout_lines": configured},
            "common_expected_inbound_ufw_rules": expected,
        }
        templar = Templar(loader=DataLoader(), variables=variables)
        loop = templar.template(trust_as_template(cleanup["loop"]))
        self.assertEqual(loop, stale + expected)
        self.assertEqual([rule for rule in loop if rule not in expected], stale)

        for rule in stale:
            with self.subTest(rule=rule):
                templar = Templar(loader=DataLoader(), variables={**variables, "item": rule})
                argv = templar.template(trust_as_template(cleanup["ansible.builtin.command"]["argv"]))
                self.assertEqual(argv, ["ufw", "--force", "delete"] + rule.split()[1:])

    def test_firewall_rejects_unsafe_cidrs_before_any_rule_mutation(self) -> None:
        tasks = load_tasks("ansible/roles/common/tasks/firewall.yml")
        guard = next(task for task in tasks if task.get("name")
                     == "Validate private non-overlapping K3s CIDRs before firewall mutation")
        argv = command_argv(guard)
        self.assertEqual(argv[:2], ["{{ ansible_playbook_python }}", "-c"])
        self.assertEqual(guard["delegate_to"], "localhost")
        self.assertIs(guard["become"], False)
        self.assertIs(guard["changed_when"], False)
        self.assertIs(guard["check_mode"], False)
        first_mutation = next(index for index, task in enumerate(tasks)
                              if command_argv(task)[:1] == ["ufw"])
        self.assertLess(tasks.index(guard), first_mutation)
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

    def test_legacy_vpn_removal_is_guarded_and_retains_recovery_identity(self) -> None:
        tasks = load_tasks("ansible/roles/ssh_access/tasks/retire-vpn.yml")
        conditions = tasks[0]["ansible.builtin.assert"]["that"]
        self.assertIn("ssh_access_finalize | bool", conditions)
        self.assertIn("ssh_access_public_verified | default(false) | bool", conditions)
        package = next(task["ansible.builtin.apt"] for task in tasks if "ansible.builtin.apt" in task)
        self.assertEqual(package["name"], "tailscale")
        self.assertEqual(package["state"], "absent")
        self.assertFalse(package["purge"])

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

    def test_k3s_api_is_probed_from_the_controller_as_unreachable(self) -> None:
        tasks = load_tasks("ansible/roles/k3s/tasks/main.yml")
        probe = next(task for task in tasks if task.get("name")
                     == "Require the Kubernetes API to remain unreachable from the controller")
        self.assertEqual(probe["ansible.builtin.wait_for"]["host"], "{{ ssh_access_public_host }}")
        self.assertEqual(probe["ansible.builtin.wait_for"]["port"], 6443)
        self.assertEqual(probe["ansible.builtin.wait_for"]["state"], "stopped")
        self.assertEqual(probe["delegate_to"], "localhost")


class RepositoryValidationContractTests(unittest.TestCase):
    def test_repository_identity_scan_respects_gitignore(self) -> None:
        validator = (ROOT / "scripts" / "validate_contracts.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('"--exclude-standard"', validator)


if __name__ == "__main__":
    unittest.main()
