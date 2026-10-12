"""The firewall helper's rule generator, plan and backend choice (`dashboard/firewall/`).

Pure logic and an in-memory iptables (`firewall_fake.py`). The same helper against a real
iptables, in a throwaway network namespace: `dashboard/firewall/netns-check.sh`.
"""
from __future__ import annotations

import os

import pytest
from firewall_fake import FakeIptables  # first: puts dashboard/firewall on sys.path
from gss_firewall import iptables
from gss_firewall.rules import (
    HOOK_RULE,
    Allow,
    Desired,
    FamilyState,
    Refused,
    parse_desired,
    parse_port_range,
    plan_family,
    plan_id,
    restore_script,
    rule_for,
    rules_for,
)
from gss_firewall.service import Helper, PlanChanged


def _allow(server, port, proto, address=""):
    return {"server": server, "port": port, "proto": proto, "address": address}


# ------------------------------------------------------------------ rule generator


def test_a_rule_is_written_the_way_iptables_prints_it():
    assert rule_for(Allow(2456, "udp", "", "vh-1"), "") == (
        "-A GSS-ALLOW -p udp -m conntrack --ctstate DNAT --ctorigdstport 2456 --ctdir ORIGINAL -j ACCEPT")
    assert rule_for(Allow(25565, "tcp", "192.0.2.10", "mc-1"), "198.51.100.0/24") == (
        "-A GSS-ALLOW -s 198.51.100.0/24 -p tcp -m conntrack --ctstate DNAT --ctorigdst 192.0.2.10 "
        "--ctorigdstport 25565 --ctdir ORIGINAL -j ACCEPT")


def test_every_rule_is_an_accept_in_the_own_chain():
    d = parse_desired({"allows": [_allow("a", 2456, "udp"), _allow("b", 25565, "tcp", "192.0.2.10")],
                       "sources": ["198.51.100.0/24", "2001:db8::/32"]})
    for family in ("ipv4", "ipv6"):
        for rule in rules_for(d, family):
            assert rule.startswith("-A GSS-ALLOW ") and rule.endswith(" -j ACCEPT")
            assert "DROP" not in rule and "REJECT" not in rule


def test_sources_and_addresses_go_to_their_own_family():
    d = parse_desired({"allows": [_allow("a", 2456, "udp"), _allow("b", 25565, "tcp", "192.0.2.10"),
                                  _allow("c", 30000, "udp", "2001:db8::10")],
                       "sources": ["198.51.100.7", "2001:db8:1::/48"]})
    v4, v6 = rules_for(d, "ipv4"), rules_for(d, "ipv6")
    assert all("-s 198.51.100.7/32" in r for r in v4) and len(v4) == 2
    assert all("-s 2001:db8:1::/48" in r for r in v6) and len(v6) == 2
    assert not any("2001:db8::10" in r for r in v4) and not any("192.0.2.10" in r for r in v6)


def test_sources_of_one_family_only_open_nothing_in_the_other():
    d = parse_desired({"allows": [_allow("a", 2456, "udp")], "sources": ["198.51.100.0/24"]})
    assert rules_for(d, "ipv6") == []
    assert len(rules_for(d, "ipv4")) == 1


def test_a_zero_prefix_source_means_from_anywhere():
    d = parse_desired({"allows": [_allow("a", 2456, "udp")], "sources": ["0.0.0.0/0"]})
    assert rules_for(d, "ipv4") == ["-A GSS-ALLOW -p udp -m conntrack --ctstate DNAT --ctorigdstport 2456 --ctdir ORIGINAL -j ACCEPT"]


def test_the_same_port_twice_is_one_rule():
    d = Desired((Allow(2456, "udp", "", "a"), Allow(2456, "udp", "", "b")), ())
    assert len(rules_for(d, "ipv4")) == 1


