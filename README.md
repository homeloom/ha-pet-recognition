# Pet Recognition

Tell which of your pets appears in pictures from Home Assistant, count their
visits, and learn from your answers. Everything runs locally.

> **Status: first release.** Pets are recognized at their sources, and
> their visits counted per day. Questions when it is unsure, and learning
> from the answers, come in a later release.

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

## Pets

Add each pet with **Add pet** on the integration's page: a name, a species
(cat, dog or bird), then photos. Upload photos, or take one now from a
camera or image entity, where the pet is the only animal of its species:
the engine crops the animal and turns it into an **example**. A few
photos from different angles and lights are a good start. Photos where
the engine finds no animal or several are refused, and so are near copies
of an example.

A pet's gear button (**Change pet**) adds photos, removes examples (shown
with their pictures), or renames the pet. A pet without examples cannot be
recognized, and a repair says so.

Examples are kept in Home Assistant's storage, and their crops under
`pet_recognition/` in the configuration directory, never in `www`. Each
pet keeps at most 200 examples of the engine's model; when it is full, its
oldest learned example makes room, and the photos you gave are never
dropped. Deleting a pet or the integration deletes its examples. When the
engine's model changes, the examples are embedded again from their crops,
in the background, the photos you gave first.

## Sources

Add each place where pets are seen with **Add source**: a name, a kind
(feeder, water fountain, litter box, pet door or other), and one or more
image or camera entities, such as a feeder's snapshot or a garden camera.
Then choose the pets expected there, or none for every pet. The **Visits**
section sets the gap between two visits (5 minutes), how late pictures may
arrive (2 minutes; cloud devices may need more), and whether every picture
shows a pet, which is on by default for a feeder or a litter box without a
camera.

Each new picture of the image entities is sent to the engine, oldest first,
one at a time. The picture an entity shows when its source is added is not
new, and neither is a picture the source already has: an entity's state,
the time of its last picture, must change, and the SHA-256 of the picture
must differ from the pictures of the source, waiting or recorded. Pictures
wait for the engine while it is busy or stopped, and survive a restart. A
picture the engine cannot read, or fails on three times while it otherwise
answers, is recorded as such and never blocks the others.

### Cameras

A camera shows a live view, so it takes pictures only when the source's
**trigger** fires: a binary sensor turning on, such as a motion sensor,
including its first motion after Home Assistant starts, or an event entity
receiving an event, such as a doorbell. Each camera of the
source then takes a few pictures (6 by default), a few seconds apart (10 by
default), set in the **Cameras** section. A trigger that fires again during
those pictures adds nothing, and a picture the source already has, such as
the same frame of a still scene, is not sent again. A camera added to a
source that already exists leaves **every picture shows a pet** as it was:
turn it off if the camera also sees scenes without a pet.

### Entities

Each source has a device with:

- **Last visitor**: the pet of the latest visit, `several`, `unknown_animal`
  when no pet could be told, or `other_animal`. Its `pets` attribute lists
  the pets of that visit.
- **Last picture**: the latest picture of the source where an animal was
  found.

Each pet's device adds **Last seen**, when the latest visit where the pet
was recognized started, with the source and the confidence as attributes,
and **Last picture**, the pet's crop in that picture. The integration's own
device has **Pending pictures**, the pictures waiting for the engine.

### Visits and counts

Pictures of a source that follow each other, each at most the gap after
the previous one, form a visit: a meal at a feeder, a trip to the litter
box. A visit ends when no picture can join it any more, the gap and the
lateness of pictures after its last picture. Each pet counts once per
visit, even when two pets take turns.

Each source's device then has:

- **Visits today**: the visits that ended among those that started today,
  unknown ones included.
- One count per pet expected at the source, **<pet> meals today** at a feeder
  and **<pet> visits today** elsewhere.
- **Visit**, an event entity that announces `visit_ended` once for each
  visit, with `visit_id`, `pets` (their names), `other_animal`, `unsure`
  (a part of the visit is still unknown), `start`, `end` and `pictures`.
  Use it in automations, such as a notification when a pet ate.

A visit counts on the day it started, even when it ends after midnight, and
the counts start again at midnight. Each count gives the day before as its
`yesterday` attribute, and keeps long-term statistics in Home Assistant.
The counts are computed again from the journal, so a restart loses none,
and a visit that ended while Home Assistant was stopped is announced once
when it starts again. The counts of a pet removed, or no longer expected at
a source, are removed.

### Pictures kept

A picture is deleted once analyzed, except the latest picture of each
source where an animal was found, which **Last picture** shows. A picture
where no animal is found, such as a garden camera's picture of people
passing by, is never kept. What was found in each picture is kept in a
journal in Home Assistant's storage, with the crop of each animal under
`pet_recognition/` in the configuration directory, for 7 days and at most
2000 pictures per source. A visit goes as a whole, once it ended more than
7 days ago, and the latest visit of each source, and the visit where each
pet was last seen, stay past those limits, so that the entities keep their
value. At most 100 pictures wait for the engine. Deleting
a source deletes its journal and crops.

### Renamed and missing entities

When you rename an entity of a source, or its trigger, the source follows
the new name. When one is removed from Home Assistant, a repair names the
source once Home Assistant has started, and its **Fix** chooses the
entities the source uses now.

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
