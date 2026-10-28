# The base image, pinned by the digest of its multi-architecture index, so
# that amd64 and aarch64 builds get the same image. The Supervisor passes the
# tag of build.yaml as BUILD_FROM, which the pin replaces.
ARG BUILD_FROM
FROM python:3.13.17-slim-trixie@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962
ARG BUILD_VERSION=0.0.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --system --no-create-home --shell /usr/sbin/nologin engine

WORKDIR /app

# Every dependency, transitive ones included, pinned by hash.
COPY requirements.txt ./
RUN pip install --require-hashes --no-deps --requirement requirements.txt

# The models, downloaded once from their pinned URLs and checked by size and
# SHA-256. Nothing is downloaded once the app runs.
COPY models.json ./
COPY pet_engine/__init__.py pet_engine/models.py pet_engine/sessions.py ./pet_engine/
RUN python -m pet_engine.models models.json /models

COPY pet_engine ./pet_engine
COPY selftest ./selftest
# Stops the build unless the models find the cat of the sample picture, with
# an embedding close to the one recorded for the pinned model.
RUN python -m pet_engine.selftest models.json /models selftest/cat.jpg selftest/cat.json

ENV ENGINE_VERSION=${BUILD_VERSION}
EXPOSE 8765
# With its watchdog on, the Supervisor restarts the app once Docker marks it
# unhealthy. Loading the models takes a few seconds, hence the start period.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-m", "pet_engine.healthcheck"]
CMD ["python", "-m", "pet_engine"]