@pytest.mark.parametrize("item, fragment", [
    (_allow("ssh", 22, "tcp"), "outside the allowed range"),
    (_allow("x", 70000, "tcp"), "not a port number"),
    (_allow("x", True, "tcp"), "not a port number"),
    (_allow("x", "2456", "udp"), "not a port number"),
    (_allow("x", 2456, "icmp"), "not tcp or udp"),
    (_allow("x", 2456, "udp", "127.0.0.1"), "cannot be reached from outside"),
    (_allow("x", 2456, "udp", "example.com"), "not an IP address"),
    (_allow("x y", 2456, "udp"), "not a server key"),
    (_allow("x;rm", 2456, "udp"), "not a server key"),
])
def test_the_positive_list_refuses_everything_else(item, fragment):
    with pytest.raises(Refused) as ei:
        parse_desired({"allows": [item], "sources": []})
    assert any(fragment in p for p in ei.value.problems), ei.value.problems


def test_a_bad_source_and_too_many_entries_are_refused():
    with pytest.raises(Refused):
        parse_desired({"allows": [], "sources": ["198.51.100.0/24 -j DROP"]})
    with pytest.raises(Refused):
        parse_desired({"allows": [_allow("a", 2000 + i, "udp") for i in range(513)], "sources": []})
    with pytest.raises(Refused):
        parse_desired(["not", "an", "object"])


def test_the_port_range_setting():
    assert parse_port_range("") == (1024, 65535)
    assert parse_port_range("2000-3000") == (2000, 3000)
    for bad in ("22", "3000-2000", "0-10", "1-70000", "a-b"):
        with pytest.raises(ValueError):
            parse_port_range(bad)
    with pytest.raises(Refused):
        parse_desired({"allows": [_allow("a", 25565, "tcp")]}, (2000, 3000))


# ------------------------------------------------------------------ plan


WANT = ["-A GSS-ALLOW -p udp -m conntrack --ctstate DNAT --ctorigdstport 2456 --ctdir ORIGINAL -j ACCEPT"]


def test_plan_from_nothing_creates_the_chain_and_one_jump():
    state = FamilyState(hook_chain=True, hook_lines=["-A DOCKER-USER -j RETURN"])
    plan = plan_family(state, WANT)
    assert (plan.create_chain, plan.hook, plan.add, plan.remove) == (True, "insert", WANT, [])
    script = restore_script(state, plan)
    assert script.splitlines() == ["*filter", ":GSS-ALLOW - [0:0]", *WANT,
                                   "-I DOCKER-USER 1 -j GSS-ALLOW", "COMMIT"]


def test_plan_of_the_wanted_state_is_empty():
    state = FamilyState(True, True, list(WANT), [HOOK_RULE, "-A DOCKER-USER -j RETURN"])
    plan = plan_family(state, WANT)
    assert plan.changes() == 0 and plan.keep == WANT


def test_plan_removes_unwanted_rules_and_duplicates_by_spec_never_by_number():
    stale = "-A GSS-ALLOW -p tcp -m conntrack --ctstate DNAT --ctorigdstport 25565 --ctdir ORIGINAL -j ACCEPT"
    state = FamilyState(True, True, [WANT[0], WANT[0], stale], [HOOK_RULE])
    plan = plan_family(state, WANT)
    assert sorted(plan.remove) == sorted([WANT[0], stale]) and plan.add == []
    script = restore_script(state, plan)
    assert ":GSS-ALLOW" not in script, "--noflush would flush an existing chain that is declared again"
    deletes = [line for line in script.splitlines() if line.startswith("-D")]
    assert all(line.startswith("-D GSS-ALLOW -") for line in deletes)
    assert not any(line.split()[2].isdigit() for line in deletes)


def test_the_jump_is_moved_to_the_top_and_deduplicated():
    foreign = "-A DOCKER-USER -s 203.0.113.0/24 -j DROP"
    moved = FamilyState(True, True, list(WANT), [foreign, HOOK_RULE])
    assert plan_family(moved, WANT).hook == "move"
    twice = FamilyState(True, True, list(WANT), [HOOK_RULE, foreign, HOOK_RULE])
    plan = plan_family(twice, WANT)
    assert plan.hook == "dedupe"
    lines = restore_script(twice, plan).splitlines()
    assert lines.count("-D DOCKER-USER -j GSS-ALLOW") == 2 and lines[-2] == "-I DOCKER-USER 1 -j GSS-ALLOW"
    assert foreign not in "\n".join(lines).replace("-A", "-D"), "a foreign rule is never named"


