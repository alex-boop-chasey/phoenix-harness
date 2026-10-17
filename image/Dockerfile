FROM node:24-bookworm

ARG TZ=Australia/Brisbane
ENV TZ=$TZ
ARG CLAUDE_CODE_VERSION
ARG PLAYWRIGHT_VERSION

RUN apt-get update && apt-get install -y --no-install-recommends \
  less git procps sudo unzip gnupg2 curl ca-certificates \
  iptables ipset iproute2 dnsutils aggregate jq \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN corepack enable

ENV NPM_CONFIG_PREFIX=/usr/local/share/npm-global \
    PATH=$PATH:/usr/local/share/npm-global/bin \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DEVCONTAINER=true

RUN mkdir -p /usr/local/share/npm-global /workspace /home/node/.claude /ms-playwright && \
  chown -R node:node /usr/local/share/npm-global /workspace /home/node/.claude /ms-playwright

# Browser system libraries (root)
RUN test -n "$PLAYWRIGHT_VERSION" && npx -y playwright@${PLAYWRIGHT_VERSION} install-deps chromium

USER node
RUN test -n "$CLAUDE_CODE_VERSION" && \
  npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} && \
  npx -y playwright@${PLAYWRIGHT_VERSION} install chromium

USER root
# DNS allowlist resolver (init-firewall.sh runs it as root; the agent uid gets /etc/resolv.conf -> 127.0.0.1)
RUN apt-get update && apt-get install -y --no-install-recommends dnsmasq-base \
  && apt-get clean && rm -rf /var/lib/apt/lists/*
COPY init-firewall.sh hands-dns-conf.sh /usr/local/bin/
RUN chmod 755 /usr/local/bin/init-firewall.sh /usr/local/bin/hands-dns-conf.sh && \
  echo "node ALL=(root) NOPASSWD: /usr/local/bin/init-firewall.sh" > /etc/sudoers.d/node-firewall && \
  chmod 0440 /etc/sudoers.d/node-firewall
USER node
WORKDIR /workspace
