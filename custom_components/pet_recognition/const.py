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
