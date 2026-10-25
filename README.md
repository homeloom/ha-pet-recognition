# Pet Recognition

Tell which of your pets appears in pictures from Home Assistant, count their
visits, and learn from your answers. Everything runs locally.

> **Status: in development.** This version only contains the recognition
> logic, in plain Python. It cannot be set up in Home Assistant yet.

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
- `pet_recognition_engine/` (to come): the app that runs the models.

The integration and the app share one version and are released together.

## License

[MIT](LICENSE).
