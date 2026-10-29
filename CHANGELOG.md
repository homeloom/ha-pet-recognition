# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-10-29

Pets are asked about on the phones when unsure, and learn from the answers
and corrections.

### Added

- Options of the entry: the score and margin that decide a pet, the
  smallest animal and the most animals in a picture of a source, and how
  many days pictures are kept (7 by default, from 2 to 30). They are in the
  diagnostics.
- Questions on the phones: once a visit ended, each part of it that stays
  unknown is asked on the companion app phones chosen in the options, with
  the crop of the animal and buttons for the two likeliest pets and Other.
  One learning question at a time while a pet has no examples (**Ask while
  learning**), and questions when unsure per source (**Ask when unsure**).
  Questions are saved before they are sent, tried again on a phone that
  fails, removed from the phones once they expire or their source is
  removed, and counted in the diagnostics.
- A repair when a phone of the options is no longer a companion app device.
- Answers to the questions: a pet corrects the pictures of the question,
  which updates the visit and its counts, and removes the question from
  every phone; Other closes it and leaves the visit to review. Only an
  open question counts, once, from the user of a phone asked. Corrections
  are kept in the journal.
- Learning from the answers: a pet named for a single animal makes it a
  learned example of that pet, from the embedding the journal kept or from
  its crop embedded again. Correcting the same picture again takes the
  example back. Learned examples are counted in the diagnostics.
- Unknown pictures are told again each time the examples change, and when
  the entry loads, so that an answer can tell who was at other visits and
  settle their questions. The comparisons run outside the event loop.
- **Correct last visit**, a select of each source that corrects its latest
  run to a pet, the unknown or the other animal.
- **Visits to review**, a sensor of each source that counts the visits
  that ended unsure.
- The **Assign visit** action, for administrators, which corrects a whole
  visit, given by its ID or by an entity of its source, and can answer
  with the visit.
- The **Visit** event announces `visit_corrected` when a correction changes
  a visit that already ended.

## [0.1.0] - 2026-10-28

The first release: pets are recognized at their sources, and their visits
counted.

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
- Pets, added in the UI with a name, a species and photos, uploaded or
  taken from a camera or image entity. Each photo with exactly one animal
  of the pet's species becomes an example; its embedding is stored, and its
  crop kept under the configuration directory. A pet's settings add photos,
  remove examples shown with their pictures, and rename it. A repair names
  each pet without examples, and examples are embedded again when the
  engine's model changes, the photos given by the user first; those are
  never dropped. Each pet has a device and an examples sensor.
- Sources, added in the UI with a name, a kind, image entities, the pets
  expected there and how visits form. Each new picture is queued for the
  engine (the same picture twice or a change of attributes adds nothing,
  and a restart takes nothing twice), analyzed one at a time with a growing
  wait while the engine is busy or stopped, and recorded in a journal with
  the pets it shows. Each picture is deleted once analyzed; the journal and
  the crops of the animals are kept 7 days.
- Camera sources: when the source's trigger fires (a binary sensor turning
  on, or an event entity receiving an event), each camera takes a burst of
  pictures, 6 by default, 10 seconds apart.
- Entities of each source (**Last visitor**, **Last picture**), of each pet
  (**Last seen**, **Last picture**), and **Pending pictures** on the
  integration's device. The latest picture of a source where an animal was
  found is kept for its last picture; a picture without an animal never is.
  The journal drops visits only as a whole, and keeps the visits these
  entities show, so that they keep their value.
- Sources follow the renames of their entities, and a repair with a fix
  names a source whose entity was removed.
- Visits counted per day, once they ended: **Visits today** for each
  source, unknown visits included, and one count per pet expected there,
  **meals** at a feeder and **visits** elsewhere, each with yesterday's
  count as an attribute. A visit counts on the day it started, and the
  counts start again at midnight. They are computed again from the journal
  after a restart. The counts of pets removed, or no longer expected at a
  source, are removed.
- A **Visit** event entity for each source, which announces `visit_ended`
  once for each visit, with its pets, whether another animal or an unknown
  one came, its start and end, and its number of pictures. A visit that
  ended while Home Assistant was stopped is announced when it starts again.
- Releases are published from the version of the manifest, which the app
  shares.