def test_the_plan_id_follows_the_desired_state_and_the_rules():
    d1 = parse_desired({"allows": [_allow("a", 2456, "udp")]})
    d2 = parse_desired({"allows": [_allow("a", 2457, "udp")]})
    s1 = {"ipv4": FamilyState(True, True, list(WANT), [HOOK_RULE])}
    s2 = {"ipv4": FamilyState(True, True, [], [HOOK_RULE])}
    assert plan_id(d1, s1) == plan_id(d1, s1)
    assert plan_id(d1, s1) != plan_id(d2, s1)
    assert plan_id(d1, s1) != plan_id(d1, s2)


# ------------------------------------------------------------------ backend choice


def test_auto_picks_the_backend_that_has_docker_user():
    fake = FakeIptables(backend="nft")
    assert iptables.detect("auto", fake, fake.reader).name == "nft"
    fake = FakeIptables(backend="legacy")
    fake.legacy_names = "filter\n"
    assert iptables.detect("auto", fake, fake.reader).name == "legacy"


def test_legacy_is_never_asked_when_the_kernel_has_no_legacy_table():
    """Asking iptables-legacy would make the kernel load the legacy table module on the host."""
    fake = FakeIptables(backend="nft")
    iptables.detect("auto", fake, fake.reader)
    with pytest.raises(iptables.BackendError, match="no legacy iptables filter table"):
        iptables.detect("legacy", fake, fake.reader)
    assert not any("legacy" in c[0] for c in fake.calls)


def test_no_docker_user_anywhere_is_a_clear_error():
    fake = FakeIptables(families=())
    with pytest.raises(iptables.BackendError, match="No DOCKER-USER chain found"):
        iptables.detect("auto", fake, fake.reader)


def test_both_backends_with_docker_user_must_be_chosen():
    def runner(cmd, stdin):
        return 0, "-N DOCKER-USER\n", ""

    with pytest.raises(iptables.BackendError, match="GSS_FIREWALL_BACKEND"):
        iptables.detect("auto", runner, lambda path: "filter\n")
    with pytest.raises(iptables.BackendError, match="auto, nft or legacy"):
        iptables.detect("ufw", runner, lambda path: "")


def test_missing_rights_and_the_nft_backend_of_docker_are_explained():
    def denied(cmd, stdin):
        return 4, "", "iptables v1.8.11 (legacy): can't initialize iptables table `filter': Permission denied (you must be root)"

    with pytest.raises(iptables.BackendError, match="NET_RAW"):
        iptables.detect("legacy", denied, lambda path: "filter\n")

    def incompatible(cmd, stdin):
        return 1, "", "iptables v1.8.11 (nf_tables): table `filter' is incompatible, use 'nft' tool."

    with pytest.raises(iptables.BackendError, match="nftables firewall backend"):
        iptables.detect("nft", incompatible, lambda path: "")


def test_legacy_waits_for_the_xtables_lock():
    assert iptables.Backend("legacy").tool("ipv4", restore=True) == ["iptables-legacy-restore", "-w", "5", "--noflush"]
    assert iptables.Backend("nft").tool("ipv6") == ["ip6tables-nft"]


# ------------------------------------------------------------------ helper: plan, apply, reconcile


@pytest.fixture()
def helper(tmp_path):
    fake = FakeIptables()
    fake.tables["ipv4"]["DOCKER-USER"] = ["-A DOCKER-USER -p tcp -m tcp --dport 22 -j ACCEPT",
                                         "-A DOCKER-USER -i eth0 -j DROP"]
    h = Helper("auto", (1024, 65535), str(tmp_path / "desired.json"), runner=fake, reader=fake.reader)
    return h, fake


