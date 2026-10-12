"""Version catalog: source parsers + catalog maintenance (feed for the version dropdown).

Network-free: the parsers get realistically shaped response excerpts, the fetchers are
monkeypatched. That is exactly why parsers and network wrappers are separate.
"""
import asyncio

from app.models.server import GameServer, GameType
from app.services import version_catalog as vc
from app.services.drivers import VersionSource

# --- real response shapes (shortened, but structurally identical to the live sources) ------

PZ_TAGS = {
    "results": [
        {"name": "latest", "last_updated": "2026-07-29T14:07:49.858847Z"},
        {"name": "latest-release", "last_updated": "2026-07-29T14:07:48.202109Z"},
        {"name": "42.20.0-release", "last_updated": "2026-07-29T14:07:46.476999Z"},
        {"name": "latest-unstable", "last_updated": "2026-06-01T11:31:24.356424Z"},
        {"name": "42.19.0-unstable", "last_updated": "2026-06-01T11:31:21.741477Z"},
        {"name": "41.78.19-release", "last_updated": "2026-04-09T02:07:24.928472Z"},
    ]
}

PZ_NEWS = {
    "appnews": {
        "newsitems": [
            {
                "title": "Build 42.20.0 Stable Released",
                "url": "https://example.invalid/42200",
                "contents": "[h3]What's new[/h3] Animals, basements and new maps.",
                "date": 1785324125,
            },
            {
                "title": "Stable(41.78.19) + UNSTABLE(42.16.3) Hotfixes Released",
                "url": "https://example.invalid/hotfix",
                "contents": "Collective hotfix.",
                "date": 1775656140,
            },
            {"title": "SOME NEW THINGS", "url": "https://example.invalid/blog", "contents": "Blog"},
        ]
    }
}

PAPER = {"versions": {"26.2": ["26.2", "26.2-rc-2"], "1.21": ["1.21.11", "1.21.10", "1.21.9-pre2"]}}

MOJANG = {
    "versions": [
        {"id": "26.2", "type": "release", "releaseTime": "2026-06-16T12:03:33+00:00"},
        {"id": "26.3-snapshot-6", "type": "snapshot", "releaseTime": "2026-07-20T10:00:00+00:00"},
        {"id": "rd-132211", "type": "old_alpha", "releaseTime": "2009-05-13T20:11:00+00:00"},
    ]
}

VS = {
    "1.22.6": {"windows": {}, "linuxserver": {}, "linux": {}},
    "1.22.5": {"windows": {}, "linuxserver": {}},
    "1.22.4-clientonly": {"windows": {}},  # no linuxserver → not selectable for us
}


# --- PZ ------------------------------------------------------------------------------------
def test_pz_tags_drop_latest_and_classify_channels():
    entries = vc.parse_pz_tags(PZ_TAGS)
    by_ref = {e.ref: e for e in entries}
    # `latest`, `latest-release`, `latest-unstable` are moving targets → not selectable.
    assert set(by_ref) == {"42.20.0-release", "42.19.0-unstable", "41.78.19-release"}
    assert by_ref["42.20.0-release"].channel == "stable"
    assert by_ref["42.20.0-release"].version == "42.20.0"
    assert by_ref["42.19.0-unstable"].channel == "unstable"
    assert by_ref["41.78.19-release"].channel == "legacy"   # Build 41 = frozen legacy branch
    assert by_ref["42.20.0-release"].released_at.year == 2026


def test_pz_changelog_maps_every_version_in_a_title():
    notes = vc.parse_pz_changelogs(PZ_NEWS)
    assert notes["42.20.0"]["title"] == "Build 42.20.0 Stable Released"
    assert "[h3]" not in notes["42.20.0"]["summary"]        # BBCode stripped
    # A collective hotfix affects SEVERAL versions — both must be mapped.
    assert notes["41.78.19"]["url"] == "https://example.invalid/hotfix"
    assert notes["42.16.3"]["url"] == "https://example.invalid/hotfix"
    assert "SOME NEW THINGS" not in str(notes)              # title without a version → ignored


