# Agent Box - Integrated AI Development Environment
FROM node:22-slim

WORKDIR /app

# ============================================================
# 1. System dependencies + apt mirror
# ============================================================
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       bash ca-certificates chromium curl gnupg fonts-liberation fonts-noto-cjk \
       fonts-noto-color-emoji git gosu jq python3 python3-pip python3-venv \
       socat tini unzip websockify libicu-dev \
    && rm -rf /var/lib/apt/lists/*

# ============================================================
# 2. GitHub CLI (gh)
# ============================================================
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh && rm -rf /var/lib/apt/lists/*

# ============================================================
# 3. Docker CLI
# ============================================================
RUN curl -fsSL https://download.docker.com/linux/debian/gpg \
      | gpg --dearmor -o /usr/share/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker.gpg] https://mirrors.tuna.tsinghua.edu.cn/docker-ce/linux/debian bookworm stable" \
      > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y docker-ce-cli && rm -rf /var/lib/apt/lists/*

# ============================================================
# 4. pnpm
# ============================================================
RUN corepack enable && corepack prepare pnpm@latest --activate

# ============================================================
# 5. uv (Python package manager)
# ============================================================
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local sh
RUN mkdir -p /etc/xdg/uv \
    && printf '[pip]\nindex-url = "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"\n' > /etc/xdg/uv/uv.toml

# ============================================================
# 6. Rust (rustup) with Tsinghua mirror
# ============================================================
ENV RUSTUP_DIST_SERVER=https://mirrors.tuna.tsinghua.edu.cn/rustup
ENV RUSTUP_UPDATE_ROOT=https://mirrors.tuna.tsinghua.edu.cn/rustup/rustup
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable
ENV PATH="/root/.cargo/bin:$PATH"

# ============================================================
# 7. Claude Code CLI
# ============================================================
RUN npm install -g @anthropic-ai/claude-code

# ============================================================
# 8. OpenClaw + plugins
# ============================================================
RUN npm install -g openclaw@latest

# Playwright (Node.js + Python) with Chromium
RUN npm install -g playwright && npx playwright install chromium --with-deps
RUN uv pip install --system playwright

# Create dirs
RUN mkdir -p /home/node/.openclaw/workspace /home/node/.claude \
    && chown -R node:node /home/node

USER node

# QQBot plugin
RUN cd /tmp \
    && git clone https://github.com/tencent-connect/openclaw-qqbot.git qqbot \
    && cd qqbot && timeout 300 openclaw plugins install . || true

# Feishu plugin
RUN timeout 300 openclaw plugins install @nicepkg/openclaw-plugin-feishu || true

USER root

# Fix extension permissions
RUN if [ -d /home/node/.openclaw/extensions ]; then \
      chown -R node:node /home/node/.openclaw/extensions; \
    fi

# ============================================================
# 9. GitHub Actions Runner
# ============================================================
ARG RUNNER_VERSION=2.322.0
ARG RUNNER_ARCH=linux-x64
RUN mkdir -p /opt/runner \
    && cd /opt/runner \
    && curl -fsSL -o runner.tar.gz \
       "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-${RUNNER_ARCH}-${RUNNER_VERSION}.tar.gz" \
    && tar xzf runner.tar.gz && rm runner.tar.gz \
    && ./bin/installdependencies.sh || true \
    && chown -R node:node /opt/runner

# ============================================================
# 10. Copy scripts
# ============================================================
COPY scripts/init.py /usr/local/bin/init.py
COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# ============================================================
# 11. Environment
# ============================================================
ENV HOME=/home/node \
    TERM=xterm-256color \
    NODE_PATH=/usr/local/lib/node_modules \
    UV_INDEX_URL=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple \
    PATH="/home/node/.cargo/bin:$PATH"

# Move rust toolchain to node user
RUN if [ -d /root/.rustup ]; then \
      mv /root/.rustup /home/node/.rustup && mv /root/.cargo /home/node/.cargo \
      && chown -R node:node /home/node/.rustup /home/node/.cargo; \
    fi
ENV RUSTUP_HOME=/home/node/.rustup
ENV CARGO_HOME=/home/node/.cargo

EXPOSE 18789 18790

WORKDIR /home/node

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
