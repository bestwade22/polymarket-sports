# NBA Polymarket Trading Bot

Automated trading bot for Polymarket NBA moneyline markets.

The strategy: take the **favourite** team's pre-game price, watch the live
book, and buy when the price has collapsed. When the price recovers to the
original pre-game level, a resting sell order takes the profit.

## How it works

```
16:00 UTC   fetch-daily      API Ninjas schedule + standings (by ET date)
  11:00 ET                    → Gamma NBA events (series 10345)
                            → match on (away, home, ET date)
                            → DynamoDB GAME#<slug>/META (status=scheduled)
                            → derive the listener window from the tip-offs

every 5 min  snapshot-price  games inside the pre-game window
                            → CLOB /book for both teams' tokens
                            → monitored_team = the higher mid
                            → status=armed

every 5 min  scale-listener  read the derived window
                            → ECS desired count 1 while the window is open

window open  Fargate listener (Spot + On-Demand fallback)

live         listener        CLOB market WS  → trigger evaluation
                            sports WS        → live score (optional)
                            CLOB user WS     → late fill notifications

on trigger   trade-worker    SQS consumer
                            → pre-trade checks (no open orders, no position)
                            → GTD buy at the best ask, 5 shares
                            → poll the fill (~30 s)
                            → GTC recovery sell at the pre-game mid

window closed               listener stops itself
                            → ECS desired count 0
```

### The listener window

The Fargate listener only needs to run while there is a game it could
trade, so its on-window is **derived from the day's tip-off times** rather
than hard-coded into a cron:

- the window **opens** `LISTENER_LEAD_MINUTES` (40) before the first
  tip-off — the pre-game snapshot arms a game at T-30 and the listener's
  watch loop polls every 5 minutes, so the lead has to cover both;
- the window **closes** `LISTENER_TRAIL_MINUTES` (90) after the last
  tip-off, covering the halftime cutoff and any in-flight buy's recovery
  sell.

`fetch-daily` computes the window and stores it as `DATE#<day>/WINDOW`.
The `scale-listener` job then reads it every five minutes and sets the ECS
service's desired count to 1 while the window is open and 0 when it is not.
The daily fetch also creates two one-shot EventBridge schedules at the
window's start and end, so the listener starts on time even if the scale
job has not run for hours. The listener also stops itself when the window
closes, so a failed scale-down cannot leave a task running all night.

A fixed cron cannot express this: an early tip-off, a double header, a
make-up game or a rescheduled tip-off all move the window. The cron
schedules that remain in the template are only a backstop.

### Why the fetch runs at 16:00 UTC

Every date in this bot is an **ET** date, and AWS cron expressions are
always **UTC**. That combination is what makes the fetch time matter:

| UTC | ET | what it fetches |
|---|---|---|
| 00:30 | 19:30 the *previous* day | yesterday's slate — **wrong** |
| 16:00 | 11:00 today | today's slate — correct |

A fetch before 05:00 UTC (00:00 ET) lands on the previous ET day, so the
bot would stage yesterday's games while the day's own slate was never
fetched. The fetch therefore runs at **16:00 UTC = 11:00 ET**: after ET
midnight, and eight hours before the earliest tip-off (19:00 ET).

The fetch also covers the **day ahead** (`DAILY_FETCH_DAYS_AHEAD`, default
1). The schedule and the Polymarket moneyline markets for the next evening
are published well in advance, so fetching ahead stages the next slate —
games *and* its listener window — before it is needed. Each day keeps its
own pair of one-shot window schedules, so the days do not overwrite one
another.

The schedule source is queried **by date**. The API Ninjas schedule
endpoint returns only the current slate when called without a date filter,
so an unfiltered fetch for any other day comes back empty.

### The trigger rule

`src/trade/trigger.py` is a pure function. It fires when **all** of these
hold:

- the game is `armed` and has not been triggered yet;
- the clock is between tip-off and the cutoff (halftime, or
  `MONITOR_MAX_MINUTES` after tip-off);
- the monitored team's best bid is `<= pre_game_mid * PRICE_DROP_RATIO`
  (default: half the pre-game price);
- the price is inside `TRIGGER_MIN_PRICE` / `TRIGGER_MAX_PRICE`;
- optionally, the opponent's price has also risen
  (`REQUIRE_OPPONENT_RISE`).

No score is ever required — the sports feed is the least reliable in the
stack and never gates a trade.

### Auto-heal

The listener holds **no trading state**, so a disconnect is recoverable. On
every reconnect it re-reads DynamoDB, re-subscribes to the (possibly
changed) token set, and re-syncs every price from the CLOB REST `/book`
endpoint — the WebSocket only sends a full snapshot on subscribe, so a move
during the outage would otherwise be missed. A client-side idle watchdog
recycles a socket that goes quiet, because the Polymarket feeds send no
server-side heartbeat.

## Setup

Requires **Python 3.12+**.

```bash
cd polymarket-sports
python3.12 -m venv --copies .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
# Edit .env with API_NINJAS_KEY and the wallet credentials.
```

## Local development

