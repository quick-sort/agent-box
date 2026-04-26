FROM python:3.12-slim

ARG UID=1000
ARG GID=1000

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
    && npm install -g @anthropic-ai/claude-code@2.1.110 \
    && UV_INSTALL_DIR=/usr/local/bin curl -LsSf https://astral.sh/uv/install.sh | sh \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid $GID app \
    && useradd --uid $UID --gid $GID -m app \
    && curl -fsSL https://github.com/tianon/gosu/releases/download/1.18/gosu-amd64 -o /usr/local/bin/gosu \
    && chmod +x /usr/local/bin/gosu

ENV PATH="/home/app/.local/bin:$PATH"
ENV HOME="/home/app"

WORKDIR /app
COPY --chown=app:app pyproject.toml .
RUN uv sync --no-dev

COPY --chown=app:app src/ src/

# Persist weixin state and project data across restarts
VOLUME ["/home/app"]

COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uv", "run", "agent-box"]
