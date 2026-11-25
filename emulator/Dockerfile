
FROM python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b

ENV PIP_NO_CACHE_DIR=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

COPY emulator/requirements-build.lock /tmp/requirements-build.lock
COPY emulator/requirements.lock /tmp/requirements.lock
RUN pip install --require-hashes --no-deps --only-binary=:all: -r /tmp/requirements-build.lock && \
    pip install --require-hashes --no-deps --no-build-isolation --only-binary=:all: --no-binary=esptool -r /tmp/requirements.lock && pip check

WORKDIR /workspace

ENTRYPOINT ["bash", "-o", "pipefail", "-c", "sed 's/\\r$//' emulator/build_firmware.sh | bash -s"]
