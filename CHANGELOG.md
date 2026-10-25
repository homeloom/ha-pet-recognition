# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Matching of animal embeddings to the examples of each pet, as plain Python
  with no Home Assistant dependency.
- Visits and runs: pictures of a source taken close together form a visit,
  unsure pictures inherit from their neighbours, and each pet counts once
  per visit on the day the visit started.
- Questions: when to ask who was at a source, once per unknown run, one
  learning question at a time while a pet has no example, and when a
  question is settled or expires.
- Corrections: relabel a run, or answer a question without overriding the
  pictures recognised since, and pick the animal that becomes an example.
- Engine: open pictures safely (JPEG, PNG and WebP, at most 40 megapixels,
  EXIF orientation), find birds, cats and dogs with YOLOX, crop each animal
  and embed it with DINOv2.
- Engine API, version 1: `/health`, `/v1/info`, `/v1/analyze` and
  `/v1/embed`, with a bearer key, size limits, one picture at a time and JSON
  errors. The contract is in `docs/api.md` and `docs/api-v1.schema.json`.
- Pet Recognition Engine app for the Supervisor: models downloaded from
  pinned URLs and checked by SHA-256 when the image is built, a self-test on
  a sample picture checked against a reference embedding, its own API key,
  and an announcement through the Supervisor's discovery, which the
  integration's setup will use.
- Connection to the engine, set up in the UI: discovered from the engine
  app, or entered by hand, with reconfiguration and a new key when the
  engine refuses the old one. Every answer of the engine is checked before
  it is used.
- Engine health: an **Engine** connectivity sensor updated every minute,
  repairs when the engine stops answering for a quarter of an hour, no
  longer serves the integration's API version or is of another release, and
  diagnostics without the engine's address or key. The entry now loads
  while the engine is stopped.