def test_apply_notes_only_on_exact_version():
    entries = vc.parse_pz_tags(PZ_TAGS)
    merged = {e.ref: e for e in vc.apply_notes(entries, vc.parse_pz_changelogs(PZ_NEWS))}
    assert merged["42.20.0-release"].changelog_title == "Build 42.20.0 Stable Released"
    # 42.19.0 has no matching post → better no note than one of another version.
    assert merged["42.19.0-unstable"].changelog_title == ""


def test_sort_entries_newest_first():
    order = [e.version for e in vc.sort_entries(vc.parse_pz_tags(PZ_TAGS))]
    assert order == ["42.20.0", "42.19.0", "41.78.19"]


def test_prereleases_sort_below_their_final_version():
    """By plain number sequence "26.2-rc-2" → (26,2,2) is greater than "26.2" → (26,2) — the
    release candidate would stand ABOVE the final version in the dropdown."""
    order = [e.version for e in vc.sort_entries(vc.parse_paper_versions(PAPER))]
    assert order.index("26.2") < order.index("26.2-rc-2")
    assert order.index("1.21.11") < order.index("1.21.9-pre2")
    assert order[0] == "26.2"          # newest FINAL version leads the list
    assert vc.sort_key("26.2") > vc.sort_key("26.2-rc-2")
    assert vc.base_version("1.21.11-rc3") == (1, 21, 11)


def test_channel_quotas_keep_stable_versions_visible():
    """A global cap would cut off the stable Minecraft vanilla releases behind Mojang's many
    snapshots — so cap per channel."""
    many_snapshots = [
        vc.CatalogEntry(version=f"26.3-snapshot-{i}", ref=f"26.3-snapshot-{i}", channel="snapshot")
        for i in range(60)
    ]
    stables = [vc.CatalogEntry(version=f"1.21.{i}", ref=f"1.21.{i}", channel="stable") for i in range(5)]
    kept = vc.limit_entries(vc.sort_entries(many_snapshots + stables))
    assert len([e for e in kept if e.channel == "snapshot"]) == vc.MAX_PER_CHANNEL["snapshot"]
    assert len([e for e in kept if e.channel == "stable"]) == 5   # all stables survive


def test_lazy_refresh_is_rate_limited(monkeypatch, mem_engine):
    """With a permanently dead source not every page view may query all endpoints again."""
    monkeypatch.setattr(vc, "engine", mem_engine)
    vc._lazy_attempts.clear()
    assert vc._lazy_refresh_due("pz", "") is True
    assert vc._lazy_refresh_due("pz", "") is False    # within the lockout
    assert vc._lazy_refresh_due("minecraft", "PAPER") is True   # other combination untouched
    vc._lazy_attempts.clear()


def test_calendar_snapshots_are_dropped():
    """"26w14a" is a namespace of its own: (26,14) would lead the list although the version is
    older than "26.3-snapshot-6"."""
    versions = {e.version for e in vc.parse_mojang_versions(
        {"versions": [
            {"id": "26w14a", "type": "snapshot", "releaseTime": "2026-04-01T10:00:00+00:00"},
            {"id": "26.3-snapshot-6", "type": "snapshot", "releaseTime": "2026-07-28T10:00:00+00:00"},
        ]}
    )}
    assert versions == {"26.3-snapshot-6"}


# --- Minecraft ------------------------------------------------------------------------------
def test_paper_groups_are_flattened_and_prereleases_marked():
    by_ver = {e.version: e for e in vc.parse_paper_versions(PAPER)}
    assert "1.21.10" in by_ver and "26.2" in by_ver          # groups flattened
    assert by_ver["26.2"].channel == "stable"
    assert by_ver["26.2-rc-2"].channel == "snapshot"
    assert by_ver["1.21.9-pre2"].channel == "snapshot"


def test_mojang_keeps_only_release_and_snapshot():
    by_ver = {e.version: e for e in vc.parse_mojang_versions(MOJANG)}
    assert set(by_ver) == {"26.2", "26.3-snapshot-6"}        # old_alpha is dropped
    assert by_ver["26.2"].channel == "stable"
    assert by_ver["26.3-snapshot-6"].channel == "snapshot"


def test_mojang_patchnotes_have_text_but_no_fabricated_link():
    notes = vc.parse_mojang_patchnotes(
        {"entries": [{"version": "1.21.10", "title": "Minecraft: Java Edition 1.21.10",
                      "shortText": "Hotfix.", "date": "2025-10-07T13:50:57.708Z"}]}
    )
    assert notes["1.21.10"]["title"].endswith("1.21.10")
    assert notes["1.21.10"]["url"] == ""   # there is no derivable minecraft.net URL


