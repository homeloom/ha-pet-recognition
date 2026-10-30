# Pet Recognition

Tell which of your pets appears in pictures from Home Assistant, count their
visits, and learn from your answers. Everything runs locally.

> **Status: 0.4.** Pets are recognized at their sources, and their visits
> counted per day. When it is unsure, it asks on the phones, and learns
> from the answers and from the corrections you make. Dashboards can show
> the visits and correct them.

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

Several photos can be added at once, in two ways:

- **Add the photos of a media folder**: upload the photos in **Media**,
  under **My media**, in a folder of their own (the upload there takes
  several files at once, on a phone too), then choose that folder. Folders
  are looked for a few levels deep in the media directories, without
  following symbolic links; each JPEG, PNG or WebP photo of the folder is
  added, without its subfolders. Other files, such as HEIC photos straight
  from a phone, are left out: export them as JPEG first. The files are only read: they stay in
  the media until you delete them.
- **A zip archive** of photos, uploaded in the photo field. Its entries
  are read one by one in memory, never extracted to disk. Folders, hidden
  files and macOS metadata are skipped; it holds at most 1000 entries and
  256 MB once uncompressed, and each photo is told by its content, not by
  its extension.

The photos are added while the form shows its progress, a few seconds
each, then a summary lists the photos added and those skipped, by file
name: near copies, no single animal of the species, not a JPEG, PNG or
WebP photo or too large, or not added because the engine could not
analyze them or the pet is full. An engine that cannot be reached stops
the batch.

