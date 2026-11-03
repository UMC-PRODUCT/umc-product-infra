from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = ROOT / "ansible" / "playbooks" / "argocd.yml"
ARGOCD_ROLE = ROOT / "ansible" / "roles" / "argocd"
ROLE_MODULES = {
    "import_role",
    "include_role",
    "ansible.builtin.import_role",
    "ansible.builtin.include_role",
}
TASK_SECTIONS = ("pre_tasks", "tasks", "post_tasks", "handlers")
READ_ONLY_MODULES = {"ansible.builtin.stat", "ansible.builtin.assert"}


def load_yaml(path: Path) -> object:
    with path.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def load_play() -> dict[str, object]:
    plays = load_yaml(PLAYBOOK)
    if not isinstance(plays, list) or len(plays) != 1:
        raise AssertionError("argocd.yml must contain exactly one play")
    return plays[0]


def walk_tasks(tasks: list[dict[str, object]]) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for task in tasks:
        found.append(task)
        for nested in ("block", "rescue", "always"):
            found.extend(walk_tasks(task.get(nested, [])))
    return found


def role_calls(play: dict[str, object]) -> list[dict[str, object]]:
    calls = [
        {"name": role} if isinstance(role, str) else role
        for role in play.get("roles", [])
    ]
    for section in TASK_SECTIONS:
        for task in walk_tasks(play.get(section, [])):
            for module in ROLE_MODULES & task.keys():
                calls.append(task[module])
    return calls


# 계정·RBAC 같은 core values 변경이 SSH·UFW·K3s·ESO 자격증명 재적용으로 번지지 않게 한다.
class ArgocdPlaybookScopeTests(unittest.TestCase):
    def test_playbook_targets_the_single_k3s_server_group(self) -> None:
        play = load_play()
        self.assertEqual(play["hosts"], "k3s_servers")
        self.assertNotIn("import_playbook", play)

    def test_only_the_argocd_role_runs(self) -> None:
        names = {call.get("name") or call.get("role") for call in role_calls(load_play())}
        self.assertEqual(names, {"argocd"})

    def test_root_application_stays_with_bootstrap(self) -> None:
        tasks_from = {
            call["tasks_from"] for call in role_calls(load_play()) if "tasks_from" in call
        }
        self.assertEqual(tasks_from, {"installed_preflight.yml"})
        self.assertNotIn("root_app", PLAYBOOK.read_text(encoding="utf-8"))

    def test_installed_core_check_runs_before_the_role(self) -> None:
        pre_tasks = load_play()["pre_tasks"]
        preflight = [
            task for task in pre_tasks
            if task.get("ansible.builtin.import_role", {}).get("tasks_from")
            == "installed_preflight.yml"
        ]
        self.assertEqual(len(preflight), 1)
        self.assertIn("preflight", preflight[0]["tags"])


# 사전 검사와 values 미리보기는 서버를 바꾸지 않아야 --tags preflight 실행이 안전하다.
class ArgocdInstalledPreflightTests(unittest.TestCase):
    def test_preflight_tasks_never_write(self) -> None:
        tasks = load_yaml(ARGOCD_ROLE / "tasks" / "installed_preflight.yml")
        for task in walk_tasks(tasks):
            with self.subTest(task=task["name"]):
                if READ_ONLY_MODULES & task.keys():
                    continue
                if "ansible.builtin.command" in task:
                    self.assertIs(task["changed_when"], False)
                    self.assertIs(task["check_mode"], False)
                    self.assertEqual(task["ansible.builtin.command"]["argv"][1], "list")
                    continue
                self.assertIs(task.get("check_mode"), True)


# argocd.yml은 k3s role을 로드하지 않으므로 argocd role이 읽는 k3s 값을 같은 값으로 갖고 있어야 한다.
class ArgocdSharedK3sDefaultsTests(unittest.TestCase):
    def test_every_k3s_variable_the_role_reads_has_the_k3s_default(self) -> None:
        argocd_defaults = load_yaml(ARGOCD_ROLE / "defaults" / "main.yml")
        k3s_defaults = load_yaml(ROOT / "ansible" / "roles" / "k3s" / "defaults" / "main.yml")
        referenced = {
            name
            for path in ARGOCD_ROLE.rglob("*")
            if path.is_file()
            and path.suffix in {".yml", ".yaml", ".j2"}
            and path.parent.name != "defaults"
            and path.parent.name != "files"
            for name in re.findall(r"\bk3s_[a-z0-9_]+", path.read_text(encoding="utf-8"))
        }
        self.assertIn("k3s_kubeconfig_path", referenced)
        for name in sorted(referenced):
            with self.subTest(variable=name):
                self.assertTrue(
                    name in argocd_defaults,
                    f"add {name} to the argocd role defaults with the k3s role value",
                )
                self.assertEqual(argocd_defaults[name], k3s_defaults[name])


if __name__ == "__main__":
    unittest.main()
