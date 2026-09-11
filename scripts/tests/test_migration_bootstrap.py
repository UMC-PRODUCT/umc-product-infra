from __future__ import annotations

import copy
import fnmatch
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def load(path: str):
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


class MigrationBootstrapTests(unittest.TestCase):
    def test_staged_roots_only_change_directory_exclusions(self):
        normal = load("bootstrap/root-app.yaml")
        for path in ("bootstrap/root-app-prepare.yaml", "bootstrap/root-app-verify.yaml"):
            staged = load(path)
            comparison = copy.deepcopy(staged)
            comparison["spec"]["source"]["directory"].pop("exclude")
            self.assertEqual(comparison, normal)
            self.assertFalse(staged["spec"]["syncPolicy"]["automated"]["prune"])

    def test_prepare_excludes_app_writers_and_dns_but_keeps_platform_and_db(self):
        paths = {
            str(path.relative_to(ROOT / "argocd"))
            for path in (ROOT / "argocd").rglob("*.yaml")
        }
        expected = {
            "applications/prod/server.yaml",
            "applications/dev/server.yaml",
            "applications/preview/applicationset.yaml",
            "applications/platform/external-dns.yaml",
        }
        pattern = load("bootstrap/root-app-prepare.yaml")["spec"]["source"]["directory"]["exclude"]
        self.assertTrue(pattern.startswith("{") and pattern.endswith("}"))
        excluded = {
            path for path in paths
            if any(fnmatch.fnmatchcase(path, item) for item in pattern[1:-1].split(","))
        }
        self.assertEqual(excluded, expected)
        self.assertTrue(expected <= paths)
        for path in paths - expected:
            documents = yaml.safe_load_all((ROOT / "argocd" / path).read_text(encoding="utf-8"))
            for document in documents:
                if document and document.get("kind") == "Application":
                    self.assertNotEqual(document["metadata"]["name"], "root")

    def test_verify_keeps_external_dns_off_until_cutover(self):
        pattern = load("bootstrap/root-app-verify.yaml")["spec"]["source"]["directory"]["exclude"]
        self.assertEqual(pattern, "applications/platform/external-dns.yaml")

    def test_core_only_install_checks_existing_apps_and_gates_root(self):
        play = load("ansible/playbooks/bootstrap.yml")[0]
        tasks = play["post_tasks"]
        inspect = next(task for task in tasks if "register" in task)
        self.assertIn("applications,applicationsets", inspect["ansible.builtin.command"]["argv"])
        self.assertIn("--all-namespaces", inspect["ansible.builtin.command"]["argv"])
        self.assertFalse(inspect["changed_when"])
        guard = next(task for task in tasks if "ansible.builtin.assert" in task)
        self.assertIn("stdout | trim | length == 0", guard["ansible.builtin.assert"]["that"][0])
        self.assertEqual(guard["when"], "not (bootstrap_root_app_enabled | default(true) | bool)")
        root = tasks[-1]
        self.assertEqual(root["ansible.builtin.include_role"]["tasks_from"], "root_app.yml")
        self.assertEqual(root["when"], "bootstrap_root_app_enabled | default(true) | bool")

    def test_new_inventory_holds_root_and_requires_explicit_confirmation(self):
        group = load("ansible/inventories/idc-new/hosts.example.yml")["all"]["children"]["k3s_servers"]
        self.assertEqual(len(group["hosts"]), 1)
        self.assertEqual(group["hosts"]["umc-cafe24-01"]["ansible_host"], "1.255.226.166")
        for flag in ("bootstrap_root_app_enabled", "bootstrap_confirm", "ssh_access_confirm", "ssh_access_finalize"):
            self.assertIs(group["vars"][flag], False)


if __name__ == "__main__":
    unittest.main()
