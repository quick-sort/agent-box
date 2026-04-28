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
    && pip install uv \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid $GID app \
    && useradd --uid $UID --gid $GID -m app \
    && mkdir -p /home/app/.npm-global /home/app/.cache/uv \
    && echo "registry=https://registry.npmmirror.com" > /home/app/.npmrc \
    && npm install -g @anthropic-ai/claude-code@2.1.110 --prefix /home/app/.npm-global \
    && chown -R app:app /home/app \
    && curl -fsSL https://github.com/tianon/gosu/releases/download/1.18/gosu-amd64 -o /usr/local/bin/gosu \
    && chmod +x /usr/local/bin/gosu

ENV HOME="/home/app"
ENV UV_CACHE_DIR="/home/app/.cache/uv"
ENV NPM_CONFIG_PREFIX="/home/app/.npm-global"
ENV PATH="/home/app/.npm-global/bin:${PATH}"

# Persist weixin state and project data across restarts
VOLUME ["/home/app"]

WORKDIR /app
COPY --chown=app:app pyproject.toml uv.lock ./
RUN chown -R app:app /app && gosu app uv sync --no-dev

COPY --chown=app:app src/ src/

COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uv", "run", "agent-box"]
