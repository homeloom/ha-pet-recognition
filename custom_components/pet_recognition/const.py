"""Constants of the Pet Recognition integration."""

from typing import Final

DOMAIN: Final = "pet_recognition"
TITLE: Final = "Pet Recognition"

# The engine refuses shorter keys.
MIN_KEY_LENGTH: Final = 16

# The subentry of a pet, and its data.
SUBENTRY_PET: Final = "pet"
CONF_SPECIES: Final = "species"
# The species a pet can be, which the engine detects.
SPECIES: Final = ("cat", "dog", "bird")

# An animal smaller than this share of a picture is left out.
MIN_AREA: Final = 0.01
# The most animals asked of the engine in one picture.
DETECTION_LIMIT: Final = 5
# The most examples a pet keeps per model.
EXAMPLE_LIMIT: Final = 200

# The subentry of a source of pictures, and its data.
SUBENTRY_SOURCE: Final = "source"
CONF_KIND: Final = "kind"
CONF_ENTITIES: Final = "entities"
CONF_PETS: Final = "pets"
CONF_ADVANCED: Final = "advanced"
CONF_GAP: Final = "gap"
CONF_LATE: Final = "late"
CONF_EVERY_PICTURE: Final = "every_picture_shows_a_pet"
CONF_CAMERA: Final = "camera"
CONF_TRIGGER: Final = "trigger"
CONF_FRAMES: Final = "frames"
CONF_INTERVAL: Final = "interval"
# What a source is, for its names and defaults.
KINDS: Final = ("feeder", "water", "litter_box", "pet_door", "other")
# Sources that take pictures only when a pet is there.
KINDS_SHOWING_A_PET: Final = frozenset({"feeder", "litter_box"})
# In minutes: the longest wait between two pictures of a visit, and how late
# pictures may arrive.
DEFAULT_GAP: Final = 5
DEFAULT_LATE: Final = 2
# The pictures a camera takes when its trigger fires, and the seconds between
# them.
DEFAULT_FRAMES: Final = 6
DEFAULT_INTERVAL: Final = 10

# How long pictures and their crops are kept, in days.
RETENTION_DAYS: Final = 7
# The most pictures waiting for the engine, and kept per source.
QUEUE_LIMIT: Final = 100
PICTURE_LIMIT: Final = 2000
