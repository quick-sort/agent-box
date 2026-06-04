FROM python:3.12-slim


COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
# Install Node.js (required by Claude Code CLI) and uv
ENV FNM_DIR="/opt/fnm"
ENV PATH="/opt/fnm/aliases/default/bin:$PATH"
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git wget unzip \
        libglib2.0-0 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libdrm2 libgbm1 libgtk-3-0 libxkbcommon0 libxshmfence1 \
        libx11-6 libx11-xcb1 libxcb1 libxcomposite1 libxcursor1 \
        libxdamage1 libxext6 libxfixes3 libxi6 libxrandr2 \
        libxrender1 libxss1 libxtst6 \
        libnss3 libnspr4 libpango-1.0-0 libcairo2 \
        libdbus-1-3 libfontconfig1 fonts-liberation libasound2 \
    && curl -fsSL https://fnm.vercel.app/install | bash -s -- --install-dir /usr/local/bin --skip-shell \
    && fnm install 24 \
    && fnm default 24 \
    && chmod -R 755 /opt/fnm/aliases/default/bin \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli-list \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
        -o /usr/share/keyrings/docker-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/debian trixie stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh openssh-client docker-ce-cli \
    && groupadd agent \
    && useradd --uid 1000 -g agent -m agent \
    && echo "registry=https://registry.npmmirror.com" > /home/agent/.npmrc \
    && npm config set prefix '/home/agent/.npm-global' \
    && npm install -g @anthropic-ai/claude-code@2.1.110 agent-browser \
    && /home/agent/.npm-global/bin/agent-browser install --with-deps \
    && chmod -R 755 /root/.agent-browser \
    && PATH="/home/agent/.npm-global/bin:$PATH" npx skills add https://github.com/vercel-labs/skills --skill find-skills \
    && PATH="/home/agent/.npm-global/bin:$PATH" npx skills add vercel-labs/agent-browser \
    && npm cache clean --force \
    && apt-get clean && rm -rf /var/lib/apt/lists/*


# Persist weixin state and project data across restarts
VOLUME ["/home/agent"]

WORKDIR /app
ENV UV_LINK_MODE=copy
ENV HOME="/home/agent"


RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

COPY pyproject.toml uv.lock /app/
COPY src /app/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

RUN chown -R agent:agent /app /home/agent 
COPY --chmod=755 entrypoint.sh /entrypoint.sh
USER agent

ENV UV_CACHE_DIR="/home/agent/.cache/uv"
ENV PATH="/home/agent/.npm-global/bin:$PATH"
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uv", "run", "agent-box"]
