# Rezeptverwaltung

Eine private, vollständig deutschsprachige Webanwendung für einen gemeinsamen Rezeptbestand. Alle angemeldeten Benutzer sehen dieselben Rezepte, Kategorien, Bilder und Kommentare. Konten werden ausschließlich administrativ per CLI angelegt.

## Enthalten

- Rezept-CRUD mit Zutatengruppen, präzisen Dezimalmengen, Schritten, Zeiten, Quelle und Hinweisen
- optionale strukturierte Brenn- und Nährwerte pro Portion und/oder pro 100 g/ml
- portionsgenaue Skalierung im Browser und dieselbe getestete Logik im Backend
- klar getrennte Rezeptbereiche für Kochen und Backen mit gemeinsamer Standardansicht
- frei wachsende, hierarchische Kategorien ohne fest codierte Namen (maximal 20 je Rezept)
- zusätzliche globale Schlagwörter und pflegbare Suchsynonyme
- gewichtete PostgreSQL-Volltextsuche mit deutscher Konfiguration, `unaccent` und `pg_trgm`
- persönliche Favoriten
- nachvollziehbarer Rezeptverlauf mit Diff, Wiederherstellung und Konfliktschutz
- widerrufbare, optional ablaufende Einzelrezept-Freigabelinks ohne Konto- oder Originaldateidaten
- mehrere geschützte Bilder, Titelbild, Alt-Text, Bildunterschriften und unveränderte Originaldateien
- private Notizen pro Konto mit Autorensnapshot
- verlustarmes Rezeptpaket (`.rezept.json`), PDF-Ausgabe und Druckansicht
- persistente Importwarteschlange für Bilder, PDFs und URLs mit getrennten, auswählbaren KI-Rezeptentwürfen
- isolierter Playwright-Renderer mit SSRF-Prüfung für Webseitenimporte
- vollständige, versionierte Serverbackups mit SHA-256-Prüfsummen
- Restore-Preflight, Step-up-Passwort, Sicherheitsbackup und atomarer Medien-Switch
- installierbare PWA mit iOS-Safe-Areas und einem datenschutzfreundlichen Service Worker
- Caddy als einziger öffentlich erreichbarer Dienst

## Schnellstart

Voraussetzungen: Docker Engine mit Compose v2 und `make`.

```bash
cp .env.example .env
```

Ersetze in `.env` mindestens `APP_SECRET_KEY`, `RENDERER_TOKEN` und `POSTGRES_PASSWORD`. Sichere Zufallswerte erzeugst du beispielsweise mit `openssl rand -base64 48`.

```bash
make up
docker compose exec app python -m app.cli users create \
  --email admin@example.de \
  --display-name "Admin" \
  --role admin
```

