FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CHIPLOOP_PRIVATE_ARTIFACT_ROOT=/var/lib/chiploop/artifacts \
    RUSTUP_HOME=/opt/rustup \
    CARGO_HOME=/opt/cargo \
    PATH=/opt/cargo/bin:${PATH}

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    build-essential \
    ca-certificates \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Firmware workflows compile for the selected processor rather than the image
# host. Preinstall the governed common targets; rustup remains available for an
# explicitly selected standard target that is not in this baseline matrix.
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
      | sh -s -- -y --profile minimal --default-toolchain stable \
    && rustup target add \
      riscv32i-unknown-none-elf \
      riscv32im-unknown-none-elf \
      riscv32imc-unknown-none-elf \
      riscv64gc-unknown-none-elf \
      thumbv6m-none-eabi \
      thumbv7m-none-eabi \
      thumbv7em-none-eabi \
      thumbv7em-none-eabihf

WORKDIR /app/backend
COPY requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/backend
RUN useradd --create-home --uid 10001 chiploop \
    && mkdir -p /app/backend/backend/workflows /var/lib/chiploop/artifacts /var/lib/chiploop/tmp \
    && chown -R chiploop:chiploop /app/backend /var/lib/chiploop /opt/rustup /opt/cargo

USER chiploop
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
