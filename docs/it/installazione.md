# Guida all'installazione

[Indice della documentazione](../README.md) · [English](../en/installation.md) · Italiano

SpoolSteward gira in contenitori Docker (`ipp-usb`, CUPS, gateway, mDNS): sull'host non si installa né si modifica nulla. Questa guida lo
installa dal pacchetto della release con le immagini pubblicate. Costruire le immagini dal sorgente è per gli sviluppatori:
[CONTRIBUTING.md](../../CONTRIBUTING.md). Problemi: [risoluzione-problemi.md](risoluzione-problemi.md).

## 1. Requisiti

| | |
|---|---|
| Server | Macchina **Linux** (ideale un mini-PC o un Raspberry Pi). Il passaggio USB e la rete `host` usata da mDNS esistono solo su Linux; su Docker Desktop (Windows, macOS) il gateway parte ma funzionano solo le stampanti **di rete** e i telefoni non le trovano da soli. |
| Software | Docker Engine con il plugin Compose (`docker compose version`), `openssl`, `curl` e `tar` (li usano gli script), `python3` (lo usa `scripts/backup.sh`). Non serve `git`. |
| Stampante | USB (tramite `ipp-usb`) o di rete (IPP). Qualunque cosa CUPS sappia pilotare. |
| Rete | Una porta TCP raggiungibile dai client (predefinita **8631**; `631` se nessun altro CUPS la usa). I client devono fidarsi del certificato (§3.3). |
| Architettura | **amd64** (PC, server) e **arm64** (Raspberry Pi 4/5, NAS ARM). Le immagini pubblicate contengono entrambe e Docker sceglie quella giusta. ARM a 32 bit (armv7) non è supportato. |
| Memoria | Circa **550 MB** di RAM libera per tutto lo stack, Docker compreso. Misurati a riposo su un Raspberry Pi 4 (arm64) e su una macchina amd64: circa 400 MB in entrambi i casi; il resto è margine per stampa e scansione. Un lavoro con un documento grande ne usa di più per qualche istante. Misura la tua macchina con `scripts/resources.sh` (CPU e RAM dei container e di Docker, sommate; `watch -n 5 scripts/resources.sh` per seguirle nel tempo). |
| Un gateway per stampante USB | `ipp-usb` vuole il dispositivo in esclusiva (§3.4). |

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
#    deve stampare "created .env": se stampa "kept the existing .env", questa cartella ne aveva già uno (§3.1)

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
| `config/tls/` | un certificato autofirmato (825 giorni) i cui nomi alternativi sono i nomi/IP indicati |
| `data/audit-archive/`, `data/backups/` | cartelle sull'host per l'audit compresso e i backup |

