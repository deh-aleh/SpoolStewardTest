# Certificati TLS: avvisi e sincronizzazione

[Indice della documentazione](../README.md) · [English](../en/certificates.md) · Italiano

Il gateway serve IPPS e il portale solo su TLS. Questa pagina descrive i requisiti del certificato, gli avvisi che compaiono quando è
sbagliato e `scripts/cert-sync.sh`, che copia un certificato rinnovato da un'altra macchina (per esempio Nginx Proxy Manager).

## 1. Cosa verifica un client

Un client controlla tre cose prima di inviare qualunque dato; se una fallisce rifiuta la connessione o avvisa:

1. **Catena**: emesso da una CA di cui il dispositivo si fida (CA pubblica, oppure la CA aziendale installata sul dispositivo, §3).
2. **Nome**: l'host a cui il client si collega è nel Subject Alternative Name (SAN) del certificato, come nome DNS o indirizzo IP.
3. **Validità**: `notAfter` è nel futuro.

L'annuncio (mDNS) pubblica l'IP della route di default, oppure `SPOOLSTEWARD_MDNS_ADDRESSES`, e il nome host di `SPOOLSTEWARD_MDNS_HOSTNAME`
(predefinito: l'host di `SPOOLSTEWARD_PORTAL_URL`, altrimenti il nome della macchina). I client che trovano la stampante via mDNS si collegano a
quell'indirizzo, che deve quindi essere nel SAN. Assegna al server un indirizzo fisso: se il DHCP lo cambia, il certificato non corrisponde più.

**Browser e solo HTTPS (HSTS).** Con `SPOOLSTEWARD_HSTS_SECONDS` sopra `0` (**spenta di default**; prova `2592000`, 30 giorni, poi `docker compose up -d`)
il portale dice al browser di usare solo HTTPS con questo **nome host**:

- **Un certificato di cui il browser non si fida** (l'autofirmato di `docker-init.sh`): il browser **ignora** l'intestazione.
- **Un certificato di cui si fida** (un'autorità vera, o la CA aziendale, §3): il browser se la ricorda. `http://stampe.lan` diventa da solo `https://stampe.lan`
  e **un errore di certificato non si può più scavalcare**. Se il certificato scade e nessuno lo rinnova, il browser mostra «la connessione non è
  privata» senza il link «continua», quindi nessuno raggiunge il portale per leggere l'avviso. Appena il certificato è rinnovato torna a funzionare subito.
- **Vale per il nome host, qualunque sia la porta.** Una pagina in `http` semplice dello stesso nome su un'altra porta smette di aprirsi.

Per spegnerla imposta `SPOOLSTEWARD_HSTS_SECONDS=0`: i browser tengono la regola finché il suo tempo non è finito (al massimo 30 giorni), finché non ricevono
l'intestazione con `0` su HTTPS valido, o finché non si cancellano i dati del sito. Con un certificato che si rinnova da solo puoi poi alzarla a `31536000` (un anno).

## 2. Avvisi nel portale (la campanella)

Il gateway non sostituisce mai il certificato. Ogni 6 ore legge `SPOOLSTEWARD_TLS_CERTFILE` e `config/tls/sync-status.json` e apre o chiude avvisi per gli
amministratori; un avviso si chiude da solo quando la causa sparisce.

| Avviso | Condizione | Interruttore |
|---|---|---|
| **Il certificato TLS è scaduto** | `notAfter` è passato | sempre attivo |
| **Non è stato possibile aggiornare il certificato** | l'ultima esecuzione di `scripts/cert-sync.sh` ha scritto `"ok": false` in `sync-status.json` (il messaggio è nell'avviso) | solo se usi lo script |
| **L'indirizzo annunciato non è nel certificato TLS** | nessun indirizzo/nome annunciato è nel SAN; i caratteri jolly (`*.example.com`) coprono una sola etichetta | `SPOOLSTEWARD_CERT_NAME_CHECK=false` lo spegne |
| **Il certificato TLS scade il (data)** | mancano meno di `SPOOLSTEWARD_CERT_EXPIRY_WARN_DAYS` giorni | **spento di default**: `SPOOLSTEWARD_CERT_EXPIRY_WARN_ENABLED=true` |

L'avviso di scadenza aggiunge un suggerimento dall'ultima sincronizzazione: se è riuscita, la sorgente non ha rinnovato; se è fallita, vedi l'altro avviso; se
lo script non è usato, rinnova e metti i file in `config/tls`.

**Perché l'avviso di scadenza è spento di default.** Un certificato Let's Encrypt dura 90 giorni e si rinnova circa 30 giorni prima di `notAfter`. Con la
soglia a 14 giorni l'avviso compare solo se il rinnovo fallisce da più di due settimane; sopra i 30 scatterebbe a ogni ciclo normale. Un certificato di una CA
aziendale dura spesso un anno e si rinnova a mano: lì 30 giorni vanno bene.

## 3. Il file della CA: cosa farne

Con una CA aziendale, installa il **certificato della CA** (un file `.crt` o `.pem`; mai la chiave privata) una volta su ogni dispositivo:

| Dispositivo | Cosa fare |
|---|---|
| Android | *Impostazioni → Sicurezza → Crittografia e credenziali → Installa un certificato → Certificato CA*. Il browser lo accetta; **alcune app ignorano le CA aggiunte dall'utente**: in quel caso usa una CA pubblica (per esempio Let's Encrypt). |
| iPhone / iPad | Apri il file, *Impostazioni → Profilo scaricato → Installa*. Poi *Impostazioni → Generali → Info → Attendibilità certificati* e attiva la CA: senza questo passaggio il certificato resta non attendibile. |
| Windows | Doppio clic sul file → *Installa certificato* → *Computer locale* → *Autorità di certificazione radice attendibili*. |
| macOS | *Accesso Portachiavi*: trascina il file in *Sistema*, doppio clic → *Attendibilità* → *Considera sempre attendibile*. |
| Linux (Debian, Ubuntu) | Copialo in `/usr/local/share/ca-certificates/` come `.crt`, poi `sudo update-ca-certificates`. Firefox ha un archivio suo: *Impostazioni → Privacy e sicurezza → Certificati → Mostra certificati → Autorità → Importa*. |

Con il certificato autofirmato di `scripts/docker-init.sh` il file da installare è `config/tls/fullchain.pem`. Una CA pubblica (Let's Encrypt) non richiede nulla sui dispositivi.

## 4. Copiare il certificato a mano

Metti `fullchain.pem` (certificato e catena) e `privkey.pem` in `config/tls/`, rendi la chiave leggibile solo all'utente `PUID` di `.env`
(`chmod 600 config/tls/privkey.pem`) ed esegui `docker compose restart gateway`.

**Hai già un certificato valido (per esempio da Nginx Proxy Manager su un'altra macchina).** Fallo usare direttamente al gateway:

1. Fai risolvere verso il server, dentro la tua rete, un nome coperto da quel certificato (diciamo `print.example.com`).
2. Copia la coppia (NPM: `data/letsencrypt/live/npm-<n>/` oppure, per i certificati personalizzati, `data/custom_ssl/npm-<n>/`) in `config/tls/`, `chmod 600` sulla chiave ed esegui `docker compose restart gateway`.
3. Imposta `SPOOLSTEWARD_PORTAL_URL=https://print.example.com:8631` in `.env` e usa `ipps://print.example.com:8631/printers/<coda>`.

I dispositivi che trovano le stampanti con mDNS ricevono il nome `<nome>.local` o l'IP, che quel certificato non copre: su quei dispositivi usa a mano il nome vero, oppure
ferma il servizio `mdns` se non ti serve la scoperta. Let's Encrypt si rinnova circa ogni 60 giorni: per quel caso usa il §5.

### 4.1 Dietro un reverse proxy

Un reverse proxy può tenere il certificato e inoltrare a `https://<server>:8631` (non provato dal progetto). Deve inoltrare con schema `https`, accettare corpi
grandi e non metterli in buffer (`client_max_body_size 250m; proxy_request_buffering off; proxy_read_timeout 600s;`). Il gateway vede ogni client con l'indirizzo del
proxy (blocco dei login, IP nell'audit) a meno che in `.env` sia impostato `FORWARDED_ALLOW_IPS=<indirizzo del proxy>`. Gli indirizzi delle stampanti restituiti ai
client portano l'`Host` inoltrato dal proxy, senza `:443`, quindi i dispositivi devono tenere l'indirizzo con cui sono stati configurati.

## 5. Tenere il certificato aggiornato da solo: `scripts/cert-sync.sh`

### 5.1 Come funziona

Eseguilo una volta a mano, poi da `cron`. A ogni esecuzione:

1. scarica i due file dalla macchina che rinnova i certificati (`rsync` su `ssh`) in una cartella temporanea privata;
2. li controlla: il certificato non è scaduto e la chiave è proprio la sua;
3. se sono uguali a quelli in uso, **si ferma** (niente riavvii);
4. altrimenti mette da parte la coppia vecchia in `config/tls/previous/`, installa la nuova e riavvia il gateway;
5. scrive l'esito in `config/tls/sync-status.json`; se è andata male il portale mostra un avviso.

Se un controllo fallisce, **i file in uso non vengono toccati**.

### 5.2 Le impostazioni (in `.env`)

`CERT_SYNC_HOST`, `CERT_SYNC_USER`, `CERT_SYNC_KEY` (percorso completo della chiave privata ssh di questa macchina) e `CERT_SYNC_REMOTE_DIR` (percorso completo della
cartella con i due file) sono obbligatorie; `CERT_SYNC_PORT` (`22`), `CERT_SYNC_CERT_FILE` (`fullchain.pem`) e `CERT_SYNC_KEY_FILE` (`privkey.pem`) sono facoltative
([configurazione.md](configurazione.md#sincronizzazione-del-certificato-cert_sync_)). La cartella dipende dal programma che rinnova:

| Programma | `CERT_SYNC_REMOTE_DIR` (esempio) | File |
|---|---|---|
| Nginx Proxy Manager, Let's Encrypt | `/home/tu/npm/data/letsencrypt/live/npm-3` | `fullchain.pem`, `privkey.pem` |
| Nginx Proxy Manager, certificato personalizzato | `/home/tu/npm/data/custom_ssl/npm-3` | `fullchain.pem`, `privkey.pem` |
| certbot | `/etc/letsencrypt/live/tuodominio.it` | `fullchain.pem`, `privkey.pem` |
| Altro (Caddy, Traefik, acme.sh, CA aziendale) | dove il programma salva i file | i nomi che usa lui |

Il numero di `npm-3` è diverso a ogni installazione: guarda dentro `live/` sulla macchina di NPM. Per sapere dove NPM tiene i dati:
`docker inspect NOME-DEL-CONTENITORE --format '{{ range .Mounts }}{{ .Source }} -> {{ .Destination }}{{ println }}{{ end }}'` (quello che finisce in `/etc/letsencrypt`).

### 5.3 Passo per passo: un utente che può solo leggere

La chiave privata è un segreto, quindi lo script non usa l'account di un amministratore: una chiave ssh rubata darebbe accesso completo alla macchina sorgente.
Si crea un utente separato che può solo leggere i certificati. Nell'esempio la macchina di NPM è `npm.lan`, quella di SpoolSteward `print.lan` (`192.168.1.50`), l'utente `certsync`.

**1 — su `npm.lan`: crea l'utente** (senza password).

```sh
sudo adduser --disabled-password --gecos "" certsync
sudo apt install rsync acl          # se mancano
```

**2 — su `print.lan`: crea la chiave ssh** (con l'utente che lancerà il cron; servono `rsync` e `ssh` anche qui).

```sh
ssh-keygen -t ed25519 -N "" -C "spoolsteward cert-sync" -f ~/.ssh/spoolsteward_certsync
cat ~/.ssh/spoolsteward_certsync.pub          # una riga che inizia con ssh-ed25519
```

La chiave privata non deve uscire da questa macchina; il suo percorso completo va in `CERT_SYNC_KEY`.

**3 — su `npm.lan`: autorizza la chiave, con dei limiti.** Usa l'IP di `print.lan` e la riga stampata al passo 2.

```sh
sudo -u certsync mkdir -p -m 700 /home/certsync/.ssh
echo 'restrict,from="192.168.1.50" ssh-ed25519 AAAA… spoolsteward cert-sync' | sudo -u certsync tee -a /home/certsync/.ssh/authorized_keys
sudo chmod 600 /home/certsync/.ssh/authorized_keys
```

`from=` fa funzionare la chiave **solo** da `print.lan`; `restrict` toglie tutto il resto (inoltro di porte, terminale, agente ssh).

**4 — su `npm.lan`: sola lettura sui certificati, nient'altro** (ACL POSIX; esempio per NPM con i dati in `/home/tu/npm/data`):

```sh
sudo setfacl -m u:certsync:x /home/tu /home/tu/npm /home/tu/npm/data          # attraversare le cartelle sopra
sudo setfacl -R -m u:certsync:rX /home/tu/npm/data/letsencrypt
sudo setfacl -R -d -m u:certsync:rX /home/tu/npm/data/letsencrypt              # vale anche per i file dei prossimi rinnovi
```

Per certbot: `sudo setfacl -R -m u:certsync:rX /etc/letsencrypt` (e la stessa con `-d`). Se il disco non supporta le ACL, usa un gruppo che può leggere la cartella e aggiungi `certsync` a quel gruppo.
Controlla:

```sh
sudo -u certsync head -1 /home/tu/npm/data/letsencrypt/live/npm-3/fullchain.pem     # -----BEGIN CERTIFICATE-----
sudo -u certsync touch /home/tu/npm/data/letsencrypt/live/npm-3/prova               # Permission denied
```

**5 — su `print.lan`: impostazioni in `.env`.**

```sh
CERT_SYNC_HOST=npm.lan
CERT_SYNC_USER=certsync
CERT_SYNC_KEY=/home/tu/.ssh/spoolsteward_certsync
CERT_SYNC_REMOTE_DIR=/home/tu/npm/data/letsencrypt/live/npm-3
```

**6 — su `print.lan`: prova,** prima senza cambiare nulla: `scripts/cert-sync.sh --dry-run`. La prima volta ssh ricorda l'impronta di `npm.lan` (in
`config/ssh/known_hosts`); se un giorno cambia, lo script si rifiuta di collegarsi. Scrive «a new certificate is available» oppure «already up to date». Poi la prova vera:
`scripts/cert-sync.sh`.

**7 — su `print.lan`: cron.** `crontab -e` con il tuo utente (deve poter usare `docker`) e aggiungi (cambia il percorso):

```
17 4 * * *  cd /home/tu/spoolsteward && scripts/cert-sync.sh >> data/cert-sync.log 2>&1
```

Una volta al giorno basta: con il certificato invariato non si installa e non si riavvia nulla.

### 5.4 Se qualcosa va storto

Il messaggio compare nella campanella e in `data/cert-sync.log`; per vederlo subito esegui `scripts/cert-sync.sh --dry-run`.

| Messaggio | Cosa significa e cosa fare |
|---|---|
| `set CERT_SYNC_HOST, CERT_SYNC_USER, CERT_SYNC_KEY and CERT_SYNC_REMOTE_DIR in .env` | Manca un'impostazione obbligatoria. |
| `the ssh key … does not exist or can not be read by this user` | `CERT_SYNC_KEY` è sbagliato (deve essere il percorso completo) o il file appartiene a un altro utente. |
| `could not download … Permission denied (publickey)` | La chiave pubblica non è in `authorized_keys` remoto, o `from=` non corrisponde all'IP di questa macchina. Rifai il passo 3. |
| `could not download … REMOTE HOST IDENTIFICATION HAS CHANGED` | L'impronta remota è cambiata (reinstallata, o qualcuno si finge lei). Se sai perché, cancella la sua riga da `config/ssh/known_hosts`. |
| `could not download … No such file or directory` | `CERT_SYNC_REMOTE_DIR` o i nomi dei file sono sbagliati (il numero `npm-3`, per esempio). |
| `could not download … Permission denied` (senza `publickey`) | L'utente remoto non può leggere: rifai il passo 4. |
| `the certificate on … is already expired: it was not installed` | La sorgente ha ancora un certificato scaduto: scopri perché non rinnova. |
| `the private key … does not belong to the certificate …: nothing was installed` | Il file della chiave e quello del certificato non sono la stessa coppia. I file in uso non sono stati toccati. |
| `the new certificate is installed but the gateway did not restart` | Esegui `docker compose restart gateway` a mano e leggi `docker compose logs gateway`. Se il cron non può usare docker, aggiungi il suo utente al gruppo `docker`. |

Per tornare alla coppia precedente: copia i due file di `config/tls/previous/` in `config/tls/` ed esegui `docker compose restart gateway`.

### 5.5 Cosa lo script non fa

- Non **rinnova** il certificato: lo copia da chi lo rinnova. Per accorgersi di un rinnovo fallito accendi l'avviso «sta per scadere» (§2).
- Non cambia i nomi nel certificato: se manca l'indirizzo annunciato, il certificato va riemesso dalla sorgente.
- Non tocca la CA dei dispositivi (§3).
- Non usa `rrsync`: rifiuta i collegamenti simbolici, e sia Nginx Proxy Manager sia certbot tengono in `live/` dei collegamenti. I limiti sono quelli dei passi 3 e 4.
