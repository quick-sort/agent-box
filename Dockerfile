FROM python:3.12-slim

ARG UID=1000
ARG GID=1000

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
# Install Node.js (required by Claude Code CLI) and uv
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y nodejs gh \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid $GID agent \
    && useradd --uid $UID --gid $GID -m agent


# Persist weixin state and project data across restarts
VOLUME ["/home/agent"]

WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project

COPY pyproject.toml uv.lock src /app

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev \
    && chown -R agent:agent /app

ENV UV_CACHE_DIR="/home/agent/.cache/uv"
ENV HOME="/home/agent"

USER agent
COPY entrypoint.sh /entrypoint.sh
RUN echo "registry=https://registry.npmmirror.com" > /home/agent/.npmrc \
    && npm config set prefix '~/.npm-global' \
    && npm install -g @anthropic-ai/claude-code@2.1.110
ENV PATH="~/.npm-global/bin:$PATH"
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uv", "run", "--frozen", "agent-box"]
