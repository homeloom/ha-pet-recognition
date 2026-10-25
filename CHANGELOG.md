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
