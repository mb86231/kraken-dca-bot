# Installation & Betrieb — Schritt für Schritt

Komplette Walkthrough von der leeren Maschine bis zum laufenden Bot, mit
drei Betriebsarten. Alle Befehle gelten für das Docker-Quick-Start-Setup
(`compose.public.yaml`, Prebuilt-Image von ghcr.io).

**Inhalt**

- [Teil A — Gemeinsame Basis (alle Betriebsarten)](#teil-a--gemeinsame-basis)
- [Betrieb 1 — Localhost-only (Default, sicherste Variante)](#betrieb-1--localhost-only-default)
- [Betrieb 2 — Zugriff aus dem Netz per IP (Plain HTTP, vertrautes LAN)](#betrieb-2--zugriff-aus-dem-netz-per-ip)
- [Betrieb 3 — Hinter einem Reverse Proxy mit TLS (empfohlen für Fernzugriff)](#betrieb-3--hinter-einem-reverse-proxy-mit-tls)
- [Teil B — Ersteinrichtung im Dashboard (alle Betriebsarten)](#teil-b--ersteinrichtung-im-dashboard)
- [Telegram (optional)](#telegram-optional)
- [Updates](#updates)
- [Troubleshooting](#troubleshooting)

---

## Teil A — Gemeinsame Basis

Gilt für alle drei Betriebsarten. Wähle danach **eine** der Betriebe 1–3.

### A1 — Voraussetzungen

- Linux-Host (oder jeder Docker-fähige Rechner) mit Docker + Compose-Plugin
- Ein Kraken-Account (für den Bot selbst)

### A2 — Verzeichnis und Compose-Datei

```bash
mkdir -p ~/kraken-dca-bot && cd ~/kraken-dca-bot
curl -O https://raw.githubusercontent.com/mb86231/kraken-dca-bot/main/compose.public.yaml
```

### A3 — `.env` nach Betriebsart anlegen

Die `.env` steuert die Netz-Bindung. Details pro Variante siehe unten — für
Betrieb 1 reicht eine leere oder keine `.env`.

### A4 — Starten und Setup-Token holen

```bash
docker compose -f compose.public.yaml up -d
docker compose -f compose.public.yaml logs -f
```

Nach dem Start erscheint in den Logs eine Zeile mit dem
**FIRST-RUN SETUP TOKEN** — notieren, dann `Strg+C`.

### A5 — Verifizieren

```bash
docker compose -f compose.public.yaml ps
# Erwartung: Status "healthy"

docker compose -f compose.public.yaml config | grep -A3 ports:
# Erwartung: genau EIN ports-Eintrag (Inhalt je nach Betriebsart)
```

Wichtig: **genau ein Binding.** Zwei Einträge für denselben Host-Port
(z. B. `0.0.0.0:8000` *und* `127.0.0.1:8000`) lassen den Container mit
einem irreführenden `address already in use` startfehlschlagen — typische
Ursache ist ein `compose.override.yaml` (Compose mergt `ports`-Listen, statt
sie zu ersetzen). Siehe [Troubleshooting](#troubleshooting).

---

## Betrieb 1 — Localhost-only (Default)

Die sicherste Variante: Das Dashboard ist **nur auf dem Rechner selbst**
erreichbar. Kein `.env`-Eintrag nötig — `compose.public.yaml` bindet
standardmäßig auf `127.0.0.1`.

```bash
# keine .env nötig; bei vorhandener Datei sicherstellen:
# (kein DCA_BOT_BIND gesetzt)
docker compose -f compose.public.yaml up -d
```

Erwartete Ausgabe von `docker compose config | grep -A3 ports:`:

```yaml
    ports:
      - mode: ingress
        ...
        published: "8000"
        host_ip: 127.0.0.1
```

**Zugriff:** nur lokal, `http://localhost:8000`.

**Von außen (temporär):** SSH-Tunnel statt Portöffnung:

```bash
ssh -L 8000:127.0.0.1:8000 user@bot-host
# dann lokal http://localhost:8000 öffnen
```

Danach weiter mit [Teil B](#teil-b--ersteinrichtung-im-dashboard).

---

## Betrieb 2 — Zugriff aus dem Netz per IP

Für **vertraute Netze** (Heim-LAN, VLAN mit ausschließlich vertrauten
Geräten): Der Container lauscht auf allen Schnittstellen, du greifst direkt
über `http://<host-ip>:8000` zu. **Achtung:** unverschlüsseltes HTTP —
jeder im Netz kann den Traffic mitlesen (inkl. Login-Passwort beim ersten
Setup und bei jeder Anmeldung). Nur verwenden, wenn das Netz vertrauenswürdig
ist.

```bash
cd ~/kraken-dca-bot
printf 'DCA_BOT_BIND=0.0.0.0\nWEB_UI_SECURE_COOKIE=false\n' > .env
docker compose -f compose.public.yaml up -d
```

Erwartete Ausgabe von `docker compose config | grep -A3 ports:`:

```yaml
    ports:
      - mode: ingress
        ...
        published: "8000"
        # kein host_ip → bindet 0.0.0.0
```

**Zugriff:** `http://<host-ip>:8000` von jedem Gerät im Netz.

Danach weiter mit [Teil B](#teil-b--ersteinrichtung-im-dashboard).

---

## Betrieb 3 — Hinter einem Reverse Proxy mit TLS

**Empfohlene Variante für Fernzugriff** (auch übers Internet): Ein
Reverse Proxy (Nginx Proxy Manager, Caddy, Traefik, nginx) terminiert TLS,
der Container bleibt mit Plain HTTP im LAN erreichbar.

### Schritt 1 — `.env` auf dem Bot-Host

```bash
cd ~/kraken-dca-bot
printf 'DCA_BOT_BIND=0.0.0.0\nWEB_UI_SECURE_COOKIE=true\n' > .env
docker compose -f compose.public.yaml up -d
```

- `DCA_BOT_BIND=0.0.0.0` — der Proxy auf einer anderen Maschine muss den
  Container über dessen IP erreichen können.
- `WEB_UI_SECURE_COOKIE=true` — Session-Cookies werden als `Secure`
  markiert. **Folge:** Login funktioniert nur noch über die HTTPS-Adresse
  des Proxys, nicht mehr über `http://<ip>:8000`. Das ist gewollt.

### Schritt 2 — Proxy-Host beim Reverse Proxy anlegen

Am Beispiel **Nginx Proxy Manager** (analog für andere):

1. **Hosts → Proxy Hosts → Add Proxy Host**
2. Domain: `bot.example.com`
3. Scheme: `http`, Forward Hostname: **IP des Bot-Hosts**, Forward Port: **8000**
4. Tab **SSL**: Zertifikat wählen (z. B. Let's Encrypt), **Force SSL** an
5. **Websockets Support**: an (für Live-Ansichten im Browser)
6. Save

Generisches nginx-Beispiel: [`docs/WEB_DASHBOARD.md`](WEB_DASHBOARD.md)
→ *Reverse Proxy / HTTPS*.

### Schritt 3 — Verifizieren

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://<bot-host-ip>:8000/login
# Erwartung: 200 (der Proxy erreicht den Container)

curl -s -o /dev/null -w "%{http_code}\n" https://bot.example.com/login
# Erwartung: 200 (TLS-Strecke komplett)
```

Danach weiter mit [Teil B](#teil-b--ersteinrichtung-im-dashboard) — das
Setup **immer über die HTTPS-Adresse** des Proxys durchführen (Secure
Cookie), nicht über die IP.

---

## Teil B — Ersteinrichtung im Dashboard

Gilt für alle Betriebsarten. Aufruf je nach Variante: `http://localhost:8000`
(1), `http://<ip>:8000` (2) oder `https://bot.example.com` (3).

### B1 — Admin-Account anlegen

Beim ersten Aufruf ist die Login-Seite ein **Setup-Formular**: Setup-Token
(aus den Logs, Teil A4) + gewünschter Username + Passwort (mind. 8 Zeichen).
Danach bist du eingeloggt.

### B2 — Kraken-API-Key eintragen

1. Kraken → **Settings → API → Create Key**: nur die Berechtigungen
   **Query Funds** und **Create & Modify Orders** aktivieren —
   **niemals** Withdraw Funds oder Admin/Transfer.
2. Dashboard → **Settings → API Keys** → Key + Secret eintragen, Save.
3. Oben rechts wird der Badge **Kraken API** grün.

### B3 — Strategie konfigurieren

**Settings → Strategy:** Trading Pair (z. B. `XBTEUR`), Betrag pro Kauf,
Deposit Day, Buy Hour. Optional **Dynamic DCA** aktivieren — dann skaliert
jeder Kauf automatisch mit dem Kurstrend (mehr beim Dip, weniger/bei 0 beim
Anstieg), Tier-Tabelle frei anpassbar. Details:
[`docs/configuration.md`](configuration.md).

### B4 — Preflight ausführen

Menü **Preflight → Run checks**. Alle automatischen Checks sollten grün
sein. Die zwei manuellen Kraken-Hinweise (Key-Berechtigungen lassen sich
nicht per API prüfen) einmal im Kraken-Account verifizieren und im
Preflight **acknowledgen**.

### B5 — Live-Trading aktivieren (bewusster Schritt)

Top-Bar: **Live: OFF** klicken und bestätigen. Solange der Schalter aus
ist, läuft der Bot im **Dry-Run**: Käufe werden berechnet, protokolliert und
auf dem Dashboard angezeigt, aber **nicht** bei Kraken platziert.

---

## Telegram (optional)

1. Bei [@BotFather](https://t.me/BotFather) `/newbot` → Token notieren.
2. Chat-ID ermitteln (z. B. [@userinfobot](https://t.me/userinfobot)).
3. Dashboard → **Settings → API Keys** → Telegram-Bereich ausfüllen.
4. Fertig — der Bot schickt Benachrichtigungen (Start, Käufe, Fehler) und
   beantwortet Befehle wie `/status`, `/price`, `/buy`, `/pause`, `/resume`.
   Sicherheitsmodell (Chat-ID-Allow-List, Einmal-Bestätigung für Kauf/Pause,
   Audit-Log): [`docs/TELEGRAM.md`](TELEGRAM.md).

---

## Updates

```bash
cd ~/kraken-dca-bot
curl -O https://raw.githubusercontent.com/mb86231/kraken-dca-bot/main/compose.public.yaml
docker compose -f compose.public.yaml pull
docker compose -f compose.public.yaml up -d
```

Die `.env` bleibt liegen und wird übernommen. Daten leben in den Named
Volumes `dca-bot-data`, `dca-bot-backups`, `dca-bot-logs` — ein Image-Update
berührt sie nicht.

---

## Troubleshooting

| Symptom | Ursache / Lösung |
|---|---|
| `failed to bind host port … address already in use`, aber `ss` zeigt nichts | Doppeltes Port-Binding durch `compose.override.yaml`. Compose mergt `ports`-Listen → zwei Bindings für denselben Host-Port. Fix: Override löschen, Bindung über `DCA_BOT_BIND` in `.env` setzen (Betrieb 2/3). |
| `PermissionError: /app/data/config.json` beim Start | Altes Image / alte Volumes mit root-Rechten. `docker compose pull` (Fix seit Oktober 2026 im Image), Volumes einmalig retten: `docker compose run --rm --user root --entrypoint chown dca-bot -R 1500:1500 /app/data /app/backups /app/logs` |
| `403 CSRF token invalid` beim Login/Setup | Seite wurde doppelt geladen (alter Stand). `docker compose pull && up -d`, dann einmal `Strg+Shift+R`. |
| Login schlägt fehl, aber über die IP geht's | `WEB_UI_SECURE_COOKIE=true` aktiv → Login nur noch über die HTTPS-Proxy-Adresse. Gewolltes Verhalten (Betrieb 3). |
| `502 Bad Gateway` am Proxy | Container lauscht noch auf `127.0.0.1` → `DCA_BOT_BIND=0.0.0.0` in `.env` setzen und `up -d` (Betrieb 3, Schritt 1). |
