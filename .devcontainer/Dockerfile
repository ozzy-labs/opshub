# OpsHub devcontainer.
#
# Primary runtime: Python 3.13 + uv (ADR-0001).
# Node.js is retained for npm-distributed dev tools (markdownlint-cli2, bats,
# @commitlint/*) and AI agent CLIs (Codex, Gemini). Versions are pinned in
# .mise.toml so most tool installation is driven by `mise install` below.
FROM mcr.microsoft.com/devcontainers/base:ubuntu-24.04

# build-essential / libssl-dev / libffi-dev cover wheel-build needs for
# common Python native deps (cryptography, pydantic-core fallback path, etc.).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        git \
        fd-find \
        ffmpeg \
        fzf \
        jq \
        libffi-dev \
        libssl-dev \
        ripgrep \
        tesseract-ocr \
        tesseract-ocr-jpn \
        tree \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Install as vscode user
USER vscode

# Install Claude Code
RUN curl -fsSL https://claude.ai/install.sh | bash

# Install mise
RUN curl --proto '=https' --tlsv1.2 -fsSL https://mise.run | sh

# Install oh-my-zsh plugins (pinned to known commits)
RUN git clone --depth=1 https://github.com/zsh-users/zsh-autosuggestions \
        ${ZSH_CUSTOM:-~/.oh-my-zsh/custom}/plugins/zsh-autosuggestions \
    && git -C ${ZSH_CUSTOM:-~/.oh-my-zsh/custom}/plugins/zsh-autosuggestions \
        fetch --depth=1 origin 85919cd1ffa7d2d5412f6d3fe437ebdbeeec4fc5 \
    && git -C ${ZSH_CUSTOM:-~/.oh-my-zsh/custom}/plugins/zsh-autosuggestions \
        checkout 85919cd1ffa7d2d5412f6d3fe437ebdbeeec4fc5 \
    && git clone --depth=1 https://github.com/zsh-users/zsh-syntax-highlighting \
        ${ZSH_CUSTOM:-~/.oh-my-zsh/custom}/plugins/zsh-syntax-highlighting \
    && git -C ${ZSH_CUSTOM:-~/.oh-my-zsh/custom}/plugins/zsh-syntax-highlighting \
        fetch --depth=1 origin 1d85c692615a25fe2293bdd44b34c217d5d2bf04 \
    && git -C ${ZSH_CUSTOM:-~/.oh-my-zsh/custom}/plugins/zsh-syntax-highlighting \
        checkout 1d85c692615a25fe2293bdd44b34c217d5d2bf04 \
    && sed -i 's/^plugins=(git)$/plugins=(git zsh-autosuggestions zsh-syntax-highlighting)/' ~/.zshrc

# Install tools defined in .mise.toml (cached in image layer)
# GITHUB_TOKEN avoids GitHub API rate limits for aqua backend downloads
ARG GITHUB_TOKEN
COPY --chown=vscode:vscode .mise.toml /home/vscode/.mise.toml
RUN ~/.local/bin/mise trust /home/vscode/.mise.toml \
    && ~/.local/bin/mise install \
    && rm /home/vscode/.mise.toml

# Install AI agent CLIs (npm available after mise installs Node.js)
RUN eval "$(/home/vscode/.local/bin/mise activate bash)" \
    && npm install -g @openai/codex @google/gemini-cli

# Install markitdown (PDF/Office/image/audio → Markdown via uv tool)
RUN eval "$(/home/vscode/.local/bin/mise activate bash)" \
    && uv tool install "markitdown[all]"

# Switch back to root for remaining setup
USER root
RUN chsh -s /bin/zsh vscode \
    && echo 'eval "$(/home/vscode/.local/bin/mise activate bash)"' >> /etc/bash.bashrc \
    && echo 'eval "$(/home/vscode/.local/bin/mise activate zsh)"' >> /etc/zsh/zshrc \
    && echo 'export HISTFILE="$HOME/.zsh_history"' >> /etc/zsh/zshrc \
    && echo 'export HISTSIZE=10000' >> /etc/zsh/zshrc \
    && echo 'export SAVEHIST=10000' >> /etc/zsh/zshrc \
    && echo '# Write history in-place; the default copy-and-rename fails on bind-mounted files' >> /etc/zsh/zshrc \
    && echo 'unsetopt HIST_SAVE_BY_COPY' >> /etc/zsh/zshrc