Rieseguirlo non sostituisce mai un file esistente. **Un `.env` esistente non viene mai modificato**, nemmeno per aggiungere una riga
mancante: lo script stampa "kept the existing .env". Se quel file non ha la riga `SPOOLSTEWARD_SECRET_KEY=`, oppure la chiave vale `none`,
è ancora `__GENERATED__` o è più corta di 64 caratteri, stampa anche un avviso; cosa fare:
[risoluzione-problemi.md](risoluzione-problemi.md#nessuna-chiave-segreta-impostata).

Non copiare a mano `docker/env.example` in `.env`: lo script inserisce il tuo utente, questa cartella, la versione e la chiave.

Dopo il primo avvio il portale ricorda una volta al primo amministratore di copiare `SPOOLSTEWARD_SECRET_KEY` in un posto sicuro
([backup.md](backup.md#salva-la-chiave-segreta)). Non condividere `config/` né `.env`: contengono segreti.

**Non modificare mai `docker-compose.yml`**: tutto quello che puoi cambiare sta in `.env`, così un aggiornamento può sostituire il compose
senza cancellare le tue scelte.

### 3.2 Impostazioni

`.env` è interamente commentato. In fase di installazione guarda `SPOOLSTEWARD_TIMEZONE` (predefinito `UTC`), `SPOOLSTEWARD_RADIUS_HOST`
e `SPOOLSTEWARD_RADIUS_SECRET` (entrambi `none` per i soli account locali), `SPOOLSTEWARD_PORTAL_URL` e `PORTAL_PORT`. Ogni impostazione,
con il suo valore predefinito e le sue regole: [configurazione.md](configurazione.md). Dopo ogni modifica: `docker compose up -d`.

### 3.3 Certificato TLS

Il TLS è obbligatorio: senza certificato il gateway non parte. Quello creato da `docker-init.sh` è autofirmato: installa
`config/tls/fullchain.pem` come attendibile su ogni client, oppure sostituisci la coppia in `config/tls/` con un certificato della tua CA.
Il certificato deve elencare i nomi/IP usati dai client, compreso l'indirizzo annunciato da mDNS. Come installare la CA su ogni
dispositivo, usare un certificato che hai già, tenerlo rinnovato o lavorare dietro un reverse proxy: [certificati.md](certificati.md).

### 3.4 Stampanti

**USB.** `ipp-usb` ha bisogno dell'uso esclusivo della stampante. Se è installato sull'host, fermalo e mascheralo (vale anche per SANE):

```sh
sudo systemctl disable --now ipp-usb && sudo systemctl mask ipp-usb
```

Una stampante USB riceve la sua coda da sola entro circa un minuto da quando la colleghi; in `.env` non serve scrivere nulla. Come le
stampanti vengono trovate, chiamate e tolte: [stampanti.md](stampanti.md#2-stampanti-usb-nuove-e-scollegate).

**Di rete.** Dai a `CUPS_QUEUES` in `.env` l'indirizzo IPP della stampante: `CUPS_QUEUES="UFFICIO=ipp://192.168.1.60/ipp/print"` (più
code si separano con spazi). Le regole di quella riga: [configurazione.md](configurazione.md#11-parte-a-variabili-di-docker-compose).
Scanner: [scansione.md](scansione.md).

### 3.5 Avvio

```sh
docker compose up -d
```

Il primo avvio scarica le immagini (`ghcr.io/deh-aleh/spoolsteward:<versione>`). Verifica: `curl -sk https://<server>:8631/healthz`
risponde 200 e `docker compose ps` mostra `cups` in stato healthy.

### 3.6 Primo amministratore

`docker compose logs gateway | grep "FIRST RUN"` mostra un codice monouso. Apri `https://<server>:8631/setup`, inserisci il codice e
scegli la password di `administrator` (minimo 12 caratteri). Dettagli: [account.md](account.md#2-il-primo-amministratore). Se non compare
nulla: [risoluzione-problemi.md](risoluzione-problemi.md#2-non-riesco-ad-accedere).

### 3.7 Utenti e dispositivi

Crea utenti e gruppi nel portale: [guida-amministratore.md](guida-amministratore.md). Collegare computer e telefoni: [stampa.md](stampa.md),
Windows passo per passo: [windows.md](windows.md). Cosa fanno gli utenti: [guida-utente.md](guida-utente.md).

## 4. Provare senza stampante e senza RADIUS

```sh
# in .env:  CUPS_VIRTUAL_QUEUE=1   SPOOLSTEWARD_RADIUS_HOST=radius   SPOOLSTEWARD_RADIUS_SECRET=testing123
docker compose --profile radius-test up -d
```

Crea la coda `virtual` (scrive PDF, nessuna carta) e un RADIUS di prova con `mario/mario123`, `anna/anna123`, `admin/admin123`. Esempio:
`ipptool -t -f file.txt "ipps://mario:mario123@localhost:8631/printers/virtual" job.test`. I PDF:
`docker compose exec cups ls /var/spool/cups-pdf/ANONYMOUS`. Non lasciare account di prova su un'installazione vera.

## 5. Aggiornamenti

Ogni release pubblica le immagini (`ghcr.io/deh-aleh/spoolsteward:<versione>`, più `spoolsteward-updater`; amd64 + arm64) e un
**pacchetto** (`spoolsteward-<versione>.tar.gz` con il suo `SHA256SUMS`) che contiene `docker-compose.yml`, gli script,
`docker/env.example` e la documentazione. Il pacchetto non contiene mai `.env`, `config/` né `data/`: un aggiornamento non può toccarli.
`.env` contiene la versione in uso (`SPOOLSTEWARD_VERSION`); cambia solo quando aggiorni, mai da sola. Le migrazioni del database partono
all'avvio.

**`scripts/update.sh`** è la via normale. Fa un backup completo (`data/backups/pre-update/`), scarica il pacchetto della nuova versione e
ne controlla la somma sha256, sostituisce `docker-compose.yml` e gli script (i vecchi restano in `.update-prev/`), scrive la nuova versione
in `.env`, scarica le immagini, riavvia e aspetta che il gateway sia in salute. Se non lo è, rimette i file e la versione di prima; se
nemmeno la versione di prima parte, ti propone di ripristinare il backup appena fatto. `scripts/update.sh <versione>` sceglie una
versione; `--check` dice solo se ne esiste una più nuova.

**Il pulsante *Aggiorna* nel portale** (menu *Backup*, riquadro *Aggiornamento*) è facoltativo. Richiede il contenitore `updater` (sotto)
e un backup programmato attivo e riuscito nelle ultime 48 ore: si fa un backup fresco, poi parte l'aggiornamento. Se la nuova versione non
parte viene rimessa quella vecchia; il ripristino del backup, se serve, lo fai tu con `scripts/backup.sh restore`.

**Beta.** Una versione come `w.x.y-beta.z` è una release di prova, pubblicata come pre-release su GitHub. Con
`SPOOLSTEWARD_UPDATE_BETA=true` (il valore scritto da `docker-init.sh`) le beta sono proposte come le release finali: nell'avviso di nuova
versione, dal pulsante *Aggiorna* e da `scripts/update.sh` senza argomenti. Installare una beta chiede una conferma in più (una casella
da spuntare nel portale; `update.sh` chiede di scrivere `YES`). Con `false` sono proposte solo le release finali; una beta che nomini tu
(`scripts/update.sh w.x.y-beta.z`) viene installata qualunque sia l'impostazione.

**Modifiche incompatibili.** Una release può dichiarare modifiche incompatibili (*breaking changes*): le sue note di rilascio hanno un
titolo "Breaking changes", oppure cambia il primo numero della versione, o il secondo mentre il primo è `0` (da `0.1.x` a `0.2.0`). Conta
anche una release saltata: passare da `w.x.y-beta.y1` a `w.x.y-beta.y3` è incompatibile se lo era `w.x.y-beta.z`. Allora la conferma avvisa
che l'aggiornamento potrebbe fallire e lasciare SpoolSteward non funzionante, e chiede di copiare prima un backup recente fuori dal server
e di tenere una copia di `.env` e `config/`; il portale avvia l'aggiornamento solo con la casella spuntata, `update.sh` solo dopo `YES`.
Leggi le note di rilascio prima di confermare.

**Versioni maggiori.** Un aggiornamento che cambia il primo numero della versione (da `1.x` a `2.0.0`) non si installa dal portale: il
riquadro *Aggiorna* lo mostra con il pulsante spento. Esegui `scripts/update.sh` da terminale, dopo aver letto le note di rilascio.

**Avviso di nuova versione.** Una volta al giorno il gateway chiede a GitHub l'elenco delle release di `SPOOLSTEWARD_UPDATE_REPOSITORY` e
mostra un avviso agli amministratori. Esce solo quella richiesta e non invia nulla del tuo server. `SPOOLSTEWARD_UPDATE_CHECK=false` lo spegne
([configurazione.md](configurazione.md#5-aggiornamenti)).

**Impostazioni nuove.** Un aggiornamento non modifica mai `.env`. Un'impostazione nuova funziona con il suo valore predefinito anche se
nel tuo file non c'è; `docker/env.example` (sostituito a ogni aggiornamento) la mostra con la sua spiegazione. Per cambiarla copia la sua
riga in `.env` ed esegui `docker compose up -d`.

**Il contenitore facoltativo `updater`** applica gli aggiornamenti chiesti dal portale. È **spento di default** perché usa il socket di
Docker (`/var/run/docker.sock`), e chi controlla quel socket controlla tutta la macchina (root): chi trovasse un difetto nel portale
potrebbe usarlo per comandare Docker e prendere il server. Legge solo un numero di versione scritto dal gateway; le immagini arrivano
sempre dal registry del tuo `.env`. Resta alla versione con cui è stato creato finché non esegui `docker compose up -d` da terminale. Per
accenderlo:

```sh
echo "COMPOSE_PROFILES=autoupdate" >> .env      # una riga in .env (oppure aggiungi --profile autoupdate ogni volta)
docker compose up -d
```

Se non vuoi quel rischio, lascialo spento e usa `scripts/update.sh`. Per spegnerlo di nuovo: togli la riga da `.env` ed esegui
`docker compose stop updater`.

**Tornare a una versione precedente.** `scripts/update.sh <versione>` rimette i file e le immagini di quella versione. Si fa **solo da terminale**: il pulsante *Aggiorna* del portale va solo avanti e rifiuta una versione più vecchia (la richiesta arriva dal gateway, la parte del sistema esposta alla rete, e non deve poter installare una versione con una falla nota). Una versione più
vecchia parte su un database che una più nuova ha migrato solo se quelle migrazioni lo hanno lasciato leggibile; quando non parte, o la
versione nuova ha rovinato qualcosa, usa il backup che `update.sh` ha fatto **prima** dell'aggiornamento:

```sh
ls data/backups/pre-update/
#   spoolsteward-v<versione>-<data>-<ora>.tar.gz   <- uno per aggiornamento, con il nome della versione che girava
scripts/backup.sh restore data/backups/pre-update/spoolsteward-v<versione>-<data>-<ora>.tar.gz    # chiede di scrivere YES
scripts/update.sh <versione>               # rimette i file di quella versione (il ripristino ha riportato il suo .env)
```

Tutto ciò che è successo dopo quel backup (storico delle stampe, nuovi utenti, richieste) si perde; il backup riporta anche il `.env` di
quel momento. Ogni tentativo fa prima un nuovo backup, quindi non si sovrascrive niente.

**Blocca la versione.** Non usare mai `latest`: lascia `SPOOLSTEWARD_VERSION` a un numero, così l'aggiornamento è una decisione.

## 6. Backup e spostamento su un'altra macchina

Backup programmati dal portale e il loro ripristino: [backup.md](backup.md). `scripts/backup.sh backup` fa una copia completa lato host
(database, `config/`, `.env`, archivio audit, code CUPS) per spostare l'installazione: [backup.md](backup.md#3-backup-completo-dal-lato-host).