# --- Vintage Story ---------------------------------------------------------------------------
def test_vs_keeps_only_versions_with_a_linux_server_build():
    versions = {e.version for e in vc.parse_vs_versions(VS)}
    assert versions == {"1.22.6", "1.22.5"}


# --- network outage ---------------------------------------------------------------------------
def test_fetchers_degrade_to_empty_when_source_is_down(monkeypatch):
    async def dead(url):
        return None

    monkeypatch.setattr(vc, "_get_json", dead)
    assert asyncio.run(vc.fetch_pz()) == []
    assert asyncio.run(vc.fetch_minecraft("PAPER")) == []
    assert asyncio.run(vc.fetch_vintagestory()) == []
    assert asyncio.run(vc.fetch_factorio()) == []
    assert asyncio.run(vc.fetch_avorion()) == []


def test_a_paper_outage_is_NOT_covered_up_with_the_vanilla_list(monkeypatch):
    """A Mojang vanilla fallback for a failed PaperMC source would undermine the staleness brake.

    The vanilla entries would be stored under the variant PAPER, `refresh_for` would report
    `ok=True` and `fetched_at` would be refreshed — the source would count as checked today
    although it was unreachable (`SOURCE_STALE_AFTER`). `source_ok` would stay True, the versions
    page would show no warning, and `auto_update` would compute against a stock from another
    source.

    The list would not even be wrong in content but **too large**: Paper does not build for every
    Mojang version, let alone snapshots. It would offer a change this module rules out in its own
    header — "an offer that fails with 'no such image' on change".

    Empty means here: `refresh_for` stores NOTHING, the last successfully fetched Paper list
    stays, its `fetched_at` ages, and after two daily runs the page says so. An old Paper list is
    worth any vanilla list for a Paper server.
    """
    async def fake(url):
        return None if url == vc.PAPER_URL else (MOJANG if url == vc.MOJANG_MANIFEST_URL else {})

    monkeypatch.setattr(vc, "_get_json", fake)
    assert asyncio.run(vc.fetch_minecraft("PAPER")) == []

    # For a NON-Paper server the Mojang list is still the right source.
    versions = {e.version for e in asyncio.run(vc.fetch_minecraft("VANILLA"))}
    assert versions == {"26.2", "26.3-snapshot-6"}


# --- catalog maintenance (DB) -----------------------------------------------------------------
def _pz_entries():
    return [
        vc.CatalogEntry(version="42.20.0", ref="42.20.0-release", channel="stable"),
        vc.CatalogEntry(version="42.19.0", ref="42.19.0-unstable", channel="unstable"),
    ]


def test_store_counts_new_only_once_and_prunes_withdrawn(monkeypatch, mem_engine):
    monkeypatch.setattr(vc, "engine", mem_engine)
    assert vc._store("pz", "", _pz_entries()) == {"total": 2, "new": 2}
    assert vc._store("pz", "", _pz_entries())["new"] == 0          # second run: nothing new
    # A withdrawn tag disappears — the table is a cache, not an archive.
    vc._store("pz", "", _pz_entries()[:1])
    assert {r.ref for r in vc._rows_for("pz", "")} == {"42.20.0-release"}


def test_entries_mark_current_and_offer_the_stable_upgrade(monkeypatch, mem_engine):
    monkeypatch.setattr(vc, "engine", mem_engine)
    vc._store("pz", "", _pz_entries())
    srv = GameServer(key="pz-1", game_type=GameType.pz, image_tag="42.19.0-unstable")

    data = asyncio.run(vc.entries_for(srv))
    by_ref = {e["ref"]: e for e in data["entries"]}
    assert by_ref["42.19.0-unstable"]["current"] is True
    assert by_ref["42.20.0-release"]["newer"] is True
    assert by_ref["42.20.0-release"]["is_new"] is True            # just seen
    # "Update available" deliberately suggests only stable versions.
    assert data["update_available"]["ref"] == "42.20.0-release"