Danach ist die Anwendung standardmäßig unter [http://localhost:8080](http://localhost:8080) erreichbar. Ohne `.env` startet der Stack bewusst nur mit Entwicklungswerten; für Produktion blockiert die Konfigurationsprüfung unsichere Werte.

Die wichtigsten Betriebsbefehle sind:

```bash
make up       # bauen, starten und auf gesunde Dienste warten
make restart  # Container neu erstellen, Konfiguration übernehmen, Bereitschaft abwarten
make update   # Sicherheitsbackup, Images aktualisieren, neu bauen und starten
make down     # Stack stoppen; persistente Volumes bleiben erhalten
```

`make update` aktualisiert zuerst externe Images und baut neue App-Images, während die laufende Version verfügbar bleibt. Vor dem Austausch erstellt es automatisch ein Backup, wenn die App bereits läuft. Für alle vier Befehle aktiviert `PRODUCTION=1` zusätzlich den TLS-Override, beispielsweise `make update PRODUCTION=1`. Das Health-Wait-Zeitlimit lässt sich bei Bedarf mit `WAIT_TIMEOUT=300` anpassen.

## Benutzerverwaltung

```bash
docker compose exec app python -m app.cli users create --email name@example.de --display-name "Name"
docker compose exec app python -m app.cli users create --email admin@example.de --display-name "Admin" --role admin
docker compose exec app python -m app.cli users list
docker compose exec app python -m app.cli users reset-password --email name@example.de
docker compose exec app python -m app.cli users set-role --email name@example.de --role admin
docker compose exec app python -m app.cli users deactivate --email name@example.de
```

Passwörter werden verdeckt interaktiv abgefragt. `--generate-password` erzeugt alternativ ein einmaliges, starkes Startpasswort. Rollenänderung, Passwortreset und Deaktivierung widerrufen sofort alle Sitzungen. Der letzte aktive Administrator kann nicht versehentlich entfernt werden.

## KI-Import

Die App startet und funktioniert vollständig ohne KI-Zugang. In diesem Zustand werden KI-Aufträge verständlich als nicht konfiguriert markiert. Enthält ein Bild, PDF oder eine gerenderte Webseite mehrere Rezepte, werden deren Quellbereiche zuerst getrennt erkannt und anschließend jeweils vollständig extrahiert. Die Importseite zeigt alle temporären Entwürfe vorausgewählt an; erst die bestätigte Auswahl erzeugt echte Rezepte. Originaldateien bestätigter Importe bleiben an den Rezepten erhalten. Nicht mehr referenzierte Quellen fehlgeschlagener, abgebrochener oder ohne Auswahl abgeschlossener Importe werden standardmäßig 30 Tage aufbewahrt und danach sicher bereinigt.

Gerichtbilder werden nicht allein aufgrund ihrer räumlichen Nähe übernommen. Jeder erkannte Bildausschnitt wird gegen Titel, Beschreibung und Zutaten des jeweiligen Rezeptentwurfs geprüft. Die Zuordnung wird anschließend dokumentweit aufgelöst, sodass derselbe Ausschnitt höchstens einem Rezept gehört. Eine optional erzeugte Bildvariante wird erneut geprüft; bei Unsicherheit verwendet die App den verifizierten Originalausschnitt oder importiert das Rezept ohne Bild.

Für einen OpenAI-kompatiblen Responses-Anbieter:

```dotenv
AI_API_KEY=...
AI_BASE_URL=https://provider.example/v1
AI_EXTRACTION_MODEL=modellname
AI_EXTRACTION_REASONING_EFFORT=high
AI_IMAGE_MODEL=bildmodellname
AI_IMAGE_QUALITY=high
AI_IMAGE_GENERATION_ENABLED=false
```

`AI_EXTRACTION_REASONING_EFFORT` steuert den Denkaufwand der Hauptanfrage an
die Responses API. Ohne Angabe verwendet die App `medium`. Die unterstützten
Werte hängen vom gewählten Modell ab; für `gpt-5.6-sol` sind `none`, `low`,
`medium`, `high`, `xhigh` und `max` verfügbar.

`AI_IMAGE_QUALITY` akzeptiert für GPT-Image-Modelle `auto`, `low`, `medium` oder
`high`. Ohne Angabe verwendet die App `auto`; für die höchste Qualitätsstufe setze
den Wert auf `high`. Die Einstellung gilt sowohl für neue Rezeptbilder als auch
für Bildbearbeitungen und die Bildaufbereitung beim Import.

Schlüssel gehören ausschließlich in `.env` beziehungsweise den Secret Store der Zielplattform. Sie werden nie an den Browser übertragen und nie exportiert. Bildgenerierung bleibt standardmäßig aus. Wenn sie aktiviert und ein Schlüssel konfiguriert ist, erscheint bei Rezepten ohne Bild die Aktion **Passendes KI-Bild erstellen**. Bei vorhandenen Bildern kann mit **Titelbild mit KI neu erstellen** eine neue Variante erzeugt werden. Dabei sendet der Worker das aktuelle Titelbild zusammen mit den Rezeptdaten an den konfigurierten Bilddienst und verwendet dessen Image-Edit-Schnittstelle. Erst nach erfolgreicher Verarbeitung wird die neue Variante zum Titelbild; das bisherige Bild bleibt zum Vergleichen oder Wiederherstellen in der Galerie. Der dauerhafte Auftrag bleibt über Seitenaktualisierungen und Worker-Neustarts hinweg erhalten. Beim Import wird die Bildaufbereitung nur für einen zuvor rezeptbezogen verifizierten Bildausschnitt aufgerufen.

Die Extraktion verwendet ein strikt validiertes JSON-Schema mit verpflichtenden Feldern und `additionalProperties: false`. Provider müssen die Structured-Outputs-Semantik der Responses API unterstützen; Details beschreibt die [offizielle OpenAI-Dokumentation](https://developers.openai.com/api/docs/guides/structured-outputs).

Der separate `renderer`-Container besitzt weder Datenbank- noch Redis-Zugang und keinen direkten Internetpfad. Er akzeptiert nur authentifizierte interne Aufträge und zwingt sämtliche Chromium-Anfragen einschließlich Loopback und Link-Local durch den isolierten `egress`-Proxy. Der Renderer prüft URL-Form, Standardports, Request- und Redirect-Budgets; der Egress-Proxy löst jedes Ziel unmittelbar beim Verbindungsaufbau auf, lehnt bereits bei einer einzigen nicht öffentlichen Antwort die gesamte Verbindung ab und verbindet anschließend direkt mit der geprüften IP. Ohne konfigurierten Sicherheitsproxy verweigert der Renderer den Betrieb. Eine Host-Firewall, die den Egress zusätzlich auf öffentliche Zielnetze beschränkt, bleibt sinnvolle Defense-in-Depth.

## Medienquoten und Exportgrenzen

Persistente Medien werden transaktional gegen konfigurierbare Grenzen je Rezept, Benutzer und Server geprüft. Auch noch aufbewahrte Quellen terminaler Importaufträge sowie generierte Bilder und Vorschaubilder zählen gegen Benutzer- und Serverquoten. `STORAGE_MIN_FREE_MB` hält zusätzlich eine freie Plattenreserve; bei Unterschreitung werden neue Medien mit HTTP 507 abgewiesen.

Die Standardwerte stehen in `.env.example` unter `MEDIA_*`, `STORAGE_MIN_FREE_MB` und `IMPORT_SOURCE_RETENTION_HOURS`. JSON-Rezeptpakete werden vor dem ersten Dateizugriff gegen `RECIPE_JSON_EXPORT_MAX_ASSETS` und `RECIPE_JSON_EXPORT_MAX_MB` geprüft; `RECIPE_JSON_EXPORT_CONCURRENCY` begrenzt parallele Exporte. Passe die Werte an die Größe des persistenten Volumes und die Zahl der Mitglieder an, ohne Benutzer- oder Servergrenzen kleiner als die jeweilige untergeordnete Grenze zu setzen.

## Backup und Wiederherstellung

Administratoren bedienen Backup und Restore unter **Einstellungen**. Alternativ für automatisierte Backups:

```bash
docker compose exec -T app python -m app.cli backups create
docker compose exec -T app python -m app.cli backups verify /data/backup-temp/datei.zip
```

Ein Cronjob auf dem Docker-Host kann `backups create` täglich ausführen und die erzeugte Datei anschließend verschlüsselt in einen getrennten Speicher kopieren. Empfohlen sind mindestens 7 tägliche, 4 wöchentliche und 12 monatliche Generationen. Prüfe regelmäßig eine Wiederherstellung in einer isolierten Testinstanz.

Backups enthalten Passwort-Hashes und private Originaldokumente. Sie enthalten ausdrücklich keine `.env`, API-Schlüssel, Sitzungsgeheimnisse, Redis-Zustände, Logs oder ältere Backup-Archive. Behandle sie wie hochsensible Daten.

Der Restore:

1. validiert Struktur, Pfade, Symlinks, Größen, Kompressionsverhältnis, Versionen und jede Prüfsumme;
2. zeigt vor Änderungen die Objekt- und Dateizahlen;
3. verlangt erneut das Admin-Passwort und exakt `WIEDERHERSTELLEN`;
4. erstellt ein Sicherheitsbackup;
5. schreibt Medien in eine neue Speichergeneration;
6. ersetzt Daten transaktional und schaltet den Medienspeicher atomar um;
7. invalidiert alle Sitzungen.

Ein Restore-Journal ermöglicht die automatische Erholung nach einem Prozessabbruch zwischen Datenbank-Commit und Dateisystem-Switch.

Das aktuelle Datenbankschema im Backup ist `0012`. Archive der Schemaversionen `0001` bis `0011` werden beim Preflight deterministisch in das aktuelle Datenmodell migriert; neuere, unbekannte Versionen werden vor jeder Änderung abgelehnt. Dieselben normalisierten Daten, die der Preflight geprüft hat, werden anschließend für den Restore verwendet.

## PWA installieren

- **iPhone/iPad:** Seite in Safari öffnen, **Teilen** wählen und **Zum Home-Bildschirm** antippen. Die App zeigt diese Anleitung bei Bedarf auch direkt an.
- **Chrome/Edge:** Den angebotenen Installieren-Button in der App oder das Installationssymbol des Browsers verwenden.

Private Rezeptseiten, API-Antworten, Login, Bilder, Originaldateien und Backups werden nie im Service-Worker-Cache abgelegt. Nur eine feste Allowlist versionierter App-Assets und die neutrale Offline-Seite steht offline bereit.

## Produktion mit TLS

1. Setze mindestens:

   ```dotenv
   APP_ENV=production
   APP_BASE_URL=https://rezepte.example.de
   APP_DOMAIN=rezepte.example.de
   ALLOWED_HOSTS=rezepte.example.de
   SESSION_COOKIE_SECURE=true
   FORCE_HTTPS=true
   PUBLIC_PORT=443
   APP_SECRET_KEY=<starker Zufallswert>
   RENDERER_TOKEN=<separater starker Zufallswert>
   POSTGRES_PASSWORD=<starkes Datenbankpasswort>
   ```

2. Starte mit dem Produktions-Override:

   ```bash
   make up PRODUCTION=1
   ```

Nur Caddy veröffentlicht Port 443. App, Worker, Renderer, PostgreSQL und Redis bleiben in getrennten internen Netzen. Caddy verwaltet Zertifikate in einem persistenten Volume. Falls dein DNS-/Firewall-Setup die TLS-ALPN-Challenge auf 443 nicht unterstützt, stelle Zertifikate außerhalb des Stacks bereit und mounte sie in Caddy; veröffentliche nicht nebenbei interne Dienste.

### Produktionscheckliste

- Secrets aus einem Secret Store injizieren; keine `.env` in Images oder Backups
- Datenbank- und Medienvolumes verschlüsseln und extern sichern
- Upstream-Basisimages nur über die geprüften, unveränderlichen Digests aus den Docker- und Compose-Dateien beziehen
- Reverse-Proxy-Logs zentral erfassen, private Inhalte und insbesondere `/freigabe/`-Tokens ausfiltern
- `/health/live` und `/health/ready`, Worker-Queue sowie Plattenplatz überwachen
- Alarmierung für fehlgeschlagene Importe, Backups und Restores einrichten
- Backup-Retention und Ablaufdateien regelmäßig bereinigen
- `RECIPE_VERSION_RETENTION` passend zur gewünschten Verlaufstiefe und Datenmenge festlegen
- Dependency- und Image-Scans im Releaseprozess ausführen
- reale iPhone-/iPad-Installation, Kameraimport und Safe Areas pro Release prüfen
- Restore mindestens quartalsweise in einer isolierten Umgebung testen

## Entwicklung und Tests

Voraussetzungen für die lokale Entwicklung sind Python 3.12 sowie eine von Vite unterstützte Node.js-Version (Node.js 24 LTS empfohlen). Vor dem Start erzeugt der Frontend-Build inhaltsbasierte Dateinamen, das Asset-Manifest und den Service Worker. Die Anwendung startet absichtlich nicht mit fehlenden oder inkonsistenten Build-Artefakten.

```bash
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-dev.lock
npm ci --ignore-scripts
npm run build
ruff check .
ruff format --check .
python -m pytest
```

Nach Änderungen unter `app/static/css`, `app/static/js`, `app/static/pwa`, am Service Worker oder an der Offline-Seite muss `npm run build` erneut ausgeführt werden. Alternativ installiert `make assets` die exakt gesperrten Node-Abhängigkeiten und baut alle Assets. Vite schreibt ausschließlich gehashte Dateien nach `app/static/dist`; Jinja löst die logischen Namen über das generierte Manifest auf. Diese Dateien erhalten ein Jahr `immutable`, während HTML und der Service Worker weiterhin revalidiert werden.

Für Integrationstests müssen `DATABASE_URL` und `REDIS_URL` auf eine isolierte Testinstanz zeigen. Der CI-Workflow startet PostgreSQL und Redis automatisch und führt Migrationen, Typprüfung, Tests, Coverage, Dependency-Audit und Compose-Validierung aus. `requirements.lock` und `requirements-dev.lock` fixieren sämtliche Versionen und Paketprüfsummen; nach Änderungen an `pyproject.toml` werden sie mit `pip-compile --generate-hashes` neu erzeugt.

Ein vollständiger lokaler Stacktest:

```bash
make up
docker compose ps
curl --fail http://localhost:8080/health/ready
```

## Architektur

```text
Browser / installierte PWA
            │ ein öffentlicher Port
          Caddy
            │
         FastAPI ───── PostgreSQL
            │             │
            ├──── Redis / Dramatiq Worker
            │                    │
            │              persistente Medien
            │                    │
            └── interner Renderer│── öffentliche Webseiten
                  (kein DB-/Redis-Netz)
```

FastAPI rendert Jinja2-Seiten und stellt unter `/api/v1` die JSON-API bereit. Frontend, API, Manifest, Service Worker und geschützte Dateien verwenden dieselbe Origin; CORS ist nicht erforderlich.

## Datenschutz- und Sicherheitsgrenzen

- Alle Rezeptdaten sind für jedes aktive Mitglied dieses privaten Servers sichtbar.
- Medien werden nur nach Sitzungsprüfung ausgeliefert; Storage-Keys sind keine öffentlichen URLs.
- Schreibende API-Aufrufe benötigen einen CSRF-Token; ein vorhandener Origin-Header muss zur konfigurierten App-Origin passen.
- Der HTML-Login verwendet zusätzlich einen kurzlebigen anonymen Double-Submit-Token und prüft Origin beziehungsweise Referer.
- Sessions sind serverseitige, widerrufbare Datensätze; Cookies enthalten nur ein zufälliges Token. Ein Konto kann gleichzeitig auf mehreren Geräten angemeldet sein. Eine normale Abmeldung beendet nur die aktuelle Gerätesitzung, während Passwortreset, Rollenwechsel, Deaktivierung und Restore weiterhin alle Sitzungen widerrufen.
- Kommentare und importierte Texte werden ausschließlich escaped gerendert.
- Uploads werden anhand von Magic Bytes sowie Bild-/PDF-Grenzen geprüft.
- Datei-Uploads prüfen Sitzung, CSRF und gegebenenfalls Adminrechte vor dem Multipart-Parsing. Ein Byte-Limit gilt auch ohne Content-Length und für unbekannte Dateifelder. Maximal zwei Uploads dürfen gleichzeitig parsen und verarbeitet werden; diese Grenze gilt für Prozesse mit gemeinsamem temporärem Verzeichnis. Weitere Uploads erhalten HTTP 503 und können wiederholt werden.
- Das temporäre Dateisystem reserviert pro Upload dessen Content-Length, andernfalls das Byte-Limit der jeweiligen Upload-Route. Die Summe aktiver Reservierungen und `STORAGE_MIN_FREE_MB` muss in den freien Platz passen; die App erzwingt auch eine angegebene Content-Length beim Lesen. Bei Platzmangel antwortet sie mit HTTP 507. Die normalen Speicherquoten gelten zusätzlich. Formularrouten ohne Datei-Upload sind auf 64 KiB begrenzt; Datei-Uploads erlauben bis zu 20 Dateifelder und 32 Textfelder mit jeweils höchstens 64 KiB.
- Suchsynonyme dürfen höchstens 64 Varianten, 4096 Zeichen pro Variante und insgesamt 32768 Zeichen erzeugen; übergroße Expansionen werden mit HTTP 422 abgewiesen. PDF-Seiten werden vor der Rasterung auf maximal 8192 Pixel pro Achse und 16 Millionen Pixel geprüft; zu große Seiten werden als ungültige Quellregion abgewiesen.
- Der Service Worker cached keine HTML-Rezeptseiten, API-, Auth-, Kommentar-, Backup- oder Mediendaten.
- Restore-Archive werden gegen Zip Slip, Symlinks, Kollisionen und Zip-Bombs geprüft.
- Es gibt keine öffentliche Registrierung. Mitglieder können für ein aktives Einzelrezept einen zufälligen, widerrufbaren und optional ablaufenden Freigabelink erzeugen. Löschen oder Archivieren widerruft alle zugehörigen Links dauerhaft.
- Die öffentliche Freigabe zeigt ausschließlich Rezeptinhalt und Rezeptbilder. Kontodaten, Kommentare, Sitzungen und hochgeladene Originaldateien bleiben geschützt; vollständige Linktoken werden nur beim Erstellen angezeigt und serverseitig ausschließlich gehasht gespeichert.
- Der App-Server schreibt bewusst keine rohen HTTP-Zugriffspfade, damit Freigabetokens nicht in Container-Logs landen. Werden Proxy-Access-Logs ergänzt, müssen `/freigabe/`-Pfade redigiert oder ausgeschlossen bleiben.

## Lizenz

Der eigene Quellcode dieses Projekts steht unter der [MIT-Lizenz](LICENSE).
Abhängigkeiten bleiben unter ihren jeweiligen Lizenzen; die Übersicht und die
besonderen Hinweise zu nativen Bibliotheken stehen in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Dieses Repository veröffentlicht Quellcode und Bauanleitungen. Es enthält keine
privaten Rezeptdaten, Kochbücher, Zugangsdaten oder mitgelieferten Container-Images.
Wer Images oder andere Binärpakete weitergibt, muss die darin enthaltenen
Drittkomponenten einschließlich Lizenztexten und gegebenenfalls zugehörigem
Quellcode gesondert berücksichtigen. Die MIT-Lizenz des eigenen Codes ersetzt
diese Pflichten nicht.

Importierte Rezepte, Bilder, PDFs und Webseiten behalten ihre jeweiligen Rechte.
Die Software erteilt keine Erlaubnis, fremde Inhalte zu kopieren, an einen
KI-Anbieter zu übertragen oder über Freigabelinks weiterzugeben. Betreiber und
Nutzer müssen dafür die erforderlichen Rechte besitzen und die Bedingungen der
jeweiligen Quellen und Dienste beachten.
