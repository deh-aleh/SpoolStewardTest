# Come è fatta l'applicazione

[Indice della documentazione](../README.md) · [English](../en/application-internals.md) · Italiano

Visita guidata del repository: a cosa serve ogni file sorgente, come funziona la configurazione (`.env`,
`SPOOLSTEWARD_*`) e cosa fanno script e file dei contenitori. Per il quadro generale leggi prima [architettura.md](architettura.md).

## 1. Struttura del repository

```
spoolsteward/   l'applicazione (pacchetto Python)
  admin/        il portale web: rotte + template Jinja
  audit/        registro delle azioni amministrative e archivio dell'audit
  auth/         account, password, chiavi di stampa, RADIUS, protezione dei login
  backup/       motore dei backup (file, cifratura, scheduler)
  db/           modelli, sessioni, esecuzione delle migrazioni
  ipp/          codec IPP, reverse proxy, riscrittura degli URI
  locales/      traduzioni (.po) per it, es, ru, zh
  migrations/   revisioni Alembic
  policy/       quote, concessioni, conteggio pagine, controlli, stampa periodica, ordinamento
  certwatch.py  cli.py  config.py  crypto_fields.py  cups_client.py  escl.py  escl_window.py  i18n.py  keywatch.py  logsetup.py  main.py  mdns.py  notices.py  printerwatch.py  secretcheck.py  titles.py  updates.py
deploy/         installazione sull'host senza docker: non supportata
docker/         definizione dei contenitori (stack/ è quella di produzione)
docs/           questa documentazione (en/ e it/)
requirements/   dipendenze Python bloccate, con hash (CONTRIBUTING.md)
scripts/        script di supporto
tests/          test automatici
docker-compose.yml  docker-compose.test.yml  pyproject.toml  alembic.ini  CONTRIBUTING.md
```

## 2. Configurazione: chi legge cosa

C'è **un solo file** di impostazioni, `.env`, creato da `scripts/docker-init.sh` da `docker/env.example` (permessi 600). Lo leggono **docker compose**
(parte A: `PUID`, `PORTAL_PORT`, `CUPS_QUEUES`, le cartelle dell'host, la versione) e i contenitori **gateway** e **mdns** (`env_file: .env`; parte B: ogni
impostazione `SPOOLSTEWARD_*`, più le opzioni `CERT_SYNC_*` di `scripts/cert-sync.sh`). Ogni variabile è descritta in [configurazione.md](configurazione.md).