def test_preview_writes_nothing_apply_writes_it_and_a_second_round_is_no_change(helper):
    h, fake = helper
    desired = {"allows": [_allow("mc-1", 25565, "tcp"), _allow("vh-1", 2456, "udp")], "sources": []}
    before = {f: dict(t) for f, t in fake.tables.items()}
    plan = h.plan(desired)
    assert fake.tables == before
    assert plan["changes"] == 8  # per family (IPv4 and IPv6): the chain, the jump, two rules
    assert {a["state"] for a in plan["allows"]} == {"opens"}
    report = h.apply(desired, plan["plan_id"])
    assert report["applied"] == plan["changes"]
    assert fake.tables["ipv4"]["DOCKER-USER"][0] == HOOK_RULE
    assert fake.tables["ipv4"]["DOCKER-USER"][1:] == ["-A DOCKER-USER -p tcp -m tcp --dport 22 -j ACCEPT",
                                                      "-A DOCKER-USER -i eth0 -j DROP"]
    assert len(fake.tables["ipv4"]["GSS-ALLOW"]) == 2
    again = h.plan(desired)
    assert again["changes"] == 0 and {a["state"] for a in again["allows"]} == {"open"}
    snapshot = repr(fake.tables)
    assert h.apply(desired, again["plan_id"])["applied"] == 0 and repr(fake.tables) == snapshot
    assert h.reconcile() == 0


def test_apply_refuses_a_plan_that_is_no_longer_current(helper):
    h, fake = helper
    desired = {"allows": [_allow("mc-1", 25565, "tcp")]}
    plan = h.plan(desired)
    fake.tables["ipv4"]["DOCKER-USER"].insert(0, "-A DOCKER-USER -s 203.0.113.0/24 -j DROP")
    with pytest.raises(PlanChanged):
        h.apply(desired, plan["plan_id"])
    assert "GSS-ALLOW" not in fake.tables["ipv4"]
    with pytest.raises(PlanChanged):
        h.apply({"allows": [_allow("mc-1", 25566, "tcp")]}, h.plan(desired)["plan_id"])


def test_closing_a_server_removes_its_rules_and_names_it(helper):
    h, fake = helper
    both = {"allows": [_allow("mc-1", 25565, "tcp"), _allow("vh-1", 2456, "udp")]}
    h.apply(both, h.plan(both)["plan_id"])
    one = {"allows": [_allow("mc-1", 25565, "tcp")]}
    plan = h.plan(one)
    assert [c["server"] for c in plan["closes"]] == ["vh-1"]
    h.apply(one, plan["plan_id"])
    assert fake.tables["ipv4"]["GSS-ALLOW"] == [
        "-A GSS-ALLOW -p tcp -m conntrack --ctstate DNAT --ctorigdstport 25565 --ctdir ORIGINAL -j ACCEPT"]


def test_reconcile_restores_the_applied_state_only(helper):
    h, fake = helper
    assert h.reconcile() == 0 and "GSS-ALLOW" not in fake.tables["ipv4"], "nothing applied, nothing done"
    desired = {"allows": [_allow("mc-1", 25565, "tcp")]}
    h.apply(desired, h.plan(desired)["plan_id"])
    fake.tables["ipv4"]["GSS-ALLOW"] = []
    assert h.reconcile() == 1 and len(fake.tables["ipv4"]["GSS-ALLOW"]) == 1
    assert oct(os.stat(h.state_path).st_mode & 0o777) == "0o600"


def test_ipv6_without_docker_user_is_left_alone(tmp_path):
    fake = FakeIptables(families=("ipv4",))
    h = Helper("auto", (1024, 65535), str(tmp_path / "d.json"), runner=fake, reader=fake.reader)
    desired = {"allows": [_allow("mc-1", 25565, "tcp")]}
    plan = h.plan(desired)
    assert plan["families"]["ipv6"]["status"] == "absent"
    h.apply(desired, plan["plan_id"])
    assert "GSS-ALLOW" not in fake.tables["ipv6"] and "GSS-ALLOW" in fake.tables["ipv4"]


def test_remove_takes_out_the_jump_and_the_chain_only(helper):
    h, fake = helper
    desired = {"allows": [_allow("mc-1", 25565, "tcp")]}
    h.apply(desired, h.plan(desired)["plan_id"])
    h.remove()
    assert "GSS-ALLOW" not in fake.tables["ipv4"]
    assert fake.tables["ipv4"]["DOCKER-USER"] == ["-A DOCKER-USER -p tcp -m tcp --dport 22 -j ACCEPT",
                                                  "-A DOCKER-USER -i eth0 -j DROP"]
    assert h.stored() is None
