# The Supervisor passes the base image of build.yaml and the app's version.
# The default is the same pinned image, for a build with Docker alone.
ARG BUILD_FROM=python:3.13.17-slim-trixie@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962
FROM ${BUILD_FROM}
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
CMD ["python", "-m", "pet_engine"]