L'applicazione ([`spoolsteward/config.py`](../../spoolsteward/config.py)) legge **solo variabili d'ambiente vere** con prefisso `SPOOLSTEWARD_`, non un file
`.env`. Ogni impostazione ha un valore predefinito sicuro; una riga vuota è un errore (`none` dove l'impostazione può restare non impostata). **`docker-compose.yml`
non si modifica mai**, così un aggiornamento può sostituirlo senza cancellare le scelte di nessuno; un aggiornamento non tocca nemmeno `.env`. Un test verifica
che ogni valore attivo di `docker/env.example` sia uguale al predefinito di `config.py`.

## 3. Il pacchetto Python, file per file

### 3.1 Punti d'ingresso

| File | Ruolo |
|---|---|
| `spoolsteward/__main__.py` | Il comando `spoolsteward`: rifiuta di partire senza TLS, con RADIUS configurato a metà o con un `SPOOLSTEWARD_ESCL_WINDOW_SECONDS` inutilizzabile, poi avvia uvicorn. |
| `spoolsteward/main.py` | Costruisce l'app FastAPI: client verso CUPS (socket unix), database, provider di autenticazione, middleware, router, `/healthz`, attività di fondo. |
| `spoolsteward/cli.py` | Il comando `spoolsteward-admin`: `set-admin-password`, `group-limit`, `user-limit`, `set-group`, `make-admin`, `jobs`, `audit-archive`, `backup-key`, `backup-run`, `backup-verify`, `backup-restore`. |
| `spoolsteward/config.py` | `Settings` (pydantic-settings): ogni variabile `SPOOLSTEWARD_*` con valore predefinito e validazione. |
| `spoolsteward/cups_client.py` | La conversazione del gateway con CUPS: elenca le code nell'ordine di visualizzazione; `queue_caps()` chiede a una coda se ha colore e unità fronte-retro (in cache 60 s) per i controlli dei permessi. |
| `spoolsteward/mdns.py` | Processo a parte (`python -m spoolsteward.mdns`): annuncia le code come `_ipps._tcp` (+ sottotipo AirPrint) e gli scanner come `_uscans._tcp`. |
| `spoolsteward/escl_window.py` | La finestra di scansione: quale indirizzo può scansionare senza credenziali, per chi e per quanto tempo (in memoria). |
| `spoolsteward/escl.py` | Scansione: proxy eSCL per i client nativi (`/eSCL/...`) e pagina di scansione del portale (`/me/scan`); scopre gli scanner a runtime. |
| `spoolsteward/i18n.py` | Motore delle traduzioni: negoziazione della lingua, `_()`, plurali, middleware. Vedi [lingue.md](lingue.md). |

### 3.2 `spoolsteward/ipp/` — il lato IPP

| File | Ruolo |
|---|---|
| `codec.py` | Parser/serializzatore IPP (RFC 8010) senza perdita, collezioni annidate comprese. `decode_head` legge solo l'intestazione, così i documenti scorrono senza entrare in memoria. |
| `gateway.py` | Il reverse proxy: controllo HTTPS, autenticazione, filtro operazioni, identità imposta, permessi (rifiuta o riscrive `print-color-mode` / `sides` prima di CUPS, filtra gli elenchi di stampanti), riserva di quota, inoltro in streaming a CUPS, registrazione dell'esito. |
| `rewrite.py` | Riscrive gli URI interni di CUPS nelle risposte perché i client continuino a parlare col gateway; dichiara `uri-authentication-supported=basic` così Mopria invia le credenziali. |

### 3.3 `spoolsteward/auth/` — chi sei

| File | Ruolo |
|---|---|
| `service.py` | Decide chi può entrare dove: `portal_login` (ogni utente abilitato; la riga decide locale o delegato) e `print_login` (una chiave di stampa; la password RADIUS solo con `keys+radius`). |
| `users.py` | Ricerca/creazione utenti, gruppo predefinito, `is_emergency()`, eliminazione (storico conservato). |
| `groups.py` | I due gruppi di sistema `default` e `administrator`. |
| `bootstrap.py` | Crea `administrator` senza password e stampa il codice monouso `FIRST RUN`. |
| `passwords.py` | Hash argon2id e regole delle password (minimo 12 caratteri, diversa dal nome utente). |
| `keys.py` | Chiavi di stampa: creazione con durata scelta o monouso, hash sha256, stati, verifica, disattivazione, rinnovo, pulizia. |
| `radius.py` | Provider RADIUS (pyrad). `base.py`: l'interfaccia dei provider. `cache.py`: breve cache dei login riusciti. |
| `throttle.py` | Blocco dei login; tenuto in `/data/login-lockout.json`, quindi un riavvio non lo azzera. |
| `dictionary` | Dizionario degli attributi RADIUS richiesto da pyrad. |

### 3.4 `spoolsteward/policy/` — quote e controlli

| File | Ruolo |
|---|---|
| `engine.py` | Finestre (settimanali/mensili nel fuso configurato, anche con l'ora legale), regole effettive, consumo, concessioni sommate ai limiti, `check_job` → `Decision` (`reason` inglese per audit/IPP, `text()` localizzato per il portale). |
| `audit.py` | `reserve()` (atomico, sotto lock; con `deny=` registra un rifiuto deciso altrove, come un permesso, senza guardare la quota) e `finalize()` di un job; collega il documento a un job `Create-Job`. |
| `permissions.py` | Permessi avanzati: `permission_rule` di un gruppo o utente, `effective()` (il gruppo, poi l'utente, campo per campo; l'amministratore di emergenza è esente), gli orari (`in_window`) e `decide()`, la funzione pura che dice se un lavoro è consentito, rifiutato o corretto. Aggiunge le righe `permission_grant` attive dell'utente e avvisa quando un permesso temporaneo è finito. |
| `grants.py` | Richieste di quota, gruppo e permesso, approvazione (idempotente), concessioni dirette, revoca, scadenza delle richieste vecchie, eliminazione delle concessioni finite. |
| `rules.py` | Crea/sostituisce/rimuove i limiti di un utente o gruppo (condiviso da CLI e portale). |
| `pages.py` | Conta le pagine prima di stampare (PDF, JPEG/PNG/GIF, URF, PostScript, testo), interpreta gli intervalli di pagine e ruota in orizzontale le pagine di un PDF (`landscape_pdf`). |
| `reconcile.py` | Ciclo del riconciliatore con CUPS (pagine reali, job annullati, riserve orfane). |
| `control.py` | Manutenzione del gateway, blocco per stampante e nascondimento mDNS, token del canale mdns. |
| `keepalive.py` | Stampa periodica di manutenzione: impostazioni, scheduler, stampa di prova. |
| `testpage.py` | Costruisce i PDF che il gateway stampa da sé (nessuna dipendenza). |
| `ordering.py` | Ordine dell'elenco: prima le fisiche, poi le virtuali; ciascuna per prima volta vista. |
| `discovery.py` | Trova le stampanti USB nuove (prima ipp-usb, poi backend usb di CUPS), crea le loro code, rimuove quelle USB irraggiungibili dopo `OFFLINE_REMOVE_HOURS`; ricorda i nomi precedenti delle code (`printer_former`). |

### 3.5 `spoolsteward/admin/` — il portale

| File | Ruolo (prefisso URL) |
|---|---|
| `static/` | Stili, script (`css/app.css` e `js/app.js` per tutte le pagine, `css/<pagina>.css` e `js/<pagina>.js` per una pagina sola; nessun CSS o JavaScript in linea nei template) e icone (`icon.svg`, `icon-dark.svg` / `icon-light.svg` scelte in base al tema, `favicon.ico`, `apple-touch-icon.png`, `icon-512.png`). Serviti senza login su `/admin/static/`. |
| `routes.py` | Login/logout, configurazione del primo avvio, elenco audit ed esportazione CSV, annullamento job (`/admin/…`). |
| `accounts.py` | Utenti, gruppi, limiti, permessi avanzati (il blocco del modulo e i suoi controlli), chiavi degli utenti, registro delle azioni admin (`/admin/users`, `/groups`, `/actions`). |
| `requests.py` | Una pagina per richieste e concessioni: richieste in attesa, approvazione/rifiuto, concessioni attive, concessioni dirette, storico, eliminazione delle concessioni finite (`/admin/requests`). |
| `reports.py` | Report di utilizzo: chi ha usato di più in un periodo, pagina / CSV / PDF (`/admin/report`). |
| `docs.py` | La documentazione nel portale: i file Markdown di `docs/` come pagine (`/docs`), in inglese o italiano. Gli amministratori vedono ogni pagina; gli altri utenti solo `USER_PAGES` (guida utente e guida Windows). Si servono solo i nomi trovati nella cartella; l'HTML grezzo è mostrato come testo; i link tra le pagine sono riscritti, quelli ai file sorgente vanno su GitHub al tag della versione in esecuzione. La cartella è `/opt/spoolsteward/docs` nell'immagine, `docs/` in una copia dei sorgenti. |
| `printers.py` | Panoramica stampanti, contatori, stampanti di prova, stampa periodica e di prova (`/admin/printers`). |
| `controls.py` | Manutenzione, blocco / nascondimento di una stampante, feed `/mdns/policy` per l'annunciatore. |
| `backups.py` | Destinazioni dei backup (`/admin/backups`). |
| `audit_archive.py` | Elenco/lettura/download/verifica degli archivi (`/admin/archive`). |
| `audit_view.py` | Come si mostra una riga di audit (testi accorciati, dispositivo, specifiche di stampa). |
| `me.py` | Area dell'utente: consumo, chiavi, password, impostazioni (tema, lingua, aggiornamento), richieste (`/me`). |
| `printing.py` | Stampa dal browser (`/me/print`): controlli, quota, opzioni, orientamento orizzontale, permessi (lo stesso `decide()` del gateway), Print-Job a CUPS. |
| `../secheaders.py` (in `spoolsteward/`) | Le intestazioni di sicurezza. La Content-Security-Policy (`SPOOLSTEWARD_CONTENT_SECURITY_POLICY`) vieta script e stili in linea: le pagine tengono CSS e JavaScript in `static/`. Le pagine hanno anche `Referrer-Policy`, `Permissions-Policy` e `X-Frame-Options`; ogni risposta ha `nosniff`, e su HTTPS `Strict-Transport-Security`. |
| `common.py` | Parti condivise: template, dipendenze di sessione e CSRF, messaggi, reindirizzamenti, funzioni di supporto. |
| `templates/*.html` | Pagine Jinja: `base.html` (struttura, menu, tema), `macros.html`, un template per pagina (`docs.html` mostra la documentazione). |

### 3.6 Altri pacchetti

| File | Ruolo |
|---|---|
| `spoolsteward/audit/adminlog.py` | `record()`: scrive una riga di `admin_audit` (chi ha fatto cosa). |
| `spoolsteward/audit/archive.py` | Stima del peso, archiviazione sicura in `.jsonl.gz`, verifica, lettura in streaming, archiviatore periodico. |
| `spoolsteward/backup/core.py` | Crea/verifica/ruota/ripristina i backup; copia SQLite; tar.gz con `MANIFEST.json`; conservazione; il file segnaposto e il limite di tempo (`bounded`) per i dischi di rete. |
| `spoolsteward/backup/crypto.py` | Cifratura con chiave di recupero (X25519 + HKDF + AES-256-GCM; chiave privata avvolta da scrypt della passphrase). |
| `spoolsteward/backup/service.py` | Gestione della tabella `backup_target` e scheduler a 60 s. |
| `spoolsteward/db/models.py` | Tutti i modelli SQLAlchemy. `session.py`: motore e sessioni (SQLite WAL). `migrate.py`: esegue Alembic dall'interno dell'app. |
| `spoolsteward/notices.py`, `spoolsteward/admin/notices.py` | La campanella (tabella `notice`): avvisi per gli amministratori, con un limite alla frequenza con cui lo stesso viene segnalato (intervallo da `SPOOLSTEWARD_LOG_LEVEL`), e avvisi per un singolo utente. `notify_user_counted` rinnova l'avviso aperto dello stesso tipo invece di aggiungere righe; `SECONDARY_KINDS` (oggi `job_adjusted`) vengono segnati come letti da `read_secondary` dopo `user.secondary_read_minutes` (20 di base, 0 = mai), a ogni pagina mostrata. |
| `spoolsteward/secretcheck.py` | A ogni avvio: avviso se `SPOOLSTEWARD_SECRET_KEY` manca o è cambiata, e il promemoria, una volta sola, di salvarla. Ciò che è già stato detto è salvato in `gateway_setting`, mai la chiave. |
| `spoolsteward/printerwatch.py` | Ogni minuto: carta inceppata, carta finita, inchiostro finito/in esaurimento sulle stampanti fisiche, come avvisi per gli amministratori e per gli utenti in attesa. |
| `spoolsteward/keywatch.py` | Ogni ora: dimentica le chiavi di stampa vecchie, se acceso avvisa delle chiavi in scadenza, e avvisa chi ha un permesso temporaneo finito. |
| `spoolsteward/updates.py`, `spoolsteward/admin/updates.py` | Controllo giornaliero di una release più nuova (avviso agli amministratori) e pulsante del portale che passa un aggiornamento al contenitore `updater` tramite file in `/data/update` (il gateway non tocca mai Docker). |
| `spoolsteward/crypto_fields.py` | Tipi di colonna SQLAlchemy che cifrano i nomi dei file quando `SPOOLSTEWARD_ENCRYPT_FILE_NAMES` è acceso (AES-GCM, chiave derivata da `SPOOLSTEWARD_SECRET_KEY`); un valore illeggibile appare come `[encrypted]`. |
| `spoolsteward/titles.py` | Attività di fondo: se la cifratura dei nomi è accesa cifra quelli scritti in chiaro e, se attivo, cancella i nomi più vecchi di `SPOOLSTEWARD_JOB_TITLE_RETENTION_DAYS`. |
| `spoolsteward/certwatch.py` | Legge il certificato TLS e `sync-status.json`; apre e chiude gli avvisi sul certificato ([certificati.md](certificati.md)). |
| `spoolsteward/logsetup.py` | Formato dei log: fuso orario di `SPOOLSTEWARD_TIMEZONE` con lo scarto da UTC, livello da `SPOOLSTEWARD_LOG_LEVEL`. |
| `spoolsteward/migrations/` | `env.py` e le revisioni `0001`…`0017`. Dalla `0014` in poi una migrazione può solo aggiungere (lo verifica un test), così una versione più vecchia rimessa al suo posto parte anche su un database più nuovo. |
| `spoolsteward/locales/` | `messages.pot` (modello) e `<lingua>/messages.po` (it, es, ru, zh). |

## 4. Script

Un'installazione ha gli script per il server e per i computer degli utenti. `make-release-package.sh`, `gen_dev_cert.sh` e
`i18n.py` servono a chi sviluppa: stanno solo nel repository, non nel pacchetto della release.

| Script | Scopo |
|---|---|
| `scripts/docker-init.sh [nomi/IP]` | Preparazione una tantum, in shell puro (servono `sed` e `openssl`): crea `.env` (con versione, utente e un segreto generato), le cartelle `data/` e un certificato TLS autofirmato per i nomi/IP indicati. |
| `scripts/backup.sh backup\|restore` | Backup completo **lato host** (database, configurazione, archivio audit, code CUPS, `.env`); serve a spostare la macchina. |
| `scripts/add-printer-linux.sh` | Aggiunge una stampante del gateway a un computer Linux con la chiave di stampa nell'indirizzo della coda. |
| `scripts/print-test.sh` | Invia una pagina di testo come utente (consuma un foglio). |
| `scripts/resources.sh` | CPU e RAM usate dai container e da Docker stesso, sommate. Con `watch -n 5` si segue nel tempo. |
| `scripts/gen_dev_cert.sh` | Certificato autofirmato solo per lo sviluppo. |
| `scripts/cert-sync.sh [--dry-run]` | Da cron: copia il certificato TLS dalla macchina che lo rinnova (`rsync` su `ssh`), lo controlla, lo installa in `config/tls/` e riavvia il gateway se è cambiato. Scrive `config/tls/sync-status.json`, letto da `certwatch.py`. Vedi [certificati.md](certificati.md). |
| `scripts/make-release-package.sh VERSIONE CARTELLA` | Costruisce il pacchetto della release (`spoolsteward-<versione>.tar.gz` + `SHA256SUMS`): compose, script, `docker/env.example`, documentazione; mai `.env`, `config/`, `data/`. |
| `scripts/update.sh [VERSIONE\|--check]` | Aggiornamento sicuro: backup, scarica e verifica (sha256) il pacchetto della release, sostituisce compose e script (mai `.env`, `config/`, `data/`), pull, riavvio, attesa della salute, ritorno ai file precedenti se serve e offerta di ripristinare il backup. Si rifiuta in una cartella con `.git`. `--from-portal` è la modalità del contenitore `updater`. |
| `scripts/queue-restore.sh NOME [--reboot]` / `--list` | Rimette una coda eliminata di `CUPS_QUEUES`: toglie il nome da `/etc/cups/spoolsteward-queues` nel contenitore `cups`; `--reboot` lo riavvia. |
| `scripts/i18n.py` | Strumenti per le traduzioni: `extract`, `missing`, `check`, `add`. |

## 5. Contenitori

| Percorso | Scopo |
|---|---|
| `docker-compose.yml` | Lo stack di produzione (`ipp-usb`, `cups`, `gateway`, `mdns`, `radius` facoltativo). Costruisce dal sorgente o scarica immagini pubblicate. |
| `docker-compose.test.yml` | Ambiente dei test: un CUPS vero su `:6310` e FreeRADIUS su `:1812`. |
| `docker/stack/Dockerfile` | L'unica immagine `spoolsteward`: l'applicazione, CUPS e ipp-usb. I servizi `gateway`, `cups`, `ipp-usb` e `mdns` la avviano con comandi diversi. |
| `docker/stack/cups/` | CUPS: `cupsd.conf` (policy da backend privato), `entrypoint.sh` (crea le code da `CUPS_QUEUES`, ognuna una sola volta). I driver (brlaser, gutenprint, splix, foo2zjs) vengono dal Dockerfile. Vede anche `/dev/bus/usb` per le stampanti che ipp-usb non sa servire. |
| `docker/stack/ipp-usb/` | `ipp-usb.conf` e l'entrypoint che lo riavvia quando cambiano i dispositivi USB. |
| `docker/test/`, `docker/radius/` | `cupsd.conf` del CUPS dei test (quello di produzione più un ascolto TCP); utenti del RADIUS di prova (`mario`, `anna`, `admin`). |
| `docker/stack/updater/` | Immagine facoltativa `spoolsteward-updater` (profilo `autoupdate`): aspetta `/data/update/request.json`, esegue `scripts/update.sh --from-portal`, scrive `status.json`. Ha il socket di Docker. |
| `docker/env.example` | Il modello di `.env`, con ogni opzione spiegata (parte A per compose, parte B per il programma). Fa parte del pacchetto della release. |

## 6. Dove guardare quando…

| Vuoi… | Guarda |
|---|---|
| cambiare come si calcola una quota | `spoolsteward/policy/engine.py`, `spoolsteward/policy/audit.py` |
| consentire/vietare un'operazione IPP | `spoolsteward/ipp/gateway.py` |
| cambiare cosa fanno i permessi colore / solo fronte / stampanti / orari | `spoolsteward/policy/permissions.py` (`decide()`), poi dove viene applicata: `spoolsteward/ipp/gateway.py` e `spoolsteward/admin/printing.py` |
| aggiungere una pagina al portale | un router in `spoolsteward/admin/`, un template in `spoolsteward/admin/templates/` (stili e script in `static/css/` e `js/`, mai in linea), una voce di menu in `base.html` |
| aggiungere un'impostazione | `spoolsteward/config.py`, poi `docker/env.example` (parte B, con la spiegazione e lo stesso valore predefinito: un test lo controlla) e [configurazione.md](configurazione.md) |
| cambiare il database | `spoolsteward/db/models.py` **e** una nuova revisione Alembic (un test verifica che coincidano) |
| tradurre un testo | [lingue.md](lingue.md) |
