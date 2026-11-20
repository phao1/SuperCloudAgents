"""Named converters for older desk records."""

from deskcore import bulletins

UPGRADES = {
    bulletins.NAME: bulletins.from_paper,
}
