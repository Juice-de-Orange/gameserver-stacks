"""Valheim's world modifiers are editable — and a typo does not start the server broken.

The five world modifiers (combat, death penalty, resources, raids, portals) and the four
`-setkey` switches decide how the world plays, so the config editor must offer them.

**Why these cannot be ordinary fields:** the image knows about twenty env variables — there is
none for the modifiers. The vendor sets them as start arguments, and `SERVER_ARGS` is the one
variable passed through unchanged. The fields therefore carry pseudo keys (`modifier.combat`,
`setkey.nomap`, `arg.saveinterval`), and the driver translates them into the argument string.

Two things that can go wrong and are pinned down here:
1. **Custom arguments must not get lost** just because the dashboard writes a modifier.
2. **An invalid value must be caught HERE.** Valheim does start with it but only writes one line
   to the log — the container looks healthy, and the world runs with different rules than
   displayed.
"""
from __future__ import annotations

import json

import pytest

from app.models.server import GameServer, GameType
from app.services.drivers.valheim import ValheimDriver


def _srv(args: str = "", **extra) -> GameServer:
    env = {"SERVER_NAME": "Valhalla", "WORLD_NAME": "Dedicated"}
    if args:
        env["SERVER_ARGS"] = args
    env.update(extra)
    s = GameServer(key="vh", game_type=GameType.valheim, container_name="valheim",
                   data_dir="/nothing", display_name="Valheim",
                   extra=json.dumps({"env": env}))
    s.id = 1
    return s


def _field(server, key):
    return next(f for f in ValheimDriver().config_fields(server) if f.key == key)


def test_the_five_axes_and_four_switches_are_editable():
    server = _srv()
    keys = {f.key for f in ValheimDriver().config_fields(server)}
    for axis in ("combat", "deathpenalty", "resources", "raids", "portals"):
        assert f"modifier.{axis}" in keys
    for switch in ("nobuildcost", "playerevents", "passivemobs", "nomap"):
        assert f"setkey.{switch}" in keys


def test_the_choices_come_from_the_vendor_guide():
    field = _field(_srv(), "modifier.deathpenalty")
    # Empty = default, then the vendor's five levels.
    assert field.choices == ("", "casual", "veryeasy", "easy", "hard", "hardcore")
    assert "default" in field.help


def test_existing_arguments_are_read():
    server = _srv("-modifier combat veryhard -setkey nomap -saveinterval 1200")
    assert _field(server, "modifier.combat").value == "veryhard"
    assert _field(server, "setkey.nomap").value == "true"
    assert _field(server, "setkey.passivemobs").value == "false"
    assert _field(server, "arg.saveinterval").value == "1200"


def test_custom_arguments_survive_a_change():
    """Whoever added `-crossplay` by hand does not lose it by changing the combat setting."""
    known, rest = ValheimDriver._split_args("-crossplay -modifier combat easy")
    assert rest == ["-crossplay"]
    known["modifier.combat"] = "veryhard"
    new = ValheimDriver._build_args(known, rest)
    assert new.startswith("-crossplay")
    assert "-modifier combat veryhard" in new
    assert "easy" not in new


def test_an_invalid_value_is_rejected():
    """Valheim starts with it — and then plays by different rules than the UI shows."""
    with pytest.raises(ValueError) as error:
        ValheimDriver().set_config(_srv(), {"modifier.combat": "brutal"})
    assert error.value.code == "config.valheim_invalid_modifier"


def test_an_empty_value_means_default(monkeypatch):
    """A modifier must also be switchable off again."""
    known, rest = ValheimDriver._split_args("-modifier raids none")
    assert known["modifier.raids"] == "none"
    known.pop("modifier.raids")
    assert ValheimDriver._build_args(known, rest) == ""


def test_the_save_interval_wants_a_number():
    with pytest.raises(ValueError) as error:
        ValheimDriver().set_config(_srv(), {"arg.saveinterval": "half-hourly"})
    assert error.value.code == "config.valheim_seconds_expected"


def test_the_help_texts_describe_the_DRIVER_default_not_the_image_default():
    """The image defaults do not apply: `ValheimDriver.container_env` sets `BACKUPS=false` and
    `RESTART_CRON=""`.

    A help text that describes the image default as in force is the same mistake as a comment
    that takes a code default for the deployed state — only in the place an operator reads
    instead of a developer.
    """
    help_texts = {f.key: f.help for f in ValheimDriver().config_fields(_srv())}
    for key in ("BACKUPS", "RESTART_CRON"):
        assert "default of the DRIVER" in help_texts[key], (
            f"{key} claims again that the image default applies"
        )
    # `UPDATE_CRON` is the cross-check: THIS image default really applies.
    assert "image default" in help_texts["UPDATE_CRON"]
    assert "NOT reset it" in help_texts["UPDATE_CRON"]
