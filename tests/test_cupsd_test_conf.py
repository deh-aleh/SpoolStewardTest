"""The CUPS of the tests runs the production image with docker/test/cupsd.conf. That file must be the production
docker/stack/cups/cupsd.conf plus the few lines a test on the host needs (a TCP listener): when someone changes the
production file and not the test one, a test would run against a CUPS that does not behave like the real one."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _test_conf_from(prod: str) -> str:
    out = []
    for line in prod.split("\n"):
        if line.startswith("# Private backend: unix socket only. Port 631"):
            out.append("# TEST ONLY: also listen on TCP (the docker port mapping reaches it from the host); the unix socket stays as in production.")
        elif line == "Listen /run/cups/cups.sock":
            out += ["Listen 0.0.0.0:631", line, "ServerAlias *  # TEST ONLY (docker port mapping)"]
        elif line == "  Order allow,deny":
            out += [line, "  Allow all  # TEST ONLY: host reaches us through the docker bridge"]
        else:
            out.append(line)
    return "\n".join(out)




def test_the_cups_of_the_tests_has_the_production_settings():
    prod = (ROOT / "docker" / "stack" / "cups" / "cupsd.conf").read_text()
    test = (ROOT / "docker" / "test" / "cupsd.conf").read_text()
    assert test == _test_conf_from(prod), (
        "docker/test/cupsd.conf is out of date: regenerate it from docker/stack/cups/cupsd.conf "
        "(production settings + the TEST ONLY lines)")
