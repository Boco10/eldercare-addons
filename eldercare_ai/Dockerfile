ARG BUILD_FROM
FROM ${BUILD_FROM}

# A base image már tartalmaz tzdata-t és s6-overlay-t.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /opt/eldercare

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY app/ /opt/eldercare/app/

# Az s6 run scriptek másolása az app UTÁN jöjjön.
# A chmod itt fut, nem a hoston — a Windows fájlrendszer nem tárolja a futtatható bitet.
COPY rootfs /
RUN chmod +x /etc/services.d/eldercare/run /etc/services.d/eldercare/finish

# A Supervisor watchdogja a konténer health állapotát nézi. Korábban a
# config.yaml `watchdog:` kulcsa végezte ezt, de az elavult — a natív Docker
# HEALTHCHECK a helyes megoldás, és ez az egyetlen, amit a linter is elfogad.
#
# A /health a konténeren BELÜLRŐL érkezik, ezért kivétel az Ingress IP-szűrő
# alól (lásd app/api/local_ui.py). A start-period azért 30 másodperc, mert az
# adatbázis és a mapping betöltése lassabb gépen eltart addig, és egy induláskori
# újraindítási hurok rosszabb, mint egy késve észlelt fagyás.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/health', timeout=4).status == 200 else 1)"

ARG BUILD_VERSION
LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="aarch64|amd64"
