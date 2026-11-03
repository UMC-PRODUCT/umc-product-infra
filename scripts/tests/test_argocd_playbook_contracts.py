from __future__ import annotations

import json
import subprocess
import sys
import tempfile
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
READ_ONLY_MODULES = {"ansible.builtin.stat", "ansible.builtin.assert", "ansible.builtin.debug"}


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

    def test_every_safety_check_is_a_preflight_pre_task(self) -> None:
        pre_tasks = load_play()["pre_tasks"]
        for task in pre_tasks:
            with self.subTest(task=task["name"]):
                self.assertIn("preflight", task["tags"])
        names = [task["name"] for task in pre_tasks]
        self.assertIn("Require a clean checkout of origin/main", names)
        self.assertEqual(
            pre_tasks[-1]["ansible.builtin.import_role"]["tasks_from"], "installed_preflight.yml"
        )

    def test_checkout_guard_reads_the_controller_even_in_check_mode(self) -> None:
        git_tasks = [
            task for task in load_play()["pre_tasks"]
            if task.get("ansible.builtin.command", {}).get("argv", [None])[0] == "git"
        ]
        self.assertEqual(
            [task["ansible.builtin.command"]["argv"][3] for task in git_tasks],
            ["fetch", "status", "rev-parse"],
        )
        self.assertEqual(git_tasks[-1]["ansible.builtin.command"]["argv"][4:], ["HEAD", "origin/main"])
        for task in git_tasks:
            with self.subTest(task=task["name"]):
                self.assertEqual(task["delegate_to"], "localhost")
                self.assertIs(task["check_mode"], False)
                self.assertIs(task["changed_when"], False)


def installed_preflight() -> list[dict[str, object]]:
    return walk_tasks(load_yaml(ARGOCD_ROLE / "tasks" / "installed_preflight.yml"))


def task_named(tasks: list[dict[str, object]], name: str) -> dict[str, object]:
    return next(task for task in tasks if task.get("name") == name)


def run_values_diff(live: object, git_text: str, tmp_dir: Path) -> str:
    script = task_named(installed_preflight(), "Compare the live Argo CD Helm values with Git")
    git_values = tmp_dir / "values.yaml"
    git_values.write_text(git_text, encoding="utf-8")
    argv = script["ansible.builtin.command"]["argv"]
    return subprocess.run(
        [sys.executable, "-I", argv[1], argv[2], str(git_values)],
        input=json.dumps(live), capture_output=True, text=True, check=True,
    ).stdout


# 사전 검사와 values 미리보기는 서버를 바꾸지 않아야 --tags preflight 실행이 안전하다.
class ArgocdInstalledPreflightTests(unittest.TestCase):
    def test_preflight_tasks_never_write(self) -> None:
        for task in installed_preflight():
            with self.subTest(task=task["name"]):
                if READ_ONLY_MODULES & task.keys():
                    continue
                self.assertIn("ansible.builtin.command", task)
                self.assertIs(task["check_mode"], False)
                argv = task["ansible.builtin.command"]["argv"]
                if task.get("delegate_to") == "localhost":
                    self.assertEqual(argv[0], "{{ ansible_playbook_python }}")
                    continue
                self.assertIs(task["changed_when"], False)
                self.assertIn(argv[1:3], (["list", "--kubeconfig"], ["get", "values"], ["--version"]))

    def test_live_diff_reads_what_the_helm_task_compares(self) -> None:
        main = walk_tasks(load_yaml(ARGOCD_ROLE / "tasks" / "main.yml"))
        copy = task_named(main, "Copy the reviewed Argo CD values")["ansible.builtin.copy"]
        helm = task_named(main, "Reconcile the pinned Argo CD Helm release")["kubernetes.core.helm"]
        tasks = installed_preflight()
        live = task_named(tasks, "Read the live Argo CD Helm values")["ansible.builtin.command"]["argv"]
        diff = task_named(tasks, "Compare the live Argo CD Helm values with Git")
        self.assertIs(helm["reset_values"], True)
        self.assertEqual(helm["values_files"], [copy["dest"]])
        self.assertEqual(
            diff["ansible.builtin.command"]["argv"][-1], "{{ role_path }}/files/" + copy["src"]
        )
        self.assertEqual(live[3], helm["release_name"])
        self.assertEqual(live[live.index("--namespace") + 1], helm["release_namespace"])
        self.assertEqual(live[live.index("--kubeconfig") + 1], helm["kubeconfig"])
        self.assertEqual(live[live.index("--output") + 1], "json")

    def test_values_diff_ignores_formatting_and_shows_value_changes(self) -> None:
        git_text = "# comment\nserver:\n  replicas: 1\nconfigs:\n  cm: {a: b}\n"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            same = {"configs": {"cm": {"a": "b"}}, "server": {"replicas": 1}}
            self.assertEqual(run_values_diff(same, git_text, tmp_dir), "")
            changed = run_values_diff({"server": {"replicas": 2}}, git_text, tmp_dir)
            self.assertIn("-  replicas: 2", changed)
            self.assertIn("+  replicas: 1", changed)
            self.assertIn("+configs:", changed)
            self.assertIn("+configs:", run_values_diff(None, git_text, tmp_dir))




if __name__ == "__main__":
    unittest.main()
