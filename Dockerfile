FROM python:3.12-slim


COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
# Install Node.js (required by Claude Code CLI) and uv
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git wget unzip \
    && curl -o- https://fnm.vercel.app/install | bash \
    && . /root/.bashrc \
    && fnm install 24 \
    && corepack enable pnpm \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 999 agent \
    && useradd --uid 1000 --gid 999 -m agent \
    && echo "registry=https://registry.npmmirror.com" > /home/agent/.npmrc \
    && npm config set prefix '/home/agent/.npm-global' \
    && npm install -g @anthropic-ai/claude-code@2.1.110


# Persist weixin state and project data across restarts
VOLUME ["/home/agent"]

WORKDIR /app
ENV UV_LINK_MODE=copy
ENV HOME="/home/agent"


COPY pyproject.toml uv.lock /app/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project


COPY src /app/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked

RUN chown -R agent:agent /app /home/agent 
COPY --chmod=755 entrypoint.sh /entrypoint.sh
USER agent

ENV UV_CACHE_DIR="/home/agent/.cache/uv"
ENV PATH="/home/agent/.npm-global/bin:$PATH"
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uv", "run", "agent-box"]
