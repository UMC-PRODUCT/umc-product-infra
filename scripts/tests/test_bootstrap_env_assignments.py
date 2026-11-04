from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import bootstrap_aws_access_keys  # noqa: E402
import bootstrap_nonprod_aws_access_keys  # noqa: E402
import bootstrap_route53_access_keys  # noqa: E402

# re 치환 문자열에서 특별한 뜻을 갖는 값이다. 운영 Secret 원장에는 글자 그대로 써야 한다.
SPECIAL_VALUES = ("\\1", "\\g<0>", "secret\\", "$1", "a\\\\b", "\\n")


class EnvAssignmentTest(unittest.TestCase):
    def test_치환_문법처럼_보이는_값도_그대로_기록한다(self) -> None:
        for module in (bootstrap_aws_access_keys, bootstrap_nonprod_aws_access_keys, bootstrap_route53_access_keys):
            keys = list(module.ENV_KEYS)
            contents = "# keep\n" + "".join(f"{key}=\n" for key in keys) + "OTHER=\\1\n"
            for value in SPECIAL_VALUES:
                with self.subTest(module=module.__name__, value=value):
                    updated = module.replace_env_assignments(contents, dict.fromkeys(keys, value))
                    expected = "# keep\n" + "".join(f"{key}={value}\n" for key in keys) + "OTHER=\\1\n"
                    self.assertEqual(updated, expected)


if __name__ == "__main__":
    unittest.main()
