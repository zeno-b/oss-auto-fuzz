# syntax=docker/dockerfile:1
ARG BASE_IMAGE=gcr.io/oss-fuzz-base/base-builder

FROM ${BASE_IMAGE} AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Etc/UTC \
    PATH="/workspace/oss-fuzz/.venv/bin:/root/.local/bin:${PATH}"

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git \
        python3 \
        python3-pip \
        python3-venv \
        python3-dev \
        build-essential \
        ninja-build \
        cmake \
        meson \
        pkg-config \
        jq \
        parallel \
        unzip \
        zip \
        rsync \
        wget \
        curl \
        file \
        binwalk \
        radare2 \
        gdb \
        ltrace \
        strace \
        netcat-openbsd \
        socat \
        libssl-dev \
        libunwind-dev \
        liblzma-dev \
        libbz2-dev \
        libzstd-dev \
        libxml2-dev \
        libmagic-dev \
        libglib2.0-dev \
        libpixman-1-dev \
        zstd \
        ccache && \
    rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --upgrade pip setuptools wheel && \
    python3 -m pip install --no-cache-dir \
        click \
        pyyaml \
        rich \
        docker \
        python-dotenv \
        psutil

WORKDIR /workspace

FROM builder AS runner
COPY config /workspace/config
COPY scripts /workspace/scripts
COPY docker/healthcheck.sh /workspace/healthcheck.sh

RUN chmod +x /workspace/healthcheck.sh

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD /workspace/healthcheck.sh

ENTRYPOINT ["/bin/bash","-lc"]
CMD ["python3 /workspace/scripts/fuzz_orchestrator.py --help"]
