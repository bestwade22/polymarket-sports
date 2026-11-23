"""Tests for the OIDC diagnostic script.

The script is what tells us *why* sts:AssumeRoleWithWebIdentity was
denied, so its comparison logic is worth pinning down: a wrong verdict
here sends the debugging in the wrong direction.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "diagnose_oidc.py"

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import diagnose_oidc  # noqa: E402


def make_token(claims: dict) -> str:
    """Builds a JWT-shaped string with the given claims."""
    def segment(payload: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        return raw.rstrip("=")

    return f"{segment({'alg': 'RS256'})}.{segment(claims)}.sig"


TOKEN_CLAIMS = {
    "iss": "https://token.actions.githubusercontent.com",
    "aud": "sts.amazonaws.com",
    "sub": "repo:bestwade22@31284603/polymarket-sports@1469031647:ref:refs/heads/main",
    "ref": "refs/heads/main",
}

# GitHub uses the previous repo:owner/repo:ref subject for repositories
# created before July 15, 2026, and an immutable subject carrying the
# numeric owner and repository ids for ones created after. A trust
# policy written in the wrong form stops matching.
LEGACY_TOKEN_CLAIMS = {
    "iss": "https://token.actions.githubusercontent.com",
    "aud": "sts.amazonaws.com",
    "sub": "repo:bestwade22/polymarket-sports:ref:refs/heads/main",
    "ref": "refs/heads/main",
}

CORRECT_TRUST = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {
                "Federated": (
                    "arn:aws:iam::830430162436:oidc-provider/"
                    "token.actions.githubusercontent.com"
                )
            },
            "Action": "sts:AssumeRoleWithWebIdentity",
            "Condition": {
                "StringEquals": {
                    "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
                },
                "StringLike": {
                    "token.actions.githubusercontent.com:sub": (
                        "repo:bestwade22*polymarket-sports*:ref:refs/heads/main"
                    )
                },
            },
        }
    ],
}


class TestDecodeClaims:
    def test_decodes_the_payload(self):
        claims = diagnose_oidc.decode_claims(make_token(TOKEN_CLAIMS))
        assert claims["sub"] == TOKEN_CLAIMS["sub"]
        assert claims["aud"] == "sts.amazonaws.com"

    def test_survives_a_malformed_token(self):
        assert diagnose_oidc.decode_claims("not-a-jwt") == {}

    def test_survives_an_empty_token(self):
        assert diagnose_oidc.decode_claims("") == {}


class TestSubMatches:
    def test_exact_match(self):
        subject = "repo:bestwade22/polymarket-sports:ref:refs/heads/main"
        assert diagnose_oidc._sub_matches(subject, subject)

    def test_wildcard_matches(self):
        pattern = "repo:bestwade22/polymarket-sports:ref:refs/heads/*"
        subject = "repo:bestwade22/polymarket-sports:ref:refs/heads/main"
        assert diagnose_oidc._sub_matches(pattern, subject)

    def test_different_repo_does_not_match(self):
        pattern = "repo:bestwade22/polymarket-trader:ref:refs/heads/main"
        subject = "repo:bestwade22/polymarket-sports:ref:refs/heads/main"
        assert not diagnose_oidc._sub_matches(pattern, subject)

    def test_missing_repository_segment_does_not_match(self):
        """The console wizard can drop the repo name - it must not match."""
        pattern = "repo:bestwade22:ref:refs/heads/main"
        subject = "repo:bestwade22/polymarket-sports:ref:refs/heads/main"
        assert not diagnose_oidc._sub_matches(pattern, subject)

    def test_legacy_pattern_does_not_match_the_numeric_id_subject(self):
        """The subject format changed - the old condition must not match."""
        pattern = "repo:bestwade22/polymarket-sports:ref:refs/heads/main"
        subject = (
            "repo:bestwade22@31284603/polymarket-sports@1469031647:"
            "ref:refs/heads/main"
        )
        assert not diagnose_oidc._sub_matches(pattern, subject)

    def test_wildcard_matches_the_numeric_id_subject(self):
        pattern = "repo:bestwade22*polymarket-sports*:ref:refs/heads/main"
        subject = (
            "repo:bestwade22@31284603/polymarket-sports@1469031647:"
            "ref:refs/heads/main"
        )
        assert diagnose_oidc._sub_matches(pattern, subject)

    def test_wildcard_matches_the_legacy_subject(self):
        pattern = "repo:bestwade22*polymarket-sports*:ref:refs/heads/main"
        subject = "repo:bestwade22/polymarket-sports:ref:refs/heads/main"
        assert diagnose_oidc._sub_matches(pattern, subject)

    def test_wildcard_still_rejects_another_repository(self):
        pattern = "repo:bestwade22*polymarket-sports*:ref:refs/heads/main"
        subject = (
            "repo:bestwade22@31284603/polymarket-trader@123:ref:refs/heads/main"
        )
        assert not diagnose_oidc._sub_matches(pattern, subject)

    def test_wildcard_still_rejects_another_branch(self):
        pattern = "repo:bestwade22*polymarket-sports*:ref:refs/heads/main"
        subject = (
            "repo:bestwade22@31284603/polymarket-sports@1469031647:"
            "ref:refs/heads/dev"
        )
        assert not diagnose_oidc._sub_matches(pattern, subject)

    def test_empty_subject_never_matches(self):
        assert not diagnose_oidc._sub_matches("repo:bestwade22/x", "")


class TestTrustExtraction:
    def test_collects_federated_principals(self):
        principals = diagnose_oidc._trust_principals(CORRECT_TRUST)
        assert principals == [
            "arn:aws:iam::830430162436:oidc-provider/"
            "token.actions.githubusercontent.com"
        ]

    def test_handles_a_missing_trust_policy(self):
        assert diagnose_oidc._trust_principals(None) == []
        assert diagnose_oidc._trust_conditions(None) == {}

    def test_collects_condition_values(self):
        conditions = diagnose_oidc._trust_conditions(CORRECT_TRUST)
        assert conditions["token.actions.githubusercontent.com:sub"] == [
            "repo:bestwade22*polymarket-sports*:ref:refs/heads/main"
        ]
        assert conditions["token.actions.githubusercontent.com:aud"] == [
            "sts.amazonaws.com"
        ]


class TestEndToEnd:
    """Runs the script against a mocked AWS CLI."""

    @pytest.fixture()
    def mock_aws(self, tmp_path, monkeypatch):
        """Installs a fake `aws` CLI that returns a canned role."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        trust_file = tmp_path / "role.json"

        def write_role(trust: dict, exists: bool = True) -> None:
            payload = {
                "Role": {
                    "Arn": (
                        "arn:aws:iam::830430162436:role/"
                        "github-polymarket-sports-deploy"
                    ),
                    "RoleId": "AROATEST",
                    "CreateDate": "2026-11-23T10:00:00+00:00",
                    "AssumeRolePolicyDocument": trust,
                }
            }
            trust_file.write_text(json.dumps(payload))

        script = bin_dir / "aws"
        script.write_text(
            "#!/bin/bash\n"
            'args=""; skip=0\n'
            'for a in "$@"; do\n'
            '  if [ "$a" = "--region" ]; then skip=1; continue; fi\n'
            '  if [ "$skip" = "1" ]; then skip=0; continue; fi\n'
            '  args="$args $a"\n'
            "done\n"
            'args="${args# }"\n'
            'case "$args" in\n'
            '  "iam get-role "*)\n'
            f'    cat "{trust_file}"\n'
            "    ;;\n"
            '  "iam list-open-id-connect-providers"*)\n'
            "    echo '{\"OpenIdConnectProviderList\":[\"arn:aws:iam::"
            '830430162436:oidc-provider/token.actions.githubusercontent.com"]}\'\n'
            "    ;;\n"
            "  *) exit 1 ;;\n"
            "esac\n"
        )
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
        return write_role, trust_file

    ROLE = "arn:aws:iam::830430162436:role/github-polymarket-sports-deploy"

    def _run(self, role_arn: str, token: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), role_arn, token],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_correct_trust_policy_is_authorised(self, mock_aws):
        write_role, _ = mock_aws
        write_role(CORRECT_TRUST)
        result = self._run(self.ROLE, make_token(TOKEN_CLAIMS))
        assert "VERDICT: the trust policy appears to authorise this token." in (
            result.stdout
        )
        assert result.returncode == 0

    def test_owner_placeholder_is_reported(self, mock_aws):
        write_role, _ = mock_aws
        broken = json.loads(json.dumps(CORRECT_TRUST))
        broken["Statement"][0]["Condition"]["StringLike"][
            "token.actions.githubusercontent.com:sub"
        ] = "repo:OWNER/polymarket-sports:ref:refs/heads/main"
        write_role(broken)
        result = self._run(self.ROLE, make_token(TOKEN_CLAIMS))
        assert "DOES NOT MATCH" in result.stdout
        assert "OWNER" in result.stdout
        assert result.returncode == 1

    def test_account_id_placeholder_is_reported(self, mock_aws):
        write_role, _ = mock_aws
        broken = json.loads(json.dumps(CORRECT_TRUST))
        broken["Statement"][0]["Principal"]["Federated"] = (
            "arn:aws:iam::ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"
        )
        write_role(broken)
        result = self._run(self.ROLE, make_token(TOKEN_CLAIMS))
        assert "ACCOUNT_ID" in result.stdout
        assert result.returncode == 1

    def test_missing_repository_segment_is_reported(self, mock_aws):
        write_role, _ = mock_aws
        broken = json.loads(json.dumps(CORRECT_TRUST))
        broken["Statement"][0]["Condition"]["StringLike"][
            "token.actions.githubusercontent.com:sub"
        ] = "repo:bestwade22:ref:refs/heads/main"
        write_role(broken)
        result = self._run(self.ROLE, make_token(TOKEN_CLAIMS))
        assert "DOES NOT MATCH" in result.stdout
        assert result.returncode == 1

    def test_wrong_audience_is_reported(self, mock_aws):
        write_role, _ = mock_aws
        write_role(CORRECT_TRUST)
        claims = dict(TOKEN_CLAIMS, aud="my-github-app")
        result = self._run(self.ROLE, make_token(claims))
        assert "sts.amazonaws.com audience" in result.stdout
        assert result.returncode == 1

    def test_legacy_condition_is_reported_against_the_numeric_id_subject(
        self, mock_aws
    ):
        """The real failure: an old-format condition against a new token."""
        write_role, _ = mock_aws
        legacy = json.loads(json.dumps(CORRECT_TRUST))
        legacy["Statement"][0]["Condition"]["StringLike"][
            "token.actions.githubusercontent.com:sub"
        ] = "repo:bestwade22/polymarket-sports:ref:refs/heads/main"
        write_role(legacy)
        result = self._run(self.ROLE, make_token(TOKEN_CLAIMS))
        assert "DOES NOT MATCH" in result.stdout
        assert "numeric id form" in result.stdout
        assert result.returncode == 1

    def test_the_shipped_trust_policy_matches_the_real_token(self, mock_aws):
        """The policy in the repo must accept the token GitHub issues."""
        write_role, _ = mock_aws
        shipped = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "infrastructure"
                / "iam"
                / "github-deploy-trust.json"
            ).read_text()
        )
        write_role(shipped)
        result = self._run(self.ROLE, make_token(TOKEN_CLAIMS))
        assert "VERDICT: the trust policy appears to authorise this token." in (
            result.stdout
        )
        assert result.returncode == 0
