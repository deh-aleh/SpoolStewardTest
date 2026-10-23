# Guida all'installazione

[Indice della documentazione](../README.md) · [English](../en/installation.md) · Italiano

SpoolSteward gira in contenitori Docker (`ipp-usb`, CUPS, gateway, mDNS): sull'host non si installa né si modifica nulla. Questa guida lo installa dal pacchetto della release con le immagini pubblicate. Costruire dal sorgente è per gli sviluppatori: [CONTRIBUTING.md](../../CONTRIBUTING.md). Problemi: [risoluzione-problemi.md](risoluzione-problemi.md).

## 1. Requisiti

| | |
|---|---|
| Server | Macchina **Linux** (ideale un mini-PC o un Raspberry Pi). Il passaggio USB e la rete `host` usata da mDNS esistono solo su Linux; su Docker Desktop (Windows, macOS) il gateway parte ma funzionano solo le stampanti **di rete** e i telefoni non le trovano da soli. |
| Software | Docker Engine con il plugin Compose, `openssl`, `curl` e `tar`, `python3` (per `scripts/backup.sh`). Non serve `git`. |
| Stampante | USB (tramite `ipp-usb`) o di rete (IPP). Qualunque cosa CUPS sappia pilotare. Un gateway per stampante USB: `ipp-usb` vuole il dispositivo in esclusiva (§3.4). |
| Rete | Una porta TCP raggiungibile dai client (predefinita **8631**; `631` se nessun altro CUPS la usa). I client devono fidarsi del certificato (§3.3). |
| Architettura | **amd64** e **arm64** (Raspberry Pi 4/5, NAS ARM); Docker sceglie l'immagine giusta. ARM a 32 bit non è supportato. |
| Memoria | Circa **550 MB** di RAM libera per tutto lo stack, Docker compreso (circa 400 MB a riposo su un Raspberry Pi 4 e su amd64; un documento grande ne usa di più per qualche istante). Misura la tua macchina con `scripts/resources.sh`. |

## 2. Percorso rapido

```sh
# 0. scarica il pacchetto dell'ultima release (https://github.com/deh-aleh/SpoolSteward/releases) e controllalo
VERSION=w.x.y #w.x.y-beta.z se vuoi installare una versione in beta
curl -LO https://github.com/deh-aleh/SpoolSteward/releases/download/v$VERSION/spoolsteward-$VERSION.tar.gz
curl -LO https://github.com/deh-aleh/SpoolSteward/releases/download/v$VERSION/SHA256SUMS
sha256sum -c --ignore-missing SHA256SUMS            # deve rispondere «OK»: il file è quello pubblicato
tar xzf spoolsteward-$VERSION.tar.gz && cd spoolsteward-$VERSION

# 1. nomi / IP con cui i client raggiungeranno il server (finiscono nel certificato TLS)
scripts/docker-init.sh print.lan 192.168.1.50

# 2. impostazioni (RADIUS è facoltativo: scrivi none in host e secret per usare solo account locali)
$EDITOR .env

# 3. stampanti (§3.4), poi avvia
docker compose up -d

# 4. leggi il codice monouso e scegli la password dell'amministratore
docker compose logs gateway | grep "FIRST RUN"
#    apri https://<server>:8631/setup
```

## 3. Passo per passo

### 3.1 Preparazione

`scripts/docker-init.sh [nome-o-IP ...]` scrive **solo dentro la cartella del progetto**:

| Creato | Cosa |
|---|---|
| `.env` | **tutte** le impostazioni in un solo file, ricavato da `docker/env.example` con il tuo utente (`PUID`/`PGID`), questa cartella, la versione e una `SPOOLSTEWARD_SECRET_KEY` appena generata. Permessi 600 (contiene segreti). |
| `config/tls/` | un certificato autofirmato (825 giorni) per i nomi/IP indicati |
| `data/audit-archive/`, `data/backups/` | cartelle sull'host per l'audit compresso e i backup |