def test_refresh_keeps_the_old_list_when_the_source_fails(monkeypatch, mem_engine):
    monkeypatch.setattr(vc, "engine", mem_engine)
    vc._store("pz", "", _pz_entries())

    async def dead(variant=""):
        return []

    monkeypatch.setitem(vc.FETCHERS, GameType.pz, dead)
    result = asyncio.run(vc.refresh_for(GameType.pz, ""))
    assert result["ok"] is False
    assert len(vc._rows_for("pz", "")) == 2      # better old than empty


def test_unsupported_game_says_so_instead_of_pretending(monkeypatch, mem_engine):
    """No selection list — but no empty screen either.

    This branch must not return BEFORE `current_ref_detected()`: for Arma Reforger `current`
    would stay empty although the driver can read the running version from the start banner,
    while the UI promises "Which version is running is shown in the overview".
    """
    monkeypatch.setattr(vc, "engine", mem_engine)
    srv = GameServer(
        key="rf-1", game_type=GameType.arma_reforger, game_version="1.7.0.54",
        image_repo="ghcr.io/acemod/arma-reforger", image_tag="sha256:9503881c",
    )
    data = asyncio.run(vc.entries_for(srv))
    assert data["supported"] is False and data["entries"] == []
    assert data["current"] == "1.7.0.54"
    assert data["image_ref"] == "ghcr.io/acemod/arma-reforger@sha256:9503881c"
    assert data["image_pinned"] is True
    assert "Steam" in data["note"]  # the honest reason, not a placeholder


def test_a_moving_image_is_reported_as_such(monkeypatch, mem_engine):
    """`latest` can move on upstream while an older image is still running here. Whoever does not
    see that takes it for pinned."""
    monkeypatch.setattr(vc, "engine", mem_engine)
    srv = GameServer(
        key="rf-1", game_type=GameType.arma_reforger,
        image_repo="ghcr.io/acemod/arma-reforger", image_tag="latest",
    )
    data = asyncio.run(vc.entries_for(srv))
    assert data["image_ref"] == "ghcr.io/acemod/arma-reforger:latest"
    assert data["image_pinned"] is False
    assert data["image_floating"] is True


def test_a_build_tag_is_neither_floating_nor_called_pinned_on_purpose(monkeypatch, mem_engine):
    """The project's own default, `sha-d042142`, was marked "floating" while the note right below
    said "pinned on purpose". A build tag moves only when its publisher re-pushes it; the note no
    longer claims anything the mark does not say."""
    monkeypatch.setattr(vc, "engine", mem_engine)
    srv = GameServer(
        key="rf-1", game_type=GameType.arma_reforger,
        image_repo="ghcr.io/acemod/arma-reforger", image_tag="sha-d042142",
    )
    data = asyncio.run(vc.entries_for(srv))
    assert data["image_pinned"] is False and data["image_floating"] is False
    assert "pinned on purpose" not in data["note"]


def test_variant_only_splits_the_catalog_for_minecraft():
    mc = GameServer(key="mc", game_type=GameType.minecraft, server_type="paper")
    assert vc.variant_for(mc) == "PAPER"
    assert vc.variant_for(GameServer(key="vs", game_type=GameType.vintagestory)) == ""


def test_detected_version_fills_in_when_nothing_is_pinned(monkeypatch, mem_engine):
    """Vintage Story deliberately runs without VERSION set. Without detection no catalog entry
    would be marked as running and `update_available` would be permanently empty — the version
    check would be silently switched off there."""
    monkeypatch.setattr(vc, "engine", mem_engine)
    vc._store("vintagestory", "", [
        vc.CatalogEntry(version="1.22.6", ref="1.22.6", channel="stable"),
        vc.CatalogEntry(version="1.21.6", ref="1.21.6", channel="stable"),
    ])
    srv = GameServer(key="vs", game_type=GameType.vintagestory, game_version="")

    class _Drv:
        # `current_ref` asks the driver via `versioned_by_image_tag`, not the game type — a dummy
        # for `get_driver` must therefore bring the version source along.
        version_source = VersionSource.env

        async def detect_version(self, server):
            return "1.21.6"

    monkeypatch.setattr("app.services.drivers.get_driver", lambda s: _Drv())
    data = asyncio.run(vc.entries_for(srv))

    assert data["current"] == "1.21.6"
    assert data["current_detected"] is True          # detected, not pinned — honestly labelled
    by_ref = {e["ref"]: e for e in data["entries"]}
    assert by_ref["1.21.6"]["current"] is True
    assert by_ref["1.22.6"]["newer"] is True
    assert data["update_available"]["ref"] == "1.22.6"


