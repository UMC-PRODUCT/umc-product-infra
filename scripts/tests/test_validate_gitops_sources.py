from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from validate_gitops_sources import validate_argocd_tree, validate_chart_version_bump

APPLICATION = "apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata:\n  name: demo\n"
CHART = "charts/umc-product-server"
ROOT_APP = """apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: root
spec:
  source:
    path: argocd
    directory:
      recurse: true
      exclude: {exclude}
"""


class GitRepository:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.git("init", "--quiet", "--initial-branch=main")
        self.write(".gitignore", ".DS_Store\n")
        self.write("bootstrap/root-app.yaml", ROOT_APP.format(exclude="preview-releases/*"))
        self.write("argocd/applications/demo.yaml", APPLICATION)
        self.write("argocd/preview-releases/pr-1.json", "{}")
        self.write(f"{CHART}/Chart.yaml", "apiVersion: v2\nname: demo\nversion: 0.4.13\n")
        self.write(f"{CHART}/templates/deployment.yaml", "kind: Deployment\n")
        self.write(f"{CHART}/values-dev.yaml", "image: {}\n")
        self.write(f"{CHART}/README.md", "chart\n")
        self.commit("base")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.git("checkout", "--quiet", "-b", "feature")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root), "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
            check=True, capture_output=True, text=True,
        ).stdout

    def write(self, name: str, text: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, message: str) -> None:
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", message)


class GitOpsSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = GitRepository(Path(directory.name))

    def test_manifest와_README와_ignore된_DS_Store는_허용(self) -> None:
        self.repo.write("argocd/applications/README.md", "doc\n")
        self.repo.write("argocd/applications/.DS_Store", "finder")
        self.assertEqual(validate_argocd_tree(self.repo.root), 1)

    def test_root_Application이_읽을_비manifest_파일은_거부(self) -> None:
        for name, text in (
            ("argocd/applications/release.json", '{"release": {}}'),
            ("argocd/applications/demo.yml", APPLICATION),
            ("argocd/applications/values.yaml", "replicas: 1\n"),
            ("argocd/applications/empty.yaml", ""),
            ("argocd/applications/notes.txt", "memo\n"),
        ):
            with self.subTest(name=name):
                self.repo.write(name, text)
                with self.assertRaises(ValueError):
                    validate_argocd_tree(self.repo.root)
                (self.repo.root / name).unlink()

    def test_root_Application의_exclude를_그대로_따름(self) -> None:
        self.repo.write("argocd/scratch/notes.json", "{}")
        with self.assertRaises(ValueError):
            validate_argocd_tree(self.repo.root)
        self.repo.write("bootstrap/root-app.yaml", ROOT_APP.format(exclude="'{preview-releases/*,scratch/*}'"))
        self.assertEqual(validate_argocd_tree(self.repo.root), 1)
        self.repo.write("bootstrap/root-app.yaml", ROOT_APP.format(exclude="scratch/*"))
        with self.assertRaises(ValueError):
            validate_argocd_tree(self.repo.root)

    def test_추적된_DS_Store는_거부(self) -> None:
        self.repo.write("argocd/.DS_Store", "finder")
        self.repo.git("add", "--force", "argocd/.DS_Store")
        with self.assertRaises(ValueError):
            validate_argocd_tree(self.repo.root)

    def test_Chart_yaml_밖의_chart_파일을_바꾸면_version도_바꿔야_함(self) -> None:
        for name in (
            f"{CHART}/templates/deployment.yaml", f"{CHART}/values-dev.yaml", f"{CHART}/files/new.sql",
            f"{CHART}/README.md", f"{CHART}/.helmignore", f"{CHART}/crds/new.yaml",
        ):
            with self.subTest(name=name):
                self.repo.git("checkout", "--quiet", "--", ".")
                self.repo.git("clean", "--quiet", "-fd")
                self.repo.write(name, "changed: true\n")
                with self.assertRaises(ValueError):
                    validate_chart_version_bump(self.repo.root, "origin/main")

    def test_version을_올린_commit은_통과(self) -> None:
        self.repo.write(f"{CHART}/templates/deployment.yaml", "kind: StatefulSet\n")
        self.repo.write(f"{CHART}/Chart.yaml", "apiVersion: v2\nname: demo\nversion: 0.4.14\n")
        self.repo.commit("bump")
        self.assertIn("0.4.13 -> 0.4.14", validate_chart_version_bump(self.repo.root, "origin/main"))

    def test_Chart_yaml만_바꾸면_bump_불필요(self) -> None:
        self.repo.write(f"{CHART}/Chart.yaml", "apiVersion: v2\nname: demo\ndescription: demo\nversion: 0.4.13\n")
        self.repo.commit("describe")
        self.assertIn("unchanged", validate_chart_version_bump(self.repo.root, "origin/main"))

    def test_main_push는_merge_base가_HEAD라_비교할_변경이_없음(self) -> None:
        self.repo.git("checkout", "--quiet", "main")
        self.repo.write(f"{CHART}/values-dev.yaml", "image: {tag: bot}\n")
        self.repo.commit("deploy bot")
        self.repo.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.assertIn("unchanged", validate_chart_version_bump(self.repo.root, "origin/main"))

    def test_base가_main에_새로_올라와도_merge_base_이후_변경만_비교(self) -> None:
        self.repo.git("checkout", "--quiet", "main")
        self.repo.write(f"{CHART}/values-dev.yaml", "image: {tag: bot}\n")
        self.repo.commit("deploy bot")
        self.repo.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.repo.git("checkout", "--quiet", "feature")
        self.assertIn("unchanged", validate_chart_version_bump(self.repo.root, "origin/main"))


if __name__ == "__main__":
    unittest.main()
