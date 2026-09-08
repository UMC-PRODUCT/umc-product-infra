from __future__ import annotations

import json
import re
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


class TailscaleBootstrapContractTests(unittest.TestCase):
    def test_standard_bootstrap_requires_tailscale_before_common(self) -> None:
        path = ROOT / "ansible" / "playbooks" / "bootstrap.yml"
        with path.open(encoding="utf-8") as stream:
            play = yaml.safe_load(stream)[0]

        role_names = [role["role"] for role in play["roles"]]
        self.assertEqual(
            role_names,
            ["tailscale", "common", "k3s", "argocd", "external_secrets_bootstrap"],
        )
        text = path.read_text(encoding="utf-8")
        self.assertIn("tailscale\n          - whois\n          - --json", text)
        self.assertIn("bootstrap_ssh_destination_address", text)
        self.assertIn(".get('Node', {}).get('ID', '') | string | length > 0", text)

    def test_enrollment_does_not_mutate_ufw_or_enable_tailscale_ssh(self) -> None:
        enroll_playbook = (
            ROOT / "ansible" / "playbooks" / "tailscale-enroll.yml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("ansible.builtin.command: ufw", enroll_playbook)

        tasks = load_tasks("ansible/roles/tailscale/tasks/main.yml")
        enrollment = next(
            task for task in tasks if task.get("name") == "Enroll the server in the tailnet"
        )
        self.assertTrue(enrollment["no_log"])
        join_task = next(
            task
            for task in enrollment["block"]
            if task.get("name")
            == "Join with the fixed server identity and safe network settings"
        )
        argv = command_argv(join_task)
        self.assertTrue(any(str(value).startswith("--auth-key=file:") for value in argv))
        self.assertIn("--ssh=false", argv)
        self.assertIn("--accept-routes=false", argv)
        self.assertIn("--netfilter-mode=on", argv)
        self.assertNotIn("--webclient=false", argv)

        client_preferences = next(
            task
            for task in enrollment["block"]
            if task.get("name") == "Disable unpinned Tailscale client auto-updates"
        )
        self.assertIn("--auto-update=false", command_argv(client_preferences))
        self.assertIn("--webclient=false", command_argv(client_preferences))

        cleanup = enrollment["always"][0]
        self.assertEqual(cleanup["ansible.builtin.file"]["state"], "absent")

    def test_tailscale_package_and_repository_key_are_pinned(self) -> None:
        path = ROOT / "ansible" / "roles" / "tailscale" / "defaults" / "main.yml"
        with path.open(encoding="utf-8") as stream:
            defaults = yaml.safe_load(stream)

        self.assertRegex(defaults["tailscale_version"], r"^\d+\.\d+\.\d+$")
        self.assertRegex(
            defaults["tailscale_apt_key_checksum"], r"^sha256:[0-9a-f]{64}$"
        )
        self.assertIn("pkgs.tailscale.com/stable/ubuntu", defaults["tailscale_apt_key_url"])

    def test_tailscale_ipv4_filter_matches_100_range_addresses(self) -> None:
        tasks = load_tasks("ansible/roles/tailscale/tasks/main.yml")
        state = next(
            task
            for task in tasks
            if task.get("name") == "Record the effective Tailscale state"
        )
        expression = state["ansible.builtin.set_fact"]["tailscale_primary_ipv4"]

        self.assertIn("select('match', '^100\\.')", expression)
        self.assertNotIn("select('match', '^100\\\\.')", expression)

    def test_ufw_has_only_an_interface_rule_for_tailnet_management(self) -> None:
        common = (
            ROOT / "ansible" / "roles" / "common" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")
        defaults = (
            ROOT / "ansible" / "roles" / "common" / "defaults" / "main.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("ufw allow in on", common)
        self.assertNotIn("common_admin_ssh_cidrs", common + defaults)
        self.assertNotIn("common_k3s_api_cidrs", common + defaults)
        self.assertIn("contain no port-based SSH or Kubernetes API allow rule", common)

    def test_direct_edge_exposes_only_https_and_trusts_no_forwarded_proxy(self) -> None:
        common_tasks = (
            ROOT / "ansible" / "roles" / "common" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")
        with (
            ROOT / "ansible" / "roles" / "common" / "defaults" / "main.yml"
        ).open(encoding="utf-8") as stream:
            common_defaults = yaml.safe_load(stream)
        with (
            ROOT
            / "ansible"
            / "roles"
            / "k3s"
            / "templates"
            / "traefik-config.yaml.j2"
        ).open(encoding="utf-8") as stream:
            traefik_config = yaml.safe_load(stream)

        self.assertEqual(common_defaults["common_public_tcp_ports"], [443])
        self.assertIn("umc-public-https", common_tasks)
        traefik_values = yaml.safe_load(traefik_config["spec"]["valuesContent"])
        self.assertIs(traefik_values["ports"]["web"]["expose"]["default"], False)
        self.assertIs(
            traefik_values["service"]["spec"]["allocateLoadBalancerNodePorts"], False
        )
        forwarded_headers = traefik_values["ports"]["websecure"][
            "forwardedHeaders"
        ]
        self.assertEqual(
            forwarded_headers,
            {"insecure": False, "trustedIPs": []},
        )

    def test_k3s_api_is_probed_from_the_controller_as_unreachable(self) -> None:
        tasks = load_tasks("ansible/roles/k3s/tasks/main.yml")
        probe = next(
            task
            for task in tasks
            if task.get("name")
            == "Require the Kubernetes API to remain unreachable from the controller"
        )

        self.assertEqual(probe["ansible.builtin.wait_for"]["port"], 6443)
        self.assertEqual(probe["ansible.builtin.wait_for"]["state"], "stopped")
        self.assertEqual(probe["delegate_to"], "localhost")

    def test_tailnet_policy_allows_ssh_and_denies_k3s_api(self) -> None:
        path = ROOT / "ansible" / "tailscale-policy.example.hujson"
        hujson = re.sub(r"//.*", "", path.read_text(encoding="utf-8"))
        policy = json.loads(hujson)

        self.assertEqual(
            policy["grants"],
            [
                {
                    "src": ["group:umc-infra-admins"],
                    "dst": ["tag:umc-idc"],
                    "ip": ["tcp:22"],
                }
            ],
        )
        self.assertEqual(policy["tests"][0]["accept"], ["tag:umc-idc:22"])
        self.assertEqual(policy["tests"][0]["deny"], ["tag:umc-idc:6443"])


class RepositoryValidationContractTests(unittest.TestCase):
    def test_repository_identity_scan_respects_gitignore(self) -> None:
        validator = (ROOT / "scripts" / "validate_contracts.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('"--exclude-standard"', validator)


if __name__ == "__main__":
    unittest.main()