def test_a_pinned_version_is_never_overridden_by_detection(monkeypatch, mem_engine):
    monkeypatch.setattr(vc, "engine", mem_engine)
    vc._store("vintagestory", "", [vc.CatalogEntry(version="1.22.6", ref="1.22.6", channel="stable")])
    srv = GameServer(key="vs", game_type=GameType.vintagestory, game_version="1.22.6")
    data = asyncio.run(vc.entries_for(srv))
    assert data["current"] == "1.22.6" and data["current_detected"] is False


# --- Factorio: the tag IS the version ----------------------------------------------------------
def test_factorio_takes_only_exact_versions():
    """`2.0`, `2`, `latest`, `stable` are moving pointers — a pin on them holds nothing.

    `-rootless` is the same version in another build flavour; if it were in the list too, the
    dropdown would look like two versions.
    """
    names = ["latest", "stable", "2.1.14", "2.1", "2", "2.0.77", "stable-2.0.77",
             "2.0.77-rootless", "stable-rootless"]
    entries = vc.parse_factorio_tags(names)
    assert {e.ref for e in entries} == {"2.1.14", "2.0.77"}


def test_factorio_reads_the_channel_from_the_stable_tags():
    entries = {e.version: e.channel for e in vc.parse_factorio_tags(
        ["2.1.14", "2.0.77", "stable-2.0.77"]
    )}
    assert entries == {"2.0.77": "stable", "2.1.14": "unstable"}


def test_without_a_stable_tag_not_everything_becomes_a_prerelease():
    """The named limit: if the channel information is missing, "everything stable" is the better lie.

    Otherwise every selectable version would sit under a channel one has to expand first — and
    the impression would be that this game has no stable release at all.
    """
    entries = vc.parse_factorio_tags(["2.1.14", "2.0.77"])
    assert {e.channel for e in entries} == {"stable"}


def test_avorion_reads_the_channel_from_the_tag_name():
    """Here the channel is unambiguous in the name — unlike Factorio no workaround via a second
    tag pattern is needed."""
    entries = {e.ref: e.channel for e in vc.parse_avorion_tags(
        ["latest", "stable", "beta", "2.5.13.44140", "2.5.13.44140-beta", "2.5.11.43793"]
    )}
    assert entries == {
        "2.5.13.44140": "stable",
        "2.5.13.44140-beta": "unstable",
        "2.5.11.43793": "stable",
    }


def test_factorio_stays_in_its_image_flavour():
    """`2.0.77` and `2.0.77-rootless` are the same GAME VERSION in two build flavours.

    The dashboard runs the rootless flavour, because otherwise the container runs under another
    uid than the `console-api` and the config editor fails with "Permission denied" from the
    first start. Only what exists in THIS flavour may therefore be selectable — otherwise a
    version change would also be a change of user.
    """
    names = ["2.1.14", "2.0.77", "stable-2.0.77", "2.0.77-rootless", "2.1.14-rootless", "2.0.60"]
    refs = {e.ref for e in vc.parse_factorio_tags(names, variant="rootless")}
    assert refs == {"2.0.77-rootless", "2.1.14-rootless"}
    # 2.0.60 does not exist as rootless — it is dropped instead of offering a dead tag.
    assert not any(r.startswith("2.0.60") for r in refs)


def test_the_game_version_stays_readable_without_the_flavour():
    """The dropdown shows the version, not the image tag — otherwise every row reads like a game
    release of its own."""
    entries = vc.parse_factorio_tags(["2.0.77", "2.0.77-rootless"], variant="rootless")
    assert [(e.version, e.ref) for e in entries] == [("2.0.77", "2.0.77-rootless")]


def test_variant_for_reads_the_flavour_from_the_tag():
    from app.models.server import GameServer, GameType

    rootless = GameServer(key="fac", game_type=GameType.factorio, image_tag="2.0.77-rootless")
    normal = GameServer(key="fac", game_type=GameType.factorio, image_tag="2.0.77")
    assert vc.variant_for(rootless) == "rootless"
    assert vc.variant_for(normal) == ""
