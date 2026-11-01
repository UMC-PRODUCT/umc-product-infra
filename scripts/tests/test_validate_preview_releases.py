from __future__ import annotations

import copy
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from validate_preview_releases import ROOT, RELEASE_PATTERN, validate_handoff, validate_release


class PreviewReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.record = {"release": {
            "prNumber": "42", "headSha": "123456789012" + "a" * 28,
            "imageTag": "123456789012", "imageDigest": "sha256:" + "b" * 64,
        }}
        self.appset = yaml.safe_load((ROOT / "argocd/applications/preview/applicationset.yaml").read_text())

    def check_record(self, record: dict, name: str = "pr-42.json") -> dict:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / name
            path.write_text(json.dumps(record))
            return validate_release(path)

    def test_숫자로만_시작하는_SHA도_문자열로_보존(self) -> None:
        self.assertEqual(self.check_record(self.record), self.record)

    def test_잘못된_성공기록_거부(self) -> None:
        for key, value in (("prNumber", "43"), ("imageTag", "latest"),
                           ("imageDigest", ""), ("headSha", "a" * 39), ("prNumber", 42)):
            with self.subTest(key=key, value=value):
                record = copy.deepcopy(self.record)
                record["release"][key] = value
                with self.assertRaises(ValueError):
                    self.check_record(record)
        for name in ("pr-042.json", "pr-0.json", "other.json"):
            with self.assertRaises(ValueError):
                self.check_record(self.record, name)

    def test_PR_generator의_필드를_덮는_추가키_거부(self) -> None:
        for record in (dict(self.record, head_sha="a" * 40), {"release": dict(self.record["release"], repository="other")}):
            with self.assertRaises(ValueError):
                self.check_record(record)

    def test_중복_JSON_key와_symlink_거부(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pr-42.json"
            path.write_text('{"release": {}, "release": {}}')
            with self.assertRaises(ValueError):
                validate_release(path)
            link = Path(directory) / "pr-43.json"
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                validate_release(link)

    def test_현재_생성기는_PR과_성공기록만_조합(self) -> None:
        validate_handoff(self.appset)
        self.assertEqual(RELEASE_PATTERN.replace("{{ .number }}", "42"), "argocd/preview-releases/pr-42.json")

    def test_최신_head_직접배포와_head별_기록경로_거부(self) -> None:
        appset = copy.deepcopy(self.appset)
        parameters = appset["spec"]["template"]["spec"]["source"]["helm"]["parameters"]
        next(item for item in parameters if item["name"] == "image.tag")["value"] = "{{ substr 0 12 .head_sha }}"
        with self.assertRaises(ValueError):
            validate_handoff(appset)
        appset = copy.deepcopy(self.appset)
        appset["spec"]["generators"][0]["matrix"]["generators"][1]["git"]["files"][0]["path"] = "releases/{{ .head_sha }}.json"
        with self.assertRaises(ValueError):
            validate_handoff(appset)

    def test_head만_바뀌어도_마지막_성공_Application_spec은_동일(self) -> None:
        # 선언의 단순 field 보간을 검사한다. 실제 controller 동작은 별도 smoke test 대상이다.
        template = json.dumps(self.appset["spec"]["template"]["spec"])

        def rendered(head: str, digest: str) -> str:
            fields = {"number": "42", "head_sha": head, "release.imageTag": self.record["release"]["imageTag"],
                      "release.imageDigest": digest}
            return re.sub(r"{{\s*\.([A-Za-z0-9_.]+)\s*}}", lambda match: fields[match[1]], template)

        digest = self.record["release"]["imageDigest"]
        self.assertEqual(rendered("a" * 40, digest), rendered("c" * 40, digest))
        self.assertNotEqual(rendered("a" * 40, digest), rendered("c" * 40, "sha256:" + "d" * 64))


if __name__ == "__main__":
    unittest.main()
