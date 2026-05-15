FROM node:22-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV NPM_CONFIG_UPDATE_NOTIFIER=false
ENV NPM_CONFIG_FUND=false
ENV NPM_CONFIG_AUDIT=false

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        python3 \
        ripgrep \
        tmux \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code @openai/codex

RUN mkdir -p /workspace/project /workspace/home /workspace/npm-cache \
    && chmod 700 /workspace/home

ENV HOME=/workspace/home
ENV NPM_CONFIG_CACHE=/workspace/npm-cache
WORKDIR /workspace/project

CMD ["sleep", "infinity"]
