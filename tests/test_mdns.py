import pytest

from spoolsteward.mdns import MAX_NAME_BYTES, SERVICE, UNIVERSAL, build_services, instance_name, txt_for

Q = {"name": "HP7305", "info": "Office printer", "model": "HP Smart Tank", "urf": ["CP1", "W8"],
     "formats": ["application/pdf", "image/urf", "application/postscript"], "color": True, "duplex": True,
     "uuid": "urn:uuid:abc", "location": "1st floor"}


def test_txt_has_the_keys_clients_need():
    t = txt_for(Q, "printers.lan", 8631, "https://printers.lan:8631/me")
    assert t["rp"] == "printers/HP7305" and t["TLS"] == "1.2" and t["air"] == "username,password"
    assert t["URF"] == "CP1,W8" and t["Color"] == "T" and t["Duplex"] == "T" and t["UUID"] == "abc"
    assert t["pdl"] == "application/pdf,image/urf"  # only formats the queue really accepts
    assert all(len(v) <= 255 for v in t.values())


def test_txt_without_urf_or_formats_is_still_valid():
    t = txt_for({"name": "q"}, "h", 1, "https://h/me")
    assert t["URF"] == "none" and t["pdl"].startswith("application/pdf") and t["Color"] == "F"


def test_each_queue_is_published_under_plain_and_airprint_type():
    out = build_services([Q], "printers.lan", ["192.168.1.5"], 8631, "https://x/me")
    (name, infos), = out.items()
    assert name == "Office printer @ printers"
    assert [i.type for i in infos] == [SERVICE, UNIVERSAL]
    assert all(i.port == 8631 and i.name == f"{name}.{SERVICE}" for i in infos)


def test_srv_target_is_a_dot_local_name_so_android_can_resolve_it():
    (infos,) = build_services([Q], "printers.lan", ["192.168.1.5"], 8631, "https://x/me").values()
    assert all(i.server == "printers.local." for i in infos)


LONG_INFO = "HP Smart Tank 7300 series [A7E76E] ufficio amministrazione piano terra"


def test_a_long_description_still_fits_in_one_dns_label():
    """The name of a service is one DNS label (63 bytes). A longer one made zeroconf refuse it, which stopped the announcer."""
    out = build_services([{**Q, "info": LONG_INFO}], "raspberrypi-printserver.example.lan", ["192.168.1.5"], 8631, "https://x/me")
    (name, infos), = out.items()
    assert len(name.encode()) <= MAX_NAME_BYTES and name.endswith(" @ raspberrypi-printser")
    assert [i.type for i in infos] == [SERVICE, UNIVERSAL]


@pytest.mark.parametrize("label", ["x" * 100, "è" * 100, "日本語" * 40, "😀" * 30])
def test_the_limit_counts_bytes_not_characters(label):
    name = instance_name(label, "gateway.lan")
    assert len(name.encode()) <= MAX_NAME_BYTES and name.endswith(" @ gateway")
    name.encode().decode()       # never cut in the middle of a character


def test_two_queues_with_the_same_long_start_get_two_names():
    qs = [{**Q, "name": "a", "info": LONG_INFO + " A"}, {**Q, "name": "b", "info": LONG_INFO + " B"}]
    out = build_services(qs, "gw.lan", ["192.168.1.5"], 8631, "https://x/me")
    assert len(out) == 2 and all(len(n.encode()) <= MAX_NAME_BYTES for n in out)


def test_a_queue_that_can_not_be_announced_does_not_take_the_others_down(caplog):
    out = build_services([{"info": "no name"}, Q], "gw.lan", ["192.168.1.5"], 8631, "https://x/me")
    assert list(out) == ["Office printer @ gw"]
    assert "is not announced" in caplog.text


def test_short_names_are_unchanged():
    assert instance_name("Office printer", "printers.lan") == "Office printer @ printers"
    assert instance_name("Scan 1", "gw.example.org", " (scanner)") == "Scan 1 (scanner) @ gw"