The local stack runs [moto](https://github.com/getmoto/moto) — a pure
Python AWS emulator, no Docker required. Only AWS state is emulated; Gamma,
the CLOB API and the Polymarket WebSockets stay real, so a local run
exercises the same code paths as AWS.

```bash
bash scripts/local_stack.sh start   # start moto + create the table and queue
# it prints the export lines to paste into a second terminal:
eval "$(bash scripts/local_stack.sh env)"

.venv/bin/python -m src.main fetch-daily --date 2026-11-17
.venv/bin/python -m src.main snapshot-price --date 2026-11-17
.venv/bin/python -m src.main listen
.venv/bin/python -m src.main trade-worker
.venv/bin/python -m src.main dashboard-export

bash scripts/local_stack.sh stop
```

## Commands

| Command | What it does |
|---|---|
| `bootstrap-local` | Create the local DynamoDB table and SQS queue |
| `fetch-daily` | Fetch the schedule, match Polymarket, write DynamoDB |
| `snapshot-price` | Take the pre-game prices and arm the imminent games |
| `listen` | Run the live WebSocket listener |
| `trade-worker` | Consume the SQS trade queue |
| `scale-listener` | Scale the Fargate listener to match the day's window |
| `run-scheduler` | Run the fetch + snapshot + scale on timers (local dev) |
| `dashboard-export` | Dump the day's games and trades to a local JSON |

Every command takes `--date YYYY-MM-DD` where relevant. All dates are
**US Eastern** dates, because that is how the NBA schedule and the
Polymarket event slugs are keyed.

## Data model

One DynamoDB table, `SportsBotState`:

| Item | Key | Purpose |
|---|---|---|
| `GAME#<slug>` | `META` | The game, its teams, prices, status |
| `GAME#<slug>` | `TRADE#<ts>#<id>` | One trade |
| `GAME#<slug>` | `LOG#<ts>#<id>` | One log line |
| `DATE#<date>` | `BATCH` | The day's schedule and standings |
| `DATE#<date>` | `WINDOW` | The derived listener window for the day |

`GSI1` (`gsi1pk` / `gsi1sk`) indexes a whole ET day, so the dashboard API
answers with one query. Every item carries `expires_at` (TTL: game time +
3 days).

## Deployment

SAM template in `infrastructure/template.yaml`; one code base, two
container images (the Lambda base image for the functions, a plain image
for the Fargate task). `.github/workflows/deploy-lambda.yml` builds both,
pushes them to ECR and deploys the stack.

```bash
# repository variable: AWS_DEPLOY_ROLE_ARN  (required)
# repository variable: BOT_SECRET_ARN      (optional)
```

Deploy into **ap-east-1 (Hong Kong)**, the same region as
`polymarket-trader`, because Polymarket restricts some regions.

The dashboard is a static site on S3 behind CloudFront, with `/dashboard`
routed to the API Gateway so the browser never sees AWS credentials.

### Credentials

The bot reads its credentials from AWS Secrets Manager at startup, using
the `SECRETS_ARN` environment variable.

By default the stack creates and populates its own secret
(`polymarket-sports/bot`) from CloudFormation parameters, which means the
private key has to be passed through GitHub Actions secrets
(`CLOB_PRIVATE_KEY`, `DEPOSIT_WALLET_ADDRESS`, `API_NINJAS_KEY`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).

**Setting `BOT_SECRET_ARN` to an existing secret skips that entirely.**
The stack then reads that secret instead, does not create one of its own,
and no credentials need to be stored in GitHub at all. Point it at the
`polymarket-trader` secret to share the same wallet:

```
Settings -> Secrets and variables -> Actions -> Variables
  BOT_SECRET_ARN = arn:aws:secretsmanager:ap-east-1:ACCOUNT_ID:secret:NAME
```

The secret must contain the keys `PRIVATE_KEY`, `DEPOSIT_WALLET_ADDRESS`,
`API_NINJAS_KEY`, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Extra keys
are ignored. See `docs/aws-console-setup.md` for the one-time AWS setup.

## Cost

| Item | Cost |
|---|---|
| Fargate 0.25 vCPU / 0.5 GB, ~5 h x 140 days | $4 (Spot) / $10 (On-Demand) for the whole season |
| `snapshot-price` Lambda, 8,640 invocations/mo | $0.07 |
| `scale-listener` Lambda, 8,640 invocations/mo | $0.07 |
| `trade-worker` Lambda, ~1,500 invocations/mo | $0.04 |
| DynamoDB | < $0.01 |
| SQS + API Gateway + S3 + CloudWatch | < $0.10 |

**Keep `DRY_RUN=true` until paper trading has validated the edge.** Holding
a losing position to resolution is a 100% loss on that share, so the
strategy's expected value depends on how often the favourite recovers.

## Tests

```bash
.venv/bin/python -m pytest
```

Covers the trigger rules, the outcome → token mapping, the schedule
matching, the book tracker, the DynamoDB access layer, the buy/sell
worker, the pre-game snapshot, the exporter, the listener (including the
auto-heal path) and a dry-run end-to-end of one game.
