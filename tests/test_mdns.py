from spoolsteward.mdns import SERVICE, UNIVERSAL, build_services, txt_for

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
