FROM python:3.12-slim


COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
# Install Node.js (required by Claude Code CLI) and uv
ENV FNM_DIR="/opt/fnm"
ENV PATH="/opt/fnm/aliases/default/bin:$PATH"
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git git-lfs wget unzip \
        libglib2.0-0 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libdrm2 libgbm1 libgtk-3-0 libxkbcommon0 libxshmfence1 \
        libx11-6 libx11-xcb1 libxcb1 libxcb-shm0 libxcomposite1 libxcursor1 \
        libxdamage1 libxext6 libxfixes3 libxi6 libxrandr2 \
        libxrender1 libxss1 libxtst6 \
        libnss3 libnspr4 libpango-1.0-0 libpangocairo-1.0-0 libcairo2 libcairo-gobject2 \
        libdbus-1-3 libfontconfig1 libfreetype6 libgdk-pixbuf-2.0-0 libatspi2.0-0 \
        fonts-liberation fonts-wqy-zenhei fonts-noto-color-emoji fonts-noto-cjk fonts-freefont-ttf libasound2 \
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
    && git lfs install --system \
    && mkdir -p /home/agent \
    && echo "registry=https://registry.npmmirror.com" > /home/agent/.npmrc \
    && npm config set prefix '/home/agent/.npm-global' \
    && npm install -g @anthropic-ai/claude-code@2.1.110 agent-browser \
    && /home/agent/.npm-global/bin/agent-browser install \
    && chmod -R 755 /root/.agent-browser \
    && npm cache clean --force \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && useradd --no-create-home -d /home/agent -s /bin/bash agent

# Kiro CLI ACP provider. Pin both the release and checksums so image builds
# remain reproducible; arm64 uses the musl build because Debian's glibc is
# below Kiro's current aarch64-gnu minimum.
ARG TARGETARCH
ARG KIRO_CLI_VERSION=2.21.1
RUN case "$TARGETARCH" in \
        amd64) archive="kirocli-x86_64-linux.zip"; checksum="1f81a69b2a5d49fc74793d8805e6c31650b78e957ebcf54e90877a8e72f4b0a1" ;; \
        arm64) archive="kirocli-aarch64-linux-musl.zip"; checksum="7f5df29bd3a1097a387a4e0c88e035e5a26d56aa59cea8c5c78123608b4549c0" ;; \
        *) echo "Unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && curl -fsSL "https://prod.download.cli.kiro.dev/stable/${KIRO_CLI_VERSION}/${archive}" -o /tmp/kiro-cli.zip \
    && echo "${checksum}  /tmp/kiro-cli.zip" | sha256sum -c - \
    && mkdir -p /tmp/kiro-cli \
    && unzip -q /tmp/kiro-cli.zip -d /tmp/kiro-cli \
    && chmod +x /tmp/kiro-cli/kirocli/install.sh \
    && HOME=/home/agent KIRO_CLI_SKIP_SETUP=1 /tmp/kiro-cli/kirocli/install.sh \
    && install -m 0755 /home/agent/.local/bin/kiro-cli /usr/local/bin/kiro-cli \
    && install -m 0755 /home/agent/.local/bin/kiro-cli-chat /usr/local/bin/kiro-cli-chat \
    && rm -f /home/agent/.local/bin/kiro-cli /home/agent/.local/bin/kiro-cli-chat \
    && test -x /usr/local/bin/kiro-cli \
    && rm -rf /tmp/kiro-cli /tmp/kiro-cli.zip


# Persist channel state, agent credentials, and project data across restarts
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

RUN chown -R agent /app /home/agent
COPY --chmod=755 entrypoint.sh /entrypoint.sh

ENV UV_CACHE_DIR="/home/agent/.cache/uv"
ENV PATH="/home/agent/.local/bin:/home/agent/.npm-global/bin:$PATH"
USER agent
RUN npx skills add https://github.com/vercel-labs/skills --skill find-skills -y -g -a claude-code \
    && npx skills add vercel-labs/agent-browser -y -g -a claude-code
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uv", "run", "agent-box"]
