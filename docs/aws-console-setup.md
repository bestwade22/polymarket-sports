# AWS Console setup (one-time)

Region for everything below: **ap-east-1** (Hong Kong) — the same region as
`polymarket-trader`, because Polymarket restricts some regions and Hong Kong
is not on its blocked or close-only list.

Replace placeholders:

| Placeholder | Your value |
|-------------|------------|
| `ACCOUNT_ID` | AWS account ID (12 digits) |
| `OWNER` | GitHub username or org (`bestwade22`) |
| `REPO` | Repository name (`polymarket-sports`) |

Policy JSON files live in [`infrastructure/iam/`](../infrastructure/iam/).

---

## Why the secret is reused

`polymarket-sports` reads its credentials from AWS Secrets Manager at
startup, using the `SECRETS_ARN` environment variable.

There are two ways to wire that up:

| Approach | `BOT_SECRET_ARN` | Where the key lives |
|----------|------------------|---------------------|
| Own secret (default) | unset | GitHub Actions secrets -> CloudFormation parameters -> `polymarket-sports/bot` |
| **Reuse (recommended)** | set to the existing secret ARN | The `polymarket-trader` secret, unchanged |

**The reuse path is recommended.** The wallet private key never has to be
pasted into GitHub, there is only one copy to keep in sync, and rotating
the key means editing one secret instead of two plus the Actions secrets.

Both paths read the same environment variable names, so the code does not
change between them.

---

## Step 1 — GitHub OIDC identity provider

1. AWS Console -> **IAM** -> **Identity providers** -> **Add provider**
2. **Provider type:** OpenID Connect
3. **Provider URL:** `https://token.actions.githubusercontent.com`
4. Click **Get thumbprint** (auto-fills)
5. **Audience:** `sts.amazonaws.com`
6. **Add provider**

If `token.actions.githubusercontent.com` already exists (it does, for
`polymarket-trader`), skip this step.

---

## Step 2 — Deploy IAM role (trust policy)

A **separate role** is used for this repo, so a failure in one bot cannot
break the other's deploy, and the trust policy can be scoped to a single
repository.

1. IAM -> **Roles** -> **Create role**
2. **Trusted entity type:** Web identity
3. **Identity provider:** `token.actions.githubusercontent.com`
4. **Audience:** `sts.amazonaws.com`
5. Skip permission policies for now -> **Next**
6. **Role name:** `github-polymarket-sports-deploy`
7. Create role, then open it -> **Trust relationships** -> **Edit trust policy**
8. Replace the JSON with
   [`github-deploy-trust.json`](../infrastructure/iam/github-deploy-trust.json),
   substituting:
   - `ACCOUNT_ID` -> your account ID
   - `OWNER/polymarket-sports` -> e.g. `bestwade22/polymarket-sports`

   The `sub` condition is scoped to `refs/heads/main` on this one repo.
   If you later push from another branch or a fork, add those subjects
   here too.

9. **Update policy**

Copy the role ARN (you need it for GitHub):

```
arn:aws:iam::ACCOUNT_ID:role/github-polymarket-sports-deploy
```

---

## Step 3 — Deploy IAM role (permissions policy)

1. Open role `github-polymarket-sports-deploy` -> **Permissions** ->
   **Add permissions** -> **Create inline policy**
2. **JSON** tab -> paste contents of
   [`github-deploy-policy.json`](../infrastructure/iam/github-deploy-policy.json)
3. **Review policy** -> name: `github-polymarket-sports-deploy-policy` ->
   **Create policy**

This policy is the `polymarket-trader` one plus the permissions this stack
needs and that stack does not:

| Extra | Why |
|-------|-----|
| `ec2:*` (VPC, subnet, IGW, route table, security group) | The Fargate listener needs a VPC with public subnets |
| `ecs:*` | The ECS cluster, service and task definition |
| `cloudfront:*` | The dashboard distribution, OAC and origin request policy |
| `apigateway:*` | The dashboard API |
| `logs:PutRetentionPolicy` | The listener log group's 30-day retention |
| `s3:GetBucketWebsite` / `s3:PutBucketWebsite` | The dashboard bucket's static site config |

Actions often missing on a minimal policy:

- `s3:TagResource` — SAM bootstrap tags the artifact bucket
- `s3:DeleteBucket` — rollback/cleanup if deploy fails
- `cloudformation:ListStacks` — SAM CLI health checks
- `iam:PassRole` — SAM passes the Lambda/ECS execution roles to the services

---

