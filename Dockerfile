# OpenClaw + Claude Code Docker Image
# 预装: uv, qqbot, weixin channel, 清华镜像源
FROM node:22-slim

# 设置工作目录
WORKDIR /app

# 配置 Debian 清华镜像源
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       bash \
       ca-certificates \
       chromium \
       curl \
       fonts-liberation \
       fonts-noto-cjk \
       fonts-noto-color-emoji \
       git \
       gosu \
       jq \
       python3 \
       python3-pip \
       python3-venv \
       socat \
       tini \
       unzip \
       websockify \
    && rm -rf /var/lib/apt/lists/*

# 安装 GitHub CLI (gh)
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# 配置 npm 清华镜像源
RUN npm config set registry https://mirrors.tuna.tsinghua.edu.cn/npm/

# 更新 npm
RUN npm install -g npm@latest

# 安装 bun
RUN curl -fsSL https://bun.sh/install | BUN_INSTALL=/usr/local bash
ENV BUN_INSTALL="/usr/local"
ENV PATH="$BUN_INSTALL/bin:$PATH"

# 安装 uv (Python 包管理器) - 使用清华镜像
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local sh \
    && uv pip install --system --index-url https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple pip --upgrade

# 配置 pip 清华镜像源
RUN mkdir -p /etc/xdg/uv \
    && echo '[global]\nindex-url = https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple' > /etc/xdg/uv/uv.toml \
    && mkdir -p /root/.config/pip \
    && echo '[global]\nindex-url = https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple' > /root/.config/pip/pip.conf

# 安装 qmd
RUN bun install -g https://github.com/tobi/qmd

# 安装 OpenClaw
RUN npm install -g openclaw@latest

# 安装 Playwright 和 Chromium
RUN npm install -g playwright && npx playwright install chromium --with-deps

# 安装 playwright-extra 和 stealth 插件
RUN npm install -g playwright-extra puppeteer-extra-plugin-stealth

# 安装 bird
RUN npm install -g @steipete/bird

# 创建配置目录并设置权限
RUN mkdir -p /home/node/.openclaw/workspace \
    && chown -R node:node /home/node

# 切换到 node 用户安装插件
USER node

# 安装 QQ 机器人插件
RUN cd /tmp \
    && git clone https://github.com/tencent-connect/openclaw-qqbot.git qqbot \
    && cd qqbot \
    && timeout 300 openclaw plugins install . || true

# 安装微信插件 (官方微信 ClawBot)
RUN timeout 300 openclaw plugins install @tencent-weixin/openclaw-weixin || true

# 切换回 root
USER root

# 确保 extensions 目录权限正确
RUN if [ -d /home/node/.openclaw/extensions ]; then \
      find /home/node/.openclaw/extensions -type d -name node_modules -prune -o -exec chown node:node {} +; \
    fi

# 配置 UV 镜像给 node 用户
RUN mkdir -p /home/node/.config/pip \
    && echo '[global]\nindex-url = https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple' > /home/node/.config/pip/pip.conf \
    && chown -R node:node /home/node/.config

# 复制初始化脚本
COPY ./init.sh /usr/local/bin/init.sh
RUN chmod +x /usr/local/bin/init.sh

# 设置基础环境变量
ENV HOME=/home/node \
    TERM=xterm-256color \
    NODE_PATH=/usr/local/lib/node_modules \
    UV_INDEX_URL=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple

# 暴露端口
EXPOSE 18789 18790

# 设置工作目录
WORKDIR /home/node

# 使用初始化脚本作为入口点
ENTRYPOINT ["/bin/bash", "/usr/local/bin/init.sh"]
