FROM node:22-bookworm-slim AS node

FROM python:3.13-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV NPM_CONFIG_UPDATE_NOTIFIER=false
ENV NPM_CONFIG_FUND=false
ENV NPM_CONFIG_AUDIT=false

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/bin/npm /usr/local/bin/npm
COPY --from=node /usr/local/bin/npx /usr/local/bin/npx
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        ripgrep \
        tmux \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code @openai/codex

WORKDIR /opt/yikes
COPY pyproject.toml README.md ./
COPY yikes ./yikes
RUN python -m pip install --no-cache-dir .

RUN mkdir -p /workspace/project /workspace/home /workspace/npm-cache \
    && chmod 700 /workspace/home

ENV HOME=/workspace/home
ENV NPM_CONFIG_CACHE=/workspace/npm-cache
WORKDIR /workspace/project

CMD ["yikes", "server", "--host", "0.0.0.0", "--port", "8989", "--token-store", "/workspace/home/.yikes/tokens.json", "--event-store", "/workspace/home/.yikes/events", "--bootstrap-token-env", "YIKES_SERVER_TOKEN"]
