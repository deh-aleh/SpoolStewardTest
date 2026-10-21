# One image for the whole stack: the application (portal and mDNS announcer), CUPS and ipp-usb.
# docker-compose.yml starts it several times with a different command: the services share the same files and always
# have the same version. Default command: the application. CUPS and ipp-usb need root, so their services say `user: "0:0"`.
# Debian 13 "trixie": the tag follows the security updates of Python 3.12 and of Debian, so a rebuild picks them up.
FROM python:3.12-slim-trixie
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    cups cups-client cups-ipp-utils cups-filters printer-driver-cups-pdf ghostscript poppler-utils \
    printer-driver-brlaser printer-driver-gutenprint printer-driver-splix printer-driver-foo2zjs \
    ipp-usb usbutils curl \
 && rm -rf /var/lib/apt/lists/*
# The image does not know the host user: docker-compose.yml starts the gateway with `user: PUID:PGID`, so the bind-mounted
# TLS key (mode 600) is readable. /data is world-writable (sticky) so a new volume works for whichever user that is.
RUN groupadd -g 1000 spoolsteward && useradd -u 1000 -g 1000 -M -s /usr/sbin/nologin spoolsteward \
 && mkdir /data && chmod 1777 /data
# the start scripts of the two backends (docker-compose.yml: `command:` of the services cups and ipp-usb)
COPY docker/stack/cups/entrypoint.sh /usr/local/lib/spoolsteward/cups.sh
COPY docker/stack/ipp-usb/entrypoint.sh /usr/local/lib/spoolsteward/ipp-usb.sh
COPY docker/stack/ipp-usb/ipp-usb.conf /etc/ipp-usb/ipp-usb.conf
RUN chmod +x /usr/local/lib/spoolsteward/*.sh
WORKDIR /src
# Python packages come only from the lock files (exact versions, checked against their SHA-256): two builds of the same
# source get the same packages. requirements/build.txt is pip and setuptools; setuptools only installs the project and
# is removed after. pip check fails the build when pyproject.toml asks for a package the lock lacks (lock not regenerated).
COPY requirements/runtime.txt requirements/build.txt ./requirements/
RUN pip install --no-cache-dir --require-hashes -r requirements/build.txt \
 && pip install --no-cache-dir --require-hashes -r requirements/runtime.txt
COPY pyproject.toml LICENSE NOTICE ./
COPY spoolsteward ./spoolsteward
RUN pip install --no-cache-dir --no-deps --no-build-isolation . \
 && pip uninstall -y setuptools && pip check && cd / && rm -rf /src
# the documentation shown in the portal (/admin/docs), kept outside /src because that folder is deleted above
COPY docs /opt/spoolsteward/docs
USER spoolsteward
ENV SPOOLSTEWARD_HOST=0.0.0.0 SPOOLSTEWARD_PORT=8631 \
    SPOOLSTEWARD_CUPS_URL=unix:///run/cups/cups.sock \
    SPOOLSTEWARD_SCAN_URL=http://ipp-usb:60000 \
    SPOOLSTEWARD_DATABASE_URL=sqlite:////data/spoolsteward.db \
    SPOOLSTEWARD_TLS_CERTFILE=/tls/fullchain.pem SPOOLSTEWARD_TLS_KEYFILE=/tls/privkey.pem
# no HEALTHCHECK and no VOLUME here: they would apply to CUPS and ipp-usb as well. The gateway's health check and
# its /data volume are in docker-compose.yml.
EXPOSE 8631
CMD ["spoolsteward"]