A pet's gear button (**Change pet**) adds photos, one or several at once,
removes examples (shown with their pictures), or renames the pet. A pet without examples cannot be
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
Then choose the pets expected there, or none for every pet, and whether to
**ask when unsure** (see [Questions on the phones](#questions-on-the-phones)).
The **Visits**
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
  found. At a source where every picture shows a pet, its latest picture,
  with an animal or not.

**Last visitor**, **Correct last visit** and **Visits to review** show the
picture of their visit as their entity picture: the crop of the animal, as
a question shows it, or the picture itself when no animal was found in it,
at a source where every picture shows a pet. Its URL is signed for two
hours, signed again each hour, and left out of the recorder.

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
  (a part of the visit is still unknown), `start`, `end` and `pictures`,
  and `visit_corrected`, with the same data, each time a correction
  changes a visit that already ended. Use it in automations, such as a
  notification when a pet ate.
- **Visits to review**: the visits that ended unsure, among the pictures
  kept, with the ID of the latest as `latest_visit_id`.

A visit counts on the day it started, even when it ends after midnight, and
the counts start again at midnight. Each count gives the day before as its
`yesterday` attribute, and keeps long-term statistics in Home Assistant.
The counts are computed again from the journal, so a restart loses none,
and a visit that ended while Home Assistant was stopped is announced once
when it starts again. The counts of a pet removed, or no longer expected at
a source, are removed.

### Correcting visits

Each source's device has **Correct last visit**, which shows who was in its
latest run of pictures: a pet expected there, `unknown_animal` or
`other_animal`, and nothing for several pets. Choosing another option
corrects the run. The **Assign visit** action (`pet_recognition.assign_visit`,
for administrators) corrects a whole visit: give its `visit_id`, from the
**Visit** events or **Visits to review**, or an entity of its source for its
latest visit, and the pet devices that were there, with `other_animal` for
an animal that is none of them. No pet says it is unknown. It can answer
with the visit as it is now:

```yaml
action: pet_recognition.assign_visit
data:
  source: sensor.feeder_visits_to_review
  pets:
    - <device ID of a pet>
```

A correction relabels the pictures, so the counts and the visit follow,
settles the questions about them, and announces `visit_corrected` once the
visit ended. A single pet teaches the animal, as an answer does (see
[Questions on the phones](#questions-on-the-phones)). The long-term
statistics of a past day keep their value: a correction changes the journal,
the counts of today and the events.

### Dashboards

Two websocket commands serve dashboards, for any user who is logged in.

`pet_recognition/overview` gives the pets, the sources and the visits of a
period, of every loaded entry. The period is yesterday and today by
default, or from `start` to `end` (ISO 8601 times; a time without an offset
is local time). A visit is in it when it goes on at some time of the
period. Visits older than the days pictures are kept are never given, even
while they still are.

```json
{"id": 1, "type": "pet_recognition/overview", "start": "2026-10-29T00:00:00+01:00"}
```

```json
{
  "pets": [
    {"id": "<pet subentry ID>", "name": "Pet a", "species": "cat",
     "device_id": "<device ID>"}
  ],
  "sources": [
    {"id": "<source subentry ID>", "name": "Feeder", "kind": "feeder",
     "pets": ["<pet subentry ID>"], "device_id": "<device ID>",
     "visit_entity": "event.feeder_visit"}
  ],
  "visits": [
    {"id": "<visit ID>", "source": "<source subentry ID>",
     "start": "2026-10-29T07:58:12+00:00", "end": "2026-10-29T08:01:40+00:00",
     "ended": true, "pets": ["<pet subentry ID>"], "other_animal": false,
     "unsure": false,
     "picture": "/api/pet_recognition/picture/<entry ID>/<crop ID>?authSig=<signature>"}
  ]
}
```

- `species` is `cat`, `dog` or `bird`, and `kind` one of `feeder`, `water`,
  `litter_box`, `pet_door` and `other`.
- A source's `pets` are those expected there: every pet when none was
  chosen. `visit_entity` is its **Visit** event entity, and `null` when it
  is not registered.
- `device_id` is `null` when the device was removed.
- Visits are sorted by `start`, times are in UTC. `ended` is false while a
  picture can still join the visit. `pets` are sorted by their key and
  leave out a pet that was removed. `unsure` says part of the visit is
  still unknown and was not corrected.
- `picture` is the crop of the animal that stands for the visit, as a
  question shows it: the largest animal of the last picture where one is
  seen, or the picture itself when no animal was found and the source keeps
  it (every picture shows a pet). It is a relative URL, signed for the user
  for 30 minutes, which loads without a header; `null` when there is none.

`pet_recognition/assign` corrects a visit as the **Assign visit** action
does, for administrators and for any user allowed to control the source's
**Correct last visit**. It takes the `visit_id`, the `pets` by their
subentry IDs (none says it is unknown), and `other_animal`, and answers
with the visit as it is now, as the overview gives it.

```json
{"id": 2, "type": "pet_recognition/assign", "visit_id": "<visit ID>",
 "pets": ["<pet subentry ID>"], "other_animal": false}
```

```json
{"visit": {"id": "<visit ID>", "source": "<source subentry ID>", "...": "..."}}
```

An unknown visit or pet fails with the `home_assistant_error` code and
the translation keys `unknown_visit` and `unknown_pet_id`; a user who may
not control the source gets `unauthorized`.

### Pictures kept

A picture is deleted once analyzed, except the latest picture of each
source where an animal was found, which **Last picture** shows. A picture
where no animal is found, such as a garden camera's picture of people
passing by, is not kept, except at a source where every picture shows a
pet: there it is all there is to show of a visit where no animal was found,
so it is kept as long as what was found in it. What was found in each picture is kept in a
journal in Home Assistant's storage, with the crop of each animal under
`pet_recognition/` in the configuration directory, for 7 days by default
(see the options) and at most 2000 pictures per source. A visit goes as a
whole, once it ended before that, and the latest visit of each source, and the visit where each
pet was last seen, stay past those limits, so that the entities keep their
value. At most 100 pictures wait for the engine. Deleting
a source deletes its journal and crops.

### Renamed and missing entities

When you rename an entity of a source, or its trigger, the source follows
the new name. When one is removed from Home Assistant, a repair names the
source once Home Assistant has started, and its **Fix** chooses the
entities the source uses now.

## Options

**Configure** on the integration's entry chooses the phones to ask, and
tunes how the pictures of the sources are recognized. Changes apply once
the integration loads again, which it does at once:

- **Phones to ask**: the phones of the Home Assistant companion app that
  get the questions. None chosen means no questions.
- **Ask while learning** (on): while a pet has no examples yet, ask about
  one visit at a time.

The **Recognition** section holds:

- **Score to decide** (0.6): how much an animal must resemble the examples
  of a pet, from 0 to 1, for the pet to be decided. Higher means fewer
  mistakes and more unknown visits.
- **Margin over the next pet** (0.04): how far ahead of the next pet the
  score must be.
- **Smallest animal** (1 %): animals smaller than this share of a picture
  are left out, such as far away in a garden.
- **Animals per picture** (5): the most animals looked for in one picture.
- **Days kept** (7, from 2 to 30): how long pictures, their crops and the
  journal are kept. Home Assistant keeps the long-term statistics of the
  counts.

Photos of pets, which become examples, keep the default smallest animal
and animals per picture. Changes to the recognition apply to new pictures.

## Questions on the phones

Once a visit has ended, each part of it where no pet could be told becomes
a question, sent to each phone of the options: "Who was at Feeder at
08:12?", with the crop of the animal, or the picture itself when no animal
was found at a source where every picture shows a pet, and three buttons,
the two likeliest pets and **Other**, since Android shows three. A part of
a visit is asked about once, and at most two hours after the visit ended,
so that a restart after a while asks nothing old.

The questions are grouped together on the phone, with the title **Pet
Recognition** and a paw icon. On iOS, the icon makes them communication
notifications, sent by that title, on a purple background; a doubt is a
normal notification, and a question while learning comes without a sound.
On Android, the icon is purple, and the questions come in their own **Pet
Recognition** channel, whose sound and importance are set in the settings
of the phone.

- **While learning**, when a pet expected at the source has no examples
  yet, every visit is unknown: one question at a time is open per source,
  and it expires after two hours without an answer, which removes it from
  the phones. Turn off **Ask while learning** to wait for the photos you
  give.
- **Once each pet has examples**, a source asks when unsure, unless its
  **Ask when unsure** is off.

A question is saved before it is sent, with the phones it reached, so a
restart sends it to the phones still missing. A phone that fails is tried
again after a minute, then longer each time, up to half an hour, for as
long as the question can be asked. A phone that is no longer a device of
the companion app raises a repair: choose the phones again in the options.
Removing a source, or the integration, removes its open questions from the
phones.

A button answers the question:

- **A pet** corrects the pictures of the question to that pet, so the visit
  and the counts show it, settles the question and removes it from every
  phone. A picture recognized on its own since the question was asked
  keeps its pet.
- **Other** closes the question without telling who it was, removes it
  from the phones, and leaves the visit to review.

An answer counts once, for an open question, and only from the user a
phone asked was registered for in the companion app. A second answer, from
another phone, changes nothing.

An answer also teaches: when the picture shown, or another picture of the
question, has a single animal, that animal becomes a learned example of the
pet, from its embedding or from its crop, which the engine embeds again.
Learned examples count towards the 200 of a pet, and make room first.
Correcting a picture again, to another pet, to several or to unknown, takes
its example back. Each time the examples change, and each time the
integration loads, the pictures that are still unknown are compared again
with every example, so one answer can tell who was at other visits and
settle their questions too. The comparisons run in the background, outside
Home Assistant's event loop.

**What leaves Home Assistant**: the notifications go through the push relay
of the companion apps, with the names of the pets and the source, the time,
and a link to the crop or the picture. They stay in Home Assistant: the
phone loads them from Home Assistant, with its own sign-in or through a
link signed for 24 hours. So the phone must reach Home Assistant to show
them.

## How it works

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
- When it is unsure, it can send a notification with the picture and
  buttons for the likeliest pets. The answer corrects the visit and becomes
  a new example.

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