Rieseguirlo non sostituisce mai un file esistente. **Un `.env` esistente non viene mai modificato**, nemmeno per aggiungere una riga mancante: lo script stampa "kept the existing .env" e, se la chiave segreta manca, vale `none`, è ancora `__GENERATED__` o è più corta di 64 caratteri, anche un avviso
([risoluzione-problemi.md](risoluzione-problemi.md#nessuna-chiave-segreta-impostata)). Non copiare a mano `docker/env.example` in `.env`. Copia `SPOOLSTEWARD_SECRET_KEY` in un posto sicuro ([backup.md](backup.md#salva-la-chiave-segreta)) e non condividere `config/` né `.env`: contengono segreti.

### 3.2 Impostazioni

`.env` è interamente commentato. In fase di installazione guarda `SPOOLSTEWARD_TIMEZONE` (predefinito `UTC`), `SPOOLSTEWARD_RADIUS_HOST` e `SPOOLSTEWARD_RADIUS_SECRET` (entrambi `none` per i soli account locali), `SPOOLSTEWARD_PORTAL_URL` e `PORTAL_PORT`. Ogni impostazione: [configurazione.md](configurazione.md). Dopo ogni modifica: `docker compose up -d`.

### 3.3 Certificato TLS

Il TLS è obbligatorio: senza certificato il gateway non parte. Quello creato da `docker-init.sh` è autofirmato: installa `config/tls/fullchain.pem` come attendibile su ogni client, oppure sostituisci la coppia in `config/tls/` con un certificato della tua CA. Deve elencare i nomi/IP usati dai client, compreso l'indirizzo annunciato da mDNS.
Installare la CA sui dispositivi, riusare un certificato, rinnovi e reverse proxy: [certificati.md](certificati.md).

### 3.4 Stampanti

**USB.** `ipp-usb` ha bisogno dell'uso esclusivo della stampante. Se è installato sull'host, fermalo e mascheralo (vale anche per SANE):

```sh
sudo systemctl disable --now ipp-usb && sudo systemctl mask ipp-usb
```

Una stampante USB riceve la sua coda da sola entro circa un minuto da quando la colleghi ([stampanti.md](stampanti.md#2-stampanti-usb-nuove-e-scollegate)). **Di rete.** Dai a `CUPS_QUEUES` in `.env` l'indirizzo IPP della stampante: `CUPS_QUEUES="UFFICIO=ipp://192.168.1.60/ipp/print"` (più code si separano con spazi;
[configurazione.md](configurazione.md#11-parte-a-variabili-di-docker-compose)). Scanner: [scansione.md](scansione.md).

### 3.5 Avvio

```sh
docker compose up -d
```

Il primo avvio scarica le immagini. Verifica: `curl -sk https://<server>:8631/healthz` risponde 200 e `docker compose ps` mostra `cups` in stato healthy.

### 3.6 Primo amministratore

`docker compose logs gateway | grep "FIRST RUN"` mostra un codice monouso. Apri `https://<server>:8631/setup`, inserisci il codice e scegli la password di `administrator` (minimo 12 caratteri). Dettagli: [account.md](account.md#2-il-primo-amministratore). Se non compare nulla: [risoluzione-problemi.md](risoluzione-problemi.md#2-non-riesco-ad-accedere).

### 3.7 Utenti e dispositivi

Crea utenti e gruppi nel portale ([guida-amministratore.md](guida-amministratore.md)). Collegare computer e telefoni: [stampa.md](stampa.md); Windows passo per passo: [windows.md](windows.md); cosa fanno gli utenti: [guida-utente.md](guida-utente.md).

## 4. Provare senza stampante e senza RADIUS

```sh
# in .env:  CUPS_VIRTUAL_QUEUE=1   SPOOLSTEWARD_RADIUS_HOST=radius   SPOOLSTEWARD_RADIUS_SECRET=testing123
docker compose --profile radius-test up -d
```

Crea la coda `virtual` (scrive PDF, nessuna carta) e un RADIUS di prova con `mario/mario123`, `anna/anna123`, `admin/admin123`. Esempio: `ipptool -t -f file.txt "ipps://mario:mario123@localhost:8631/printers/virtual" job.test`; i PDF: `docker compose exec cups ls /var/spool/cups-pdf/ANONYMOUS`. Non lasciare account di prova su un'installazione vera.

## 5. Aggiornamenti

Ogni release pubblica le immagini (`ghcr.io/deh-aleh/spoolsteward:<versione>`, più `spoolsteward-updater`; amd64 + arm64) e un **pacchetto** (`spoolsteward-<versione>.tar.gz` con il suo `SHA256SUMS`) che contiene `docker-compose.yml`, gli script, `docker/env.example` e la documentazione. Il pacchetto non contiene mai `.env`, `config/` né `data/`.
`.env` contiene la versione in uso (`SPOOLSTEWARD_VERSION`), che cambia solo quando aggiorni; non usare mai `latest`, così l'aggiornamento è una decisione. Le migrazioni del database partono all'avvio. Un aggiornamento non modifica mai `.env`: un'impostazione nuova funziona con il suo valore predefinito e `docker/env.example` (sostituito a ogni aggiornamento) la mostra.

**`scripts/update.sh`** è la via normale. Fa un backup completo (`data/backups/pre-update/`), scarica il nuovo pacchetto e ne controlla la somma sha256, sostituisce `docker-compose.yml` e gli script (i vecchi restano in `.update-prev/`), scrive la nuova versione in `.env`, scarica le immagini, riavvia e aspetta che il gateway sia in salute. Se non lo è,
rimette i file e la versione di prima; se nemmeno quella parte, ti propone di ripristinare il backup appena fatto. `scripts/update.sh <versione>` sceglie una versione; `--check` dice solo se ne esiste una più nuova.

**Il pulsante *Aggiorna* nel portale** (menu *Backup*, riquadro *Aggiornamento*) è facoltativo. Richiede il contenitore `updater` (sotto) e un backup programmato attivo e riuscito nelle ultime 48 ore: si fa un backup fresco, poi parte l'aggiornamento. Se la nuova versione non parte viene rimessa quella vecchia; il ripristino del backup, se serve, lo fai tu con `scripts/backup.sh restore`.

**Beta.** Una versione come `w.x.y-beta.z` è una pre-release su GitHub. Con `SPOOLSTEWARD_UPDATE_BETA=true` (il valore scritto da `docker-init.sh`) le beta sono proposte come le release finali (nell'avviso, dal pulsante e da `scripts/update.sh` senza argomenti), con una conferma in più (una casella nel portale; `YES` in `update.sh`).
Con `false` sono proposte solo le release finali; una beta che nomini tu (`scripts/update.sh w.x.y-beta.z`) viene installata comunque.

**Modifiche incompatibili.** Una release può dichiararle: le sue note hanno un titolo "Breaking changes", oppure cambia il primo numero della versione, o il secondo mentre il primo è `0` (da `0.1.x` a `0.2.0`). Conta anche una release saltata. La conferma avvisa allora che l'aggiornamento potrebbe lasciare SpoolSteward non funzionante e chiede di copiare prima un backup recente
fuori dal server e di tenere `.env` e `config/`; il portale parte solo con la casella spuntata, `update.sh` solo dopo `YES`. Leggi prima le note di rilascio. Le **versioni maggiori** (da `1.x` a `2.0.0`) non si installano dal portale: il pulsante è spento; esegui `scripts/update.sh` da terminale.

**Avviso di nuova versione.** Una volta al giorno il gateway chiede a GitHub le release di `SPOOLSTEWARD_UPDATE_REPOSITORY` e mostra un avviso agli amministratori, senza inviare nulla del tuo server. `SPOOLSTEWARD_UPDATE_CHECK=false` lo spegne ([configurazione.md](configurazione.md#5-aggiornamenti)).

**Il contenitore facoltativo `updater`** applica gli aggiornamenti chiesti dal portale. È **spento di default** perché usa il socket di Docker (`/var/run/docker.sock`), e chi lo controlla controlla tutta la macchina (root): chi trovasse un difetto nel portale potrebbe usarlo per prendere il server. Legge solo un numero di versione scritto dal gateway; le immagini arrivano
sempre dal registry del tuo `.env`. Per accenderlo:

```sh
echo "COMPOSE_PROFILES=autoupdate" >> .env      # una riga in .env (oppure aggiungi --profile autoupdate ogni volta)
docker compose up -d
```

Per spegnerlo: togli la riga da `.env` ed esegui `docker compose stop updater`. Se non vuoi quel rischio, usa `scripts/update.sh`.

**Tornare a una versione precedente.** `scripts/update.sh <versione>` rimette i file e le immagini di quella versione, **solo da terminale**: il pulsante del portale va solo avanti (il gateway è esposto alla rete e non deve poter installare una versione con una falla nota). Una versione più vecchia parte su un database che una più nuova ha migrato solo se quelle migrazioni lo hanno lasciato leggibile;
quando non parte, o la nuova ha rovinato qualcosa, usa il backup che `update.sh` ha fatto **prima** dell'aggiornamento:

```sh
ls data/backups/pre-update/
#   spoolsteward-v<versione>-<data>-<ora>.tar.gz   <- uno per aggiornamento, con il nome della versione che girava
scripts/backup.sh restore data/backups/pre-update/spoolsteward-v<versione>-<data>-<ora>.tar.gz    # chiede di scrivere YES
scripts/update.sh <versione>               # rimette i file di quella versione (il ripristino ha riportato il suo .env)
```

Tutto ciò che è successo dopo quel backup (storico delle stampe, nuovi utenti, richieste) si perde. Ogni tentativo fa prima un nuovo backup, quindi non si sovrascrive niente.

## 6. Backup e spostamento su un'altra macchina

Backup programmati dal portale e il loro ripristino: [backup.md](backup.md). `scripts/backup.sh backup` fa una copia completa lato host (database, `config/`, `.env`, archivio audit, code CUPS) per spostare l'installazione: [backup.md](backup.md#3-backup-completo-dal-lato-host).
