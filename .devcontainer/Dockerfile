FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

RUN apt-get update \
    && export DEBIAN_FRONTEND=noninteractive \
    && apt-get install -y --no-install-recommends \
        bluetooth \
        bluez \
        build-essential \
        curl \
        git \
        jq \
        libdbus-1-3 \
        libffi-dev \
        libglib2.0-0 \
        libssl-dev \
        pkg-config \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
