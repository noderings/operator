# syntax=docker/dockerfile:1

##############################################
# 1. Builder — install Python deps into a venv
##############################################
ARG PYTHON_VERSION=3.12
FROM --platform=$TARGETPLATFORM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# Build-time deps only (packages without pre-built wheels for the target arch).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv "${VIRTUAL_ENV}"

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install --upgrade pip setuptools wheel \
    && pip install -r requirements.txt


##############################################
# 2. Runtime — minimal image, non-root
##############################################
ARG PYTHON_VERSION=3.12
FROM --platform=$TARGETPLATFORM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ARG BUILD_DATE
ARG GIT_COMMIT
ARG VERSION=0.1.0

LABEL org.opencontainers.image.title="proxmox-operator" \
      org.opencontainers.image.description="Kopf-based Kubernetes operator for Proxmox VMs" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${GIT_COMMIT}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src

WORKDIR /app

# ca-certificates: HTTPS to Proxmox API and Kubernetes API.
# No build toolchain in the final image.
# Debian ships a system "operator" group (gid 37); use "app" for uid/gid 1000.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin app

COPY --from=builder --chown=1000:1000 /opt/venv /opt/venv
COPY --chown=1000:1000 src ./src

USER 1000

# Writable paths (/tmp) are provided by the Helm chart (readOnlyRootFilesystem).
# In-cluster run watches all namespaces by default (-A). Adjust via Helm values.
ENTRYPOINT ["kopf", "run", "-A", "-m", "proxmox_operator.handlers"]
