from __future__ import annotations

import copy
import fnmatch
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


ROOT_APPS = ("bootstrap/root-app.yaml", "bootstrap/root-app-prepare.yaml", "bootstrap/root-app-verify.yaml")


def load(path: str):
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def exclude_patterns(path: str) -> list[str]:
    pattern = load(path)["spec"]["source"]["directory"]["exclude"]
    return pattern[1:-1].split(",") if pattern.startswith("{") else [pattern]


# 이관 준비·검증 단계에서 앱 쓰기와 DNS 전환이 앞서 열리지 않는지 확인한다.
class MigrationBootstrapTests(unittest.TestCase):
    def test_staged_roots_only_change_directory_exclusions(self):
        normal = copy.deepcopy(load("bootstrap/root-app.yaml"))
        normal["spec"]["source"]["directory"].pop("exclude")
        for path in ("bootstrap/root-app-prepare.yaml", "bootstrap/root-app-verify.yaml"):
            staged = load(path)
            comparison = copy.deepcopy(staged)
            comparison["spec"]["source"]["directory"].pop("exclude")
            self.assertEqual(comparison, normal)
            self.assertFalse(staged["spec"]["syncPolicy"]["automated"]["prune"])

    # PR별 배포 기록 JSON은 kind가 없어 root가 읽으면 manifest 해석 전체가 실패한다.
    def test_every_root_skips_preview_release_records(self):
        records = [
            str(path.relative_to(ROOT / "argocd"))
            for path in (ROOT / "argocd" / "preview-releases").glob("*.json")
        ] + ["preview-releases/pr-42.json"]
        self.assertEqual(exclude_patterns("bootstrap/root-app.yaml"), ["preview-releases/*"])
        for path in ROOT_APPS:
            patterns = exclude_patterns(path)
            for record in records:
                self.assertTrue(any(fnmatch.fnmatchcase(record, item) for item in patterns), (path, record))

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
        patterns = exclude_patterns("bootstrap/root-app-prepare.yaml")
        excluded = {
            path for path in paths
            if any(fnmatch.fnmatchcase(path, item) for item in patterns)
        }
        self.assertEqual(excluded, expected)
        self.assertTrue(expected <= paths)
        for path in paths - expected:
            documents = yaml.safe_load_all((ROOT / "argocd" / path).read_text(encoding="utf-8"))
            for document in documents:
                if document and document.get("kind") == "Application":
                    self.assertNotEqual(document["metadata"]["name"], "root")

    def test_verify_keeps_external_dns_off_until_cutover(self):
        self.assertEqual(
            exclude_patterns("bootstrap/root-app-verify.yaml"),
            ["preview-releases/*", "applications/platform/external-dns.yaml"],
        )

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
        for flag in ("bootstrap_root_app_enabled", "bootstrap_confirm", "ssh_access_confirm", "ssh_access_finalize"):
            self.assertIs(group["vars"][flag], False)

    # 예제에는 운영 서버 주소를 두지 않고, 자리표시자를 남긴 채로는 bootstrap이 원격 접속 전에 멈춘다.
    def test_example_addresses_are_placeholders_rejected_before_remote_contact(self):
        try:
            from ansible.parsing.dataloader import DataLoader
            from ansible.template import Templar, trust_as_template
        except ImportError:
            self.skipTest("Install ansible/requirements.txt to verify the Ansible runtime")

        group = load("ansible/inventories/idc-new/hosts.example.yml")["all"]["children"]["k3s_servers"]
        host = group["hosts"]["umc-cafe24-01"]
        for value in (host["ansible_host"], group["vars"]["ssh_access_public_host"]):
            self.assertIn("CHANGE_ME", value)
            self.assertNotRegex(value, r"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+")

        guard = load("ansible/playbooks/bootstrap.yml")[0]["pre_tasks"][0]["ansible.builtin.assert"]["that"]
        reviewed = {
            **group["vars"], **host,
            "ansible_host": "203.0.113.10", "ansible_user": "infra_operator",
            "ssh_access_public_host": "203.0.113.10",
            "bootstrap_confirm": True, "ssh_access_confirm": True, "ssh_access_finalize": True,
            "groups": {"k3s_servers": ["umc-cafe24-01"]},
        }
        for replaced, accepted in (
            ({}, True),
            ({"ssh_access_public_host": group["vars"]["ssh_access_public_host"]}, False),
            ({"ansible_host": host["ansible_host"]}, False),
        ):
            with self.subTest(replaced=replaced):
                templar = Templar(loader=DataLoader(), variables={**reviewed, **replaced})
                results = [templar.evaluate_conditional(trust_as_template(condition)) for condition in guard]
                self.assertIs(all(results), accepted)


if __name__ == "__main__":
    unittest.main()
