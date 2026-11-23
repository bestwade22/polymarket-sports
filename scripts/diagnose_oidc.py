#!/usr/bin/env python3
"""Diagnose why sts:AssumeRoleWithWebIdentity was denied.

Compares the OIDC token's real claims against the role's real trust
policy and reports exactly which condition failed, instead of leaving
it to guesswork.

Usage:
    python3 scripts/diagnose_oidc.py <role-arn> [token]

The token is optional. When it is not passed, the script reads it from
the ACTIONS_ID_TOKEN_REQUEST_* environment variables that GitHub sets
for the job.
"""

from __future__ import annotations

import base64
import fnmatch
import json
import os
import re
import subprocess
import sys
import urllib.request

REGION = os.environ.get("AWS_REGION", "ap-east-1")


def _aws(*args: str) -> tuple[int, str]:
    """Runs an AWS CLI call and returns (exit code, combined output)."""
    try:
        proc = subprocess.run(
            ["aws", *args, "--region", REGION],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"could not run aws cli: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def fetch_token() -> str:
    """Fetches the job's OIDC token from the GitHub token endpoint."""
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    auth = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not url or not auth:
        return ""
    request = urllib.request.Request(
        url if "&" in url else f"{url}&audience=sts.amazonaws.com",
        headers={"Authorization": f"bearer {auth}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response).get("value", "")
    except Exception as exc:  # noqa: BLE001 - diagnostics must never crash
        print(f"(token request failed: {exc})")
        return ""


def decode_claims(token: str) -> dict:
    """Decodes the payload of a JWT without verifying its signature."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception as exc:  # noqa: BLE001 - diagnostics must never crash
        print(f"(could not decode token: {exc})")
        return {}


def get_role(role_name: str) -> dict | None:
    """Reads the role, or None when it is missing or unreadable."""
    code, out = _aws("iam", "get-role", "--role-name", role_name)
    if code != 0:
        print(f"    iam get-role failed:\n{out.strip()}")
        return None
    try:
        return json.loads(out)["Role"]
    except (ValueError, KeyError):
        print(f"    unexpected get-role output:\n{out.strip()}")
        return None


def list_providers() -> list[str]:
    """Lists the account's OpenID Connect providers."""
    code, out = _aws("iam", "list-open-id-connect-providers")
    if code != 0:
        return []
    try:
        return json.loads(out).get("OpenIdConnectProviderList", [])
    except ValueError:
        return []


def _sub_matches(pattern: str, subject: str) -> bool:
    """Matches an IAM sub condition against a token subject.

    IAM's StringLike treats '*' as a wildcard; StringEquals does not.
    """
    if not subject:
        return False
    if "*" in pattern:
        return fnmatch.fnmatchcase(subject, pattern)
    return pattern == subject


def _trust_principals(trust: dict | None) -> list[str]:
    """Collects every trusted Federated principal from the trust policy."""
    principals: list[str] = []
    for statement in (trust or {}).get("Statement", []):
        federated = statement.get("Principal", {}).get("Federated", [])
        if isinstance(federated, str):
            federated = [federated]
        principals.extend(federated)
    return principals


def _trust_conditions(trust: dict | None) -> dict[str, list[str]]:
    """Collects every condition key and its expected values."""
    conditions: dict[str, list[str]] = {}
    for statement in (trust or {}).get("Statement", []):
        for mapping in statement.get("Condition", {}).values():
            for key, expected in mapping.items():
                values = [expected] if isinstance(expected, str) else list(expected)
                conditions.setdefault(key, []).extend(values)
    return conditions


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    role_arn = sys.argv[1]
    role_name = role_arn.rsplit("/", 1)[-1]
    account_id = role_arn.split(":")[4] if role_arn.count(":") >= 4 else "?"

    print("=" * 72)
    print("OIDC assume-role diagnosis")
    print("=" * 72)
    print(f"Role ARN   : {role_arn}")
    print(f"Role name  : {role_name}")
    print(f"Account ID : {account_id}")
    print(f"Region     : {REGION}")
    print()

    # 1. Does the role exist?
    print("[1] Role lookup")
    role = get_role(role_name)
    if role is None:
        print("    RESULT: the role does NOT exist, or the caller cannot see it.")
        print("    >>> A missing role and a broken trust policy produce the same")
        print("    >>> STS error message, so this is worth ruling out first.")
        print()
    else:
        print(f"    role exists : {role.get('Arn')}")
        print(f"    role id     : {role.get('RoleId')}")
        print(f"    created     : {role.get('CreateDate', '')}")
        print()

    # 2. Does the OIDC provider exist?
    print("[2] OpenID Connect providers in this account")
    providers = list_providers()
    if not providers:
        print("    RESULT: no OIDC providers found, or the caller cannot list them.")
        print("    >>> Add token.actions.githubusercontent.com under")
        print("    >>> IAM -> Identity providers -> Add provider.")
        print()
    else:
        for provider in providers:
            print(f"    {provider}")
            if provider.rsplit("/", 1)[-1] == "token.actions.githubusercontent.com":
                print("       ^ the GitHub provider")
        print()

    # 3. The trust policy as AWS stores it.
    print("[3] Trust policy as stored in AWS")
    trust = role.get("AssumeRolePolicyDocument") if role else None
    if not trust:
        print("    RESULT: could not read the trust policy.")
        print()
    else:
        print(json.dumps(trust, indent=4))
        print()

    # 4. The token's real claims.
    print("[4] OIDC token claims")
    token = sys.argv[2] if len(sys.argv) > 2 else fetch_token()
    claims = decode_claims(token) if token else {}
    if not claims:
        print("    RESULT: could not obtain or decode the OIDC token.")
        print("    >>> Check that the workflow grants `id-token: write`.")
        print()
        return 1

    for key in ("iss", "aud", "sub", "ref", "repository", "repository_owner"):
        if key in claims:
            print(f"    {key:17}: {claims[key]}")
    print()

    # 5. Compare the two.
    print("[5] Comparison")
    problems: list[str] = []

    subject = claims.get("sub", "")
    audiences = claims.get("aud", [])
    if isinstance(audiences, str):
        audiences = [audiences]
    issuer = claims.get("iss", "")

    principals = _trust_principals(trust)
    print(f"    trust allows principals : {principals}")

    issuer_arn = issuer.replace("https://", "").rstrip("/")
    if issuer_arn:
        matched = [p for p in principals if issuer_arn in p]
        if matched:
            print(f"    issuer {issuer_arn}: MATCHES {matched}")
        else:
            print(f"    issuer {issuer_arn}: DOES NOT MATCH any trusted principal")
            problems.append(
                "The trust policy does not trust the provider that signed this "
                "token. The provider ARN must be exactly "
                "arn:aws:iam::"
                f"{account_id}:oidc-provider/token.actions.githubusercontent.com"
            )
        # A principal that still contains a placeholder names no real
        # provider, so it will never be matched by AWS even though the
        # issuer substring is present.
        for principal in principals:
            if "ACCOUNT_ID" in principal or "OWNER" in principal:
                problems.append(
                    f"The trusted principal still contains a placeholder:\n"
                    f"        {principal}\n"
                    "    >>> Replace ACCOUNT_ID with "
                    f"{account_id} before applying the trust policy."
                )

    print(f"    token audiences : {audiences}")
    if "sts.amazonaws.com" not in audiences:
        problems.append(
            "The token was not issued for the sts.amazonaws.com audience."
        )

    conditions = _trust_conditions(trust)
    sub_key = "token.actions.githubusercontent.com:sub"
    sub_conditions = conditions.get(sub_key, [])
    if not sub_conditions:
        print(f"    {sub_key}: NOT PRESENT in the trust policy")
        problems.append(
            "The trust policy has no sub condition. That would accept any token "
            "from the provider, so the policy shown above is probably not the "
            "one actually attached to the role."
        )
    else:
        print(f"    {sub_key} conditions: {sub_conditions}")
        if any(_sub_matches(p, subject) for p in sub_conditions):
            print(f"    token sub {subject}: MATCHES")
        else:
            print(f"    token sub {subject}: DOES NOT MATCH")
            problems.append(
                "The trust policy's sub condition does not match the token's "
                "subject.\n"
                f"        token     : {subject}\n"
                f"        condition : {sub_conditions}\n"
                "    >>> Put the token subject above into the trust policy verbatim."
            )
            # Repositories created after July 15, 2026 get an immutable
            # subject that embeds the numeric owner and repository ids:
            # repo:owner@ID/repo@ID:ref:refs/heads/main. A condition
            # written in the older repo:owner/repo:ref form never
            # matches those, and the difference is easy to miss.
            if re.search(r"@\d+", subject):
                problems.append(
                    "This token's subject carries GitHub's numeric id form.\n"
                    f"        actual   : {subject}\n"
                    "    >>> A condition like repo:owner/repo:ref:refs/heads/main\n"
                    "    >>> will never match it. Use a wildcard instead:\n"
                    "    >>>   repo:bestwade22*polymarket-sports*:ref:refs/heads/main"
                )
        for pattern in sub_conditions:
            if "OWNER" in pattern or "ACCOUNT_ID" in pattern or "REPO" in pattern:
                problems.append(
                    f"The sub condition still contains a placeholder:\n"
                    f"        {pattern}\n"
                    "    >>> Replace OWNER with the GitHub owner "
                    "(bestwade22) and REPO with the repository name."
                )

    # 6. Verdict.
    print()
    print("=" * 72)
    if problems:
        print("VERDICT: the trust policy does not authorise this token.")
        print()
        for index, problem in enumerate(problems, 1):
            print(f"{index}. {problem}")
            print()
    else:
        print("VERDICT: the trust policy appears to authorise this token.")
        print("If AssumeRoleWithWebIdentity still fails, check:")
        print("  - whether the role's permissions policy allows the action")
        print("  - whether the workflow's `audience` input matches the token")
    print("=" * 72)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
