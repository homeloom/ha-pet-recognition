# Pet Recognition

Tell which of your pets appears in pictures from Home Assistant, count their
visits, and learn from your answers. Everything runs locally.

> **Status: in development.** This version contains the recognition logic,
> the engine app, the connection to the engine, and its health. Pets and
> sources cannot be added yet.

## Setup

1. Install the **Pet Recognition Engine** app (see its documentation), or
   run its image with Docker.
2. Install the integration and restart Home Assistant.
3. Under the Supervisor, Home Assistant discovers the engine: confirm it in
   **Settings**, **Devices & services**. Otherwise, add **Pet Recognition**
   there and enter the engine's URL and API key.

The engine's address and key can be changed later with **Reconfigure**.
When the engine refuses its key, Home Assistant asks for the new one.

## Engine health

The integration asks the engine every minute whether it answers, even when
its entities are disabled. The **Engine** connectivity sensor turns off
while it does not, and the integration keeps loading: it does not need the
engine to start. Repairs appear in **Settings**, **Repairs**:

- when the engine has not answered as an engine for a quarter of an hour;
- when the engine no longer serves the API version of the integration, which
  then needs an update;
- when the engine and the integration are of different releases, which
  still work together but should be updated to the same one. Engines built
  without a release version, such as development builds, are not compared.

When the engine refuses its key, the integration stops asking it until the
new key is entered.

The diagnostics of the entry give the versions, the engine's limits and the
kind of the last failure, without the engine's address or key.

## How it will work

- **Pets** are declared in the Home Assistant UI, each with a few example
  photos.
- **Sources** are any image or camera entities: a pet feeder's snapshot, a
  litter box, a pet door, a garden camera.
- An app, **Pet Recognition Engine**, finds the animals in each picture and
  turns each of them into an embedding: a list of numbers that stays close
  for pictures of the same animal. It runs on your own hardware, with open
  models, and keeps no data.
- The integration compares each animal with the examples of every pet. It
  decides only when one pet is similar enough and clearly ahead of the
  others, and never while a pet expected at that source has no example yet.
- Pictures that follow each other at a source form a **visit**, such as a
  meal at a feeder. Visits are counted per pet, per source and per day.
- When it is unsure, it can send a notification with the picture and one
  button per pet. The answer corrects the visit and becomes a new example.

## Repository layout

- `custom_components/pet_recognition/`: the Home Assistant integration. Its
  `core/` package is plain Python, with no Home Assistant import.
- `pet_recognition_engine/`: the app that runs the models, for the
  Supervisor or Docker. Its `pet_engine` package finds the animals in a
  picture, embeds each of them, and serves them over HTTP.
- `repository.yaml`: makes this repository an app repository for the
  Supervisor.
- `docs/`: the API between the integration and the engine.

The integration and the app share one version and are released together.

## License

[MIT](LICENSE). The models the app downloads have their own licenses: see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
