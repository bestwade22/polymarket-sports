# syntax=docker/dockerfile:1
#
# Container image for the NBA Polymarket trading bot.
#
# The same code backs every Lambda function and the Fargate listener, so
# there is exactly one build to maintain. The Lambda handlers live in
# lambda_handlers/, the Fargate task runs `python -m src.main listen`.
#
# Two targets, because the two runtimes need different base images:
#
#   --target lambda  -> the AWS Lambda base image (used by the five
#                        Lambda functions). Its entrypoint is the Lambda
#                        runtime, so the code must live in /var/task and
#                        the handler is chosen by the function's
#                        ImageConfig.
#   --target fargate -> plain python:3.12-slim (used by the ECS task).
#
# Build:
#   docker build --platform linux/arm64 --target lambda  -t sportsbot:lambda  .
#   docker build --platform linux/arm64 --target fargate -t sportsbot:fargate .

# --- Lambda target ----------------------------------------------------------

FROM public.ecr.aws/lambda/python:3.12-arm64 AS lambda

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    AWS_REGION=ap-east-1

# The Lambda runtime mounts the function code at /var/task.
WORKDIR /var/task

COPY requirements.txt ./
RUN python3 -m pip install --no-cache-dir --upgrade pip \
    && python3 -m pip install --no-cache-dir -r requirements.txt

COPY config/ ./config/
COPY src/ ./src/
COPY lambda_handlers/ ./lambda_handlers/
COPY web/ ./web/
COPY scripts/ ./scripts/

# The runtime user is not root; it must be able to read the code.
RUN chown -R 65534:65534 /var/task
USER 65534

# Overridden per function via ImageConfig.Command:
#   lambda_handlers.fetch_daily.handler
#   lambda_handlers.snapshot_price.handler
#   lambda_handlers.scale_listener.handler
#   lambda_handlers.trade_worker.handler
#   lambda_handlers.dashboard_api.handler
CMD ["lambda_handlers.fetch_daily.handler"]

# --- Fargate target ---------------------------------------------------------

FROM python:3.12-slim AS fargate

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    AWS_REGION=ap-east-1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY config/ ./config/
COPY src/ ./src/
COPY lambda_handlers/ ./lambda_handlers/
COPY web/ ./web/
COPY scripts/ ./scripts/

# Run as a non-root user.
RUN useradd --create-home --uid 10001 botuser \
    && chown -R botuser:botuser /app
USER botuser

CMD ["python", "-m", "src.main", "listen"]
