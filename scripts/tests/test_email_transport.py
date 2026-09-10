"""메일 provider 전환 시 발신자·Secret·네트워크 경계를 검증한다."""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("helm"), "helm이 필요합니다")
class EmailTransportTests(unittest.TestCase):
    def render(self, environment: str, *overrides: str) -> subprocess.CompletedProcess:
        command = [
            "helm", "template", "umc-product-server", "charts/umc-product-server",
            "--namespace", "app" if environment == "prod" else "dev-app",
            "-f", f"charts/umc-product-server/values-{environment}.yaml",
        ]
        for override in overrides:
            command.extend(["--set-string", override])
        return subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)

    def documents(self, result: subprocess.CompletedProcess) -> list[dict]:
        self.assertEqual(result.returncode, 0, result.stderr)
        return [item for item in yaml.safe_load_all(result.stdout) if item]

    def check_network(self, documents: list[dict], smtp: bool) -> None:
        policies = [item["spec"] for item in documents if item["kind"] == "NetworkPolicy"]
        self.assertEqual(len(policies), 2)
        allow = next(policy for policy in policies if policy.get("egress"))
        self.assertEqual(allow["policyTypes"], ["Ingress", "Egress"])
        self.assertEqual(allow["ingress"][0]["ports"], [{"protocol": "TCP", "port": 8080}])
        public = next(rule for rule in allow["egress"] if "ipBlock" in rule["to"][0])
        self.assertEqual(public["ports"], [
            {"protocol": "TCP", "port": port} for port in ([443, 587] if smtp else [443])
        ])
        block = public["to"][0]["ipBlock"]
        self.assertEqual(block["cidr"], "0.0.0.0/0")
        for cidr in ("10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "100.64.0.0/10"):
            self.assertIn(cidr, block["except"])

    def test_smtp_uses_secret_and_outbound_only(self) -> None:
        for environment in ("prod", "dev"):
            with self.subTest(environment=environment):
                overrides = () if environment == "dev" else (
                    "env.EMAIL_PROVIDER=smtp", "env.SMTP_HOST=smtp.gmail.com",
                    "env.SMTP_PORT=587", "env.SMTP_USERNAME=umcproduct1227@gmail.com",
                    "env.EMAIL_NO_REPLY_ADDRESS=umcproduct1227@gmail.com",
                )
                documents = self.documents(self.render(environment, *overrides))
                self.check_network(documents, smtp=True)
                deployment = next(item for item in documents if item["kind"] == "Deployment")
                container = deployment["spec"]["template"]["spec"]["containers"][0]
                env = {item["name"]: item["value"] for item in container["env"]}
                self.assertEqual(env["EMAIL_PROVIDER"], "smtp")
                self.assertEqual(env["EMAIL_NO_REPLY_ADDRESS"], env["SMTP_USERNAME"])
                self.assertNotIn("SMTP_PASSWORD", env)
                self.assertIn({"secretRef": {"name": "app-email", "optional": False}}, container["envFrom"])

    def test_prod_keeps_ses_until_dev_verification(self) -> None:
        documents = self.documents(self.render("prod"))
        self.check_network(documents, smtp=False)
        deployment = next(item for item in documents if item["kind"] == "Deployment")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        env = {item["name"]: item["value"] for item in container["env"]}
        self.assertEqual(env["EMAIL_PROVIDER"], "ses")
        self.assertEqual(env["EMAIL_NO_REPLY_ADDRESS"], "no-reply@university.neordinary.com")
        self.assertNotIn("SMTP_USERNAME", env)

    def test_ses_rollback_restores_sender_and_closes_smtp_egress(self) -> None:
        for environment in ("prod", "dev"):
            with self.subTest(environment=environment):
                sender = ("no-reply@university.neordinary.com" if environment == "prod"
                          else "no-reply-nonprod@university.neordinary.com")
                documents = self.documents(self.render(
                    environment, "env.EMAIL_PROVIDER=ses", f"env.EMAIL_NO_REPLY_ADDRESS={sender}"
                ))
                self.check_network(documents, smtp=False)

    def test_rejects_wrong_transport_sender_and_plaintext_password(self) -> None:
        for override in (
            "env.EMAIL_PROVIDER=other", "env.EMAIL_PROVIDER=", "env.SMTP_HOST=untrusted.example.com",
            "env.SMTP_PORT=25", "env.SMTP_USERNAME=another@example.com",
            "env.EMAIL_NO_REPLY_ADDRESS=no-reply@university.neordinary.com",
            "env.SMTP_PASSWORD=synthetic-not-a-real-secret",
        ):
            with self.subTest(override=override):
                self.assertNotEqual(self.render("dev", override).returncode, 0)

    def test_preview_secret_does_not_receive_gmail_password(self) -> None:
        result = subprocess.run(
            ["helm", "template", "umc-secrets", "charts/umc-secrets"],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        for item in self.documents(result):
            if item["kind"] == "ExternalSecret" and item["metadata"]["name"] == "app-email":
                keys = {datum["secretKey"] for datum in item["spec"]["data"]}
                self.assertEqual("SMTP_PASSWORD" in keys, item["metadata"]["namespace"] != "preview")


if __name__ == "__main__":
    unittest.main()
