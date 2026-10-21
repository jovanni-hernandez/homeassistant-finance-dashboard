FROM mcr.microsoft.com/devcontainers/python:3.11-bookworm

USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    git \
    nodejs \
    npm \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY run.sh /app/run.sh
RUN chmod a+x /app/run.sh

CMD ["/app/run.sh"]