## Step 4 — GitHub repository variables (before first deploy)

GitHub repo -> **Settings** -> **Secrets and variables** -> **Actions** ->
**Variables**:

| Variable | Value | Required |
|----------|-------|----------|
| `AWS_REGION` | `ap-east-1` | optional (this is the default) |
| `AWS_DEPLOY_ROLE_ARN` | `arn:aws:iam::ACCOUNT_ID:role/github-polymarket-sports-deploy` | **yes** |
| `BOT_SECRET_ARN` | the existing secret ARN (see step 5) | optional, but recommended |

**No repository secrets are needed** when `BOT_SECRET_ARN` is set. If you
leave it unset instead, the stack creates its own secret and you must add
these Actions secrets: `CLOB_PRIVATE_KEY`, `DEPOSIT_WALLET_ADDRESS`,
`API_NINJAS_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

---

## Step 5 — The secret to read

The secret must contain these keys:

```json
{
  "PRIVATE_KEY": "0x...",
  "DEPOSIT_WALLET_ADDRESS": "0x...",
  "API_NINJAS_KEY": "...",
  "TELEGRAM_BOT_TOKEN": "...",
  "TELEGRAM_CHAT_ID": "..."
}
```

`SIGNATURE_TYPE` is optional (defaults to `1`), and `DRY_RUN` is optional
(the deploy passes `true` unless you change it). Extra keys — such as the
`GITHUB_PAT` in the `polymarket-trader` secret — are ignored.

The `polymarket-trader` secret already has all of these. To find its ARN:

1. AWS Console -> **Secrets Manager** -> open the secret
2. Copy the ARN from the secret detail page, or:
   ```
   CloudFormation -> stack polymarket-trader -> Outputs -> TraderSecretArn
   ```

---

## Step 6 — First deploy

**Actions** -> **Deploy NBA Polymarket Bot** -> **Run workflow** (or push to
`main`).

SAM will create:

- Bootstrap stack `aws-sam-cli-managed-default` (S3 artifact bucket)
- Application stack `polymarket-sports` (Lambdas, ECS, CloudFront, S3, IAM)

The deploy is safe to run repeatedly — `sam deploy` with
`--no-fail-on-empty-changeset` does nothing when nothing changed.

### If deploy failed before (ROLLBACK_FAILED)

Clean up in the console **before** re-running deploy:

1. **S3** -> bucket `aws-sam-cli-managed-default-samclisourcebucket-*` -> empty -> **Delete**
2. **CloudFormation** -> stack `aws-sam-cli-managed-default` -> **Delete**
3. Confirm step 3's policy includes `s3:TagResource` and `s3:DeleteBucket`
4. Re-run **Deploy NBA Polymarket Bot**

The workflow's own `Diagnose CloudFormation failure` step prints the stack
status and the last 20 stack events, which is usually enough on its own.

---

## Step 7 — Verify

### CloudFormation

**CloudFormation** -> **Stacks** -> `polymarket-sports` -> status
**CREATE_COMPLETE** (or **UPDATE_COMPLETE**).

Outputs:

| Output | Use |
|--------|-----|
| `BotSecretArn` | confirm which secret the bot reads |
| `DashboardUrl` | the dashboard URL |
| `DashboardApiUrl` | the dashboard API, if you want it directly |
| `StateTableName` | DynamoDB table |
| `TradeQueueUrl` | the trade queue |

### Lambda

**Lambda** -> **Functions**: `polymarket-sports` stack, five functions:

| Function | Schedule |
|----------|----------|
| `FetchDailyFunction` | daily |
| `SnapshotPriceFunction` | every 5 minutes |
| `TradeWorkerFunction` | SQS event |
| `ScaleListenerFunction` | every 5 minutes |
| `DashboardApiFunction` | API Gateway |

### Manual smoke test

Invoke `FetchDailyFunction` from the console and check the log group. It
should report the games it matched against Polymarket. If it reports zero
games, `API_NINJAS_KEY` is not reaching the function.

### Telegram

The bot posts to Telegram from the Lambdas, so the messages arrive while
your laptop is off. `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` must be
present in the secret. If nothing arrives, check the function's log for
`Telegram fetch notification failed`.

---

## Step 8 — Going live

`DRY_RUN` is `true` in the deploy, so no real orders are placed. To trade
for real, set `"DRY_RUN": "false"` **in the secret** — no redeploy needed.
Keep it on paper until the edge has been validated over real games: holding
a losing position to resolution is a 100% loss on that share.
