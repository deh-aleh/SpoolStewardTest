# Certificati TLS: avvisi e sincronizzazione

[Indice della documentazione](../README.md) · [English](../en/certificates.md) · Italiano

Il gateway serve IPPS e il portale solo su TLS. Questa pagina descrive i requisiti del certificato, gli avvisi che compaiono quando è
sbagliato e `scripts/cert-sync.sh`, che copia un certificato rinnovato da un'altra macchina (per esempio Nginx Proxy Manager).

## 1. Cosa verifica un client

Un client verifica tre cose prima di inviare qualunque dato; se una fallisce rifiuta la connessione o mostra un avviso:

1. **Catena**: il certificato è emesso da una CA di cui il dispositivo si fida (CA pubblica, oppure la CA aziendale installata sul dispositivo, §3).
2. **Nome**: l'host a cui il client si collega compare nel Subject Alternative Name (SAN) del certificato, come nome DNS o indirizzo IP.
3. **Validità**: `notAfter` è nel futuro.

L'annuncio (mDNS) pubblica l'indirizzo IP della route di default, oppure `SPOOLSTEWARD_MDNS_ADDRESSES`, e il nome host di `SPOOLSTEWARD_MDNS_HOSTNAME`
(predefinito: la parte host di `SPOOLSTEWARD_PORTAL_URL`, altrimenti il nome della macchina). I client che trovano la stampante via mDNS si collegano a
quell'indirizzo, che deve quindi essere nel SAN. Se l'indirizzo cambia (DHCP) il certificato non corrisponde più: assegna al server un indirizzo fisso.

**Browser e solo HTTPS (HSTS).** A ogni risposta HTTPS il portale invia `Strict-Transport-Security: max-age=<secondi>`
(`SPOOLSTEWARD_HSTS_SECONDS`; **spenta di default**, `0` = mai). Per accenderla imposta per esempio `2592000` (30 giorni) ed esegui `docker compose up -d`. Dice al browser di usare solo HTTPS con questo **nome host**. Come va a finire:

- **Un certificato di cui il browser non si fida** (l'autofirmato di `scripts/docker-init.sh`, con un avviso a cui passi oltre): il browser
  **ignora** l'intestazione, come richiede lo standard. Non cambia nulla.
- **Un certificato di cui il browser si fida** (un'autorità vera, o la CA aziendale installata sul computer, §3): il browser se la ricorda. Da quel momento
  un indirizzo scritto o linkato come `http://stampe.lan` diventa da solo `https://stampe.lan`, e **un errore di certificato non si può più scavalcare**.
  Esempio: Mario si fida della CA aziendale. Apre il portale; il certificato scade 20 giorni dopo e nessuno lo rinnova; il suo browser mostra «la
  connessione non è privata» **senza** il link «Avanzate – continua», quindi non riesce nemmeno a raggiungere il portale per leggere l'avviso. Appena il
  certificato è rinnovato torna a funzionare subito: il browser ricorda «niente scavalcamento», non l'errore.
- **Vale per nome host, non per porta.** Se lo stesso nome serve qualcos'altro solo in `http` semplice (una seconda pagina web dello stesso Raspberry su
  un'altra porta), quel browser la forza a `https` e smette di aprirsi.

Se l'hai accesa e vuoi spegnerla, imposta `SPOOLSTEWARD_HSTS_SECONDS=0` ed esegui `docker compose up -d`: i browser che già la ricordano tengono la regola finché il suo tempo non
è finito (al massimo 30 giorni), o finché non ricevono l'intestazione con `0` su una connessione HTTPS valida, o finché non si cancellano i dati del sito nel
browser. Con un certificato di un'autorità vera che si rinnova da solo puoi poi alzarla a `31536000` (un anno).

## 2. Avvisi nel portale (la campanella)

Il gateway non sostituisce mai il certificato. Legge `SPOOLSTEWARD_TLS_CERTFILE` e `config/tls/sync-status.json` ogni 6 ore e
apre o chiude avvisi per gli amministratori. Un avviso si chiude da solo quando la causa sparisce.

| Avviso (titolo nella campanella) | Condizione | Interruttore |
|---|---|---|
| **Il certificato TLS è scaduto** | `notAfter` è passato | sempre attivo |
| **Non è stato possibile aggiornare il certificato** | l'ultima esecuzione di `scripts/cert-sync.sh` ha scritto `"ok": false` in `sync-status.json` (il messaggio è nell'avviso) | compare solo se usi lo script |
| **L'indirizzo annunciato non è nel certificato TLS** | nessun indirizzo/nome annunciato (mDNS) è nel SAN; i caratteri jolly (`*.example.com`) coprono una sola etichetta | `SPOOLSTEWARD_CERT_NAME_CHECK=false` lo spegne |
| **Il certificato TLS scade il (data)** | mancano meno di `SPOOLSTEWARD_CERT_EXPIRY_WARN_DAYS` giorni | **spento di default**: `SPOOLSTEWARD_CERT_EXPIRY_WARN_ENABLED=true` |

L'avviso di scadenza aggiunge un suggerimento in base all'ultima sincronizzazione: se è riuscita, la sorgente non ha rinnovato (controlla il
programma che rinnova); se è fallita, rimanda all'altro avviso; se lo script non è usato, chiede di rinnovare e mettere i file in `config/tls`.

**Perché l'avviso di scadenza è spento di default.** Un certificato Let's Encrypt dura 90 giorni e viene rinnovato circa 30 giorni prima di
`notAfter`. Con la soglia a 14 giorni l'avviso compare solo se il rinnovo fallisce da più di due settimane. Sopra i 30 giorni scatterebbe a ogni
ciclo di rinnovo normale. I certificati di una CA aziendale durano spesso un anno e si rinnovano a mano: lì 30 giorni vanno bene.

Le impostazioni stanno in `.env`; si applicano con `docker compose up -d`.

## 3. Il file della CA: cosa farne

Con una CA aziendale, installa il **certificato della CA** (un file `.crt` o `.pem`; mai la chiave privata) una volta su ogni dispositivo. I
dispositivi si fidano poi di ogni certificato emesso da quella CA, compreso quello del gateway:

| Dispositivo | Cosa fare |
|---|---|
| Android | *Impostazioni → Sicurezza → Crittografia e credenziali → Installa un certificato → Certificato CA*, scegli il file. Il browser lo accetta; **alcune app ignorano le CA aggiunte dall'utente**: se un'app si rifiuta di collegarsi, usa un certificato di una CA pubblica (per esempio Let's Encrypt). |
| iPhone / iPad | Apri il file (da Mail o da File), *Impostazioni → Profilo scaricato → Installa*. Poi *Impostazioni → Generali → Info → Attendibilità certificati* e attiva l'interruttore della CA: senza questo secondo passaggio il certificato resta non attendibile. |
| Windows | Doppio clic sul file → *Installa certificato* → *Computer locale* → *Colloca tutti i certificati nel seguente archivio* → *Autorità di certificazione radice attendibili*. |
| macOS | Apri *Accesso Portachiavi*, trascina il file in *Sistema*, doppio clic sul certificato → *Attendibilità* → *Considera sempre attendibile*. |
| Linux (Debian, Ubuntu) | Copia il file in `/usr/local/share/ca-certificates/` con estensione `.crt`, poi `sudo update-ca-certificates`. Firefox ha un archivio suo: *Impostazioni → Privacy e sicurezza → Certificati → Mostra certificati → Autorità → Importa*. |

Se invece non hai una CA e usi il certificato autofirmato creato da `scripts/docker-init.sh`, il file da installare è `config/tls/fullchain.pem`, con gli stessi passaggi.

Con un certificato di una CA pubblica (Let's Encrypt) non c'è nulla da installare sui dispositivi: già si fidano.

## 4. Copiare il certificato a mano

Metti `fullchain.pem` (il certificato, con la catena) e `privkey.pem` (la chiave) in `config/tls/`, rendi la chiave leggibile solo a te
(`chmod 600 config/tls/privkey.pem`) e riavvia: `docker compose restart gateway`. La chiave deve essere leggibile dall'utente `PUID` di `.env`.

**Hai già un certificato valido (per esempio emesso da Nginx Proxy Manager su un'altra macchina).** Il modo più semplice è far usare quello stesso
certificato direttamente al gateway, senza proxy in mezzo:

1. Fai in modo che un nome coperto da quel certificato (diciamo `print.example.com`) risolva verso il server dentro la tua rete (record nel DNS
   locale o nel router).
2. Copia la coppia dalla macchina che la tiene (NPM: `data/letsencrypt/live/npm-<n>/fullchain.pem` e `privkey.pem`; certificati personalizzati:
   `data/custom_ssl/npm-<n>/`) in `config/tls/fullchain.pem` e `config/tls/privkey.pem`, rendi la chiave leggibile dall'utente `PUID`
   (`chmod 600`) ed esegui `docker compose restart gateway`.
3. Imposta `SPOOLSTEWARD_PORTAL_URL=https://print.example.com:8631` in `.env` e usa `ipps://print.example.com:8631/printers/<coda>`.

I dispositivi che trovano le stampanti con mDNS ricevono il nome `<nome>.local` o l'IP, che un certificato per `print.example.com` non copre:
su quei dispositivi usa a mano il nome vero, oppure ferma il servizio `mdns` se non ti serve la scoperta.

Un certificato Let's Encrypt si rinnova circa ogni 60 giorni; per quel caso usa lo script del §5.

### 4.1 Dietro un reverse proxy

Invece di dare il certificato al gateway, un reverse proxy può tenerlo e inoltrare a `https://<server>:8631` (non provato dal progetto).
Il proxy deve inoltrare con schema `https`, accettare corpi grandi e non metterli in buffer (`client_max_body_size 250m;
proxy_request_buffering off; proxy_read_timeout 600s;`). Il gateway vede ogni client con l'indirizzo del proxy (blocco dei login per
indirizzo, IP nell'audit) a meno che in `.env` sia impostato `FORWARDED_ALLOW_IPS=<indirizzo del proxy>`, così si fida di `X-Forwarded-For`.
Gli indirizzi delle stampanti restituiti ai client portano l'`Host` inoltrato dal proxy, senza `:443`, quindi i dispositivi devono tenere
l'indirizzo con cui sono stati configurati.

## 5. Tenere il certificato aggiornato da solo: `scripts/cert-sync.sh`

### 5.1 Come funziona

Eseguilo una volta a mano, poi da `cron`. A ogni esecuzione:

1. scarica i due file dalla macchina che rinnova i certificati (con `rsync` su `ssh`) in una cartella temporanea privata;
2. li controlla: il certificato non è scaduto e la chiave privata è proprio la sua;
3. se sono uguali a quelli in uso, **si ferma** (niente da fare, niente riavvii);
4. altrimenti mette da parte la coppia vecchia in `config/tls/previous/`, installa la nuova e riavvia il gateway;
5. scrive com'è andata in `config/tls/sync-status.json`: se è andata male, il portale mostra l'avviso «l'ultimo aggiornamento del certificato è fallito».

Se qualcosa non va nei controlli, **i file in uso non vengono toccati**: un certificato sbagliato non può rompere un server che funziona.

### 5.2 Le impostazioni (in `.env`)

| Impostazione | Cosa | Predefinito |
|---|---|---|
| `CERT_SYNC_HOST` | La macchina che ha il certificato (nome o indirizzo) | obbligatoria |
| `CERT_SYNC_USER` | L'utente con cui ci si collega (quello di sola lettura del §5.3) | obbligatoria |
| `CERT_SYNC_KEY` | Percorso **completo** della chiave privata ssh di questa macchina | obbligatoria |
| `CERT_SYNC_REMOTE_DIR` | Percorso **completo**, sulla macchina remota, della cartella che contiene i due file | obbligatoria |
| `CERT_SYNC_PORT` | Porta ssh | `22` |
| `CERT_SYNC_CERT_FILE` | Nome del file del certificato (con la catena) | `fullchain.pem` |
| `CERT_SYNC_KEY_FILE` | Nome del file della chiave | `privkey.pem` |

Il percorso e i nomi cambiano con il programma che rinnova i certificati, ed è per questo che si possono scegliere:

| Programma | `CERT_SYNC_REMOTE_DIR` (esempio) | File |
|---|---|---|
| Nginx Proxy Manager, certificato Let's Encrypt | `/home/tu/npm/data/letsencrypt/live/npm-3` | `fullchain.pem`, `privkey.pem` |
| Nginx Proxy Manager, certificato personalizzato | `/home/tu/npm/data/custom_ssl/npm-3` | `fullchain.pem`, `privkey.pem` |
| certbot | `/etc/letsencrypt/live/tuodominio.it` | `fullchain.pem`, `privkey.pem` |
| Altro (Caddy, Traefik, acme.sh, una CA aziendale) | la cartella dove il programma salva i file | i nomi che usa lui, per esempio `cert.pem` e `key.pem` |

Il numero di `npm-3` è diverso da installazione a installazione: guarda quale cartella c'è dentro `live/` sulla macchina di NPM. Per sapere dove NPM tiene i dati:
`docker inspect NOME-DEL-CONTENITORE --format '{{ range .Mounts }}{{ .Source }} -> {{ .Destination }}{{ println }}{{ end }}'` (cerca quello che finisce in `/etc/letsencrypt`).

### 5.3 Passo per passo: un utente che può solo leggere

La chiave privata è un segreto, quindi lo script non usa l'account di un amministratore: una chiave ssh rubata darebbe accesso completo alla macchina sorgente.
Si crea un utente separato che può soltanto leggere i certificati.

Nell'esempio la macchina di NPM si chiama `npm.lan`, quella di SpoolSteward `print.lan` (IP `192.168.1.50`), l'utente si chiama `certsync`.

**Passo 1 — su `npm.lan`: crea l'utente** (senza password: ci si entra solo con la chiave).

```sh
sudo adduser --disabled-password --gecos "" certsync
sudo apt install rsync acl          # se mancano: rsync serve a copiare, acl per i permessi del passo 4
```

**Passo 2 — su `print.lan`: crea la chiave ssh** (con il tuo utente, quello che lancerà il cron).
Servono `rsync` e `ssh` anche su questa macchina (`sudo apt install rsync openssh-client` se mancano).

```sh
ssh-keygen -t ed25519 -N "" -C "spoolsteward cert-sync" -f ~/.ssh/spoolsteward_certsync
cat ~/.ssh/spoolsteward_certsync.pub          # una riga che inizia con ssh-ed25519: la useremo al passo 3
```

La chiave privata (`spoolsteward_certsync`, senza `.pub`) non deve uscire da questa macchina. Il percorso completo (per esempio `/home/tu/.ssh/spoolsteward_certsync`)
andrà in `CERT_SYNC_KEY`.

**Passo 3 — su `npm.lan`: autorizza la chiave, con dei limiti.** Sostituisci `192.168.1.50` con l'IP di `print.lan` e la parte `ssh-ed25519 AAAA…` con la riga stampata al passo 2.

```sh
sudo -u certsync mkdir -p -m 700 /home/certsync/.ssh
echo 'restrict,from="192.168.1.50" ssh-ed25519 AAAA… spoolsteward cert-sync' | sudo -u certsync tee -a /home/certsync/.ssh/authorized_keys
sudo chmod 600 /home/certsync/.ssh/authorized_keys
```

Le due parole davanti alla chiave sono i limiti: `from="192.168.1.50"` fa funzionare la chiave **solo** da `print.lan`; `restrict` toglie tutto il resto
(niente inoltro di porte, niente terminale interattivo, niente agente ssh).

**Passo 4 — su `npm.lan`: dai all'utente il permesso di leggere i certificati, e nient'altro.** Con le ACL POSIX si concede la sola lettura di una cartella.
Esempio per NPM, con i dati in `/home/tu/npm/data`:

```sh
# l'utente deve poter "attraversare" le cartelle sopra (x) e leggere (r) quelle dei certificati
sudo setfacl -m u:certsync:x /home/tu /home/tu/npm /home/tu/npm/data
sudo setfacl -R -m u:certsync:rX /home/tu/npm/data/letsencrypt
sudo setfacl -R -d -m u:certsync:rX /home/tu/npm/data/letsencrypt    # vale anche per i file creati ai prossimi rinnovi
```

Per certbot: `sudo setfacl -R -m u:certsync:rX /etc/letsencrypt` (e la stessa riga con `-d`).

Se il tuo disco non supporta le ACL (`setfacl` risponde «Operation not supported»), l'alternativa è creare un gruppo che può leggere la cartella e aggiungere `certsync` a quel gruppo.

Controlla che funzioni e che sia davvero di sola lettura:

```sh
sudo -u certsync head -1 /home/tu/npm/data/letsencrypt/live/npm-3/fullchain.pem     # deve stampare -----BEGIN CERTIFICATE-----
sudo -u certsync touch /home/tu/npm/data/letsencrypt/live/npm-3/prova               # deve rispondere: Permission denied
```

**Passo 5 — su `print.lan`: scrivi le impostazioni in `.env`.**

```sh
CERT_SYNC_HOST=npm.lan
CERT_SYNC_USER=certsync
CERT_SYNC_KEY=/home/tu/.ssh/spoolsteward_certsync
CERT_SYNC_REMOTE_DIR=/home/tu/npm/data/letsencrypt/live/npm-3
```

**Passo 6 — su `print.lan`: prova.** Prima senza cambiare nulla:

```sh
scripts/cert-sync.sh --dry-run
```

La prima volta ssh ricorda l'impronta di `npm.lan` (nel file `config/ssh/known_hosts`) e dalle volte successive la controlla: se un giorno quell'impronta cambia, lo script
si rifiuta di collegarsi (è il segnale che qualcuno si sta spacciando per `npm.lan`). Se tutto va bene scrive «a new certificate is available» oppure «already up to date».
Poi la prova vera: `scripts/cert-sync.sh`. Apri il portale: nessun avviso sul certificato, e se il certificato era cambiato il gateway si è riavviato.

**Passo 7 — su `print.lan`: mettilo in cron.** Con il tuo utente (deve poter usare `docker`):

```sh
crontab -e
```

e aggiungi una riga (cambia il percorso della cartella di SpoolSteward):

```
17 4 * * *  cd /home/tu/spoolsteward && scripts/cert-sync.sh >> data/cert-sync.log 2>&1
```

Esegue ogni giorno alle 4:17 e aggiunge l'output a `data/cert-sync.log`. Una volta al giorno basta: se il certificato non è cambiato non si installa nulla e non si riavvia nulla.

### 5.4 Se qualcosa va storto

Il messaggio compare nel portale (campanella) e in `data/cert-sync.log`. Per vederlo subito: `scripts/cert-sync.sh --dry-run`.

| Messaggio | Cosa significa e cosa fare |
|---|---|
| `set CERT_SYNC_HOST, CERT_SYNC_USER, CERT_SYNC_KEY and CERT_SYNC_REMOTE_DIR in .env` | Manca una delle quattro impostazioni obbligatorie. |
| `the ssh key … does not exist or can not be read by this user` | Il percorso in `CERT_SYNC_KEY` è sbagliato o il file appartiene a un altro utente. Deve essere il percorso completo. |
| `could not download … Permission denied (publickey)` | La chiave pubblica non è in `authorized_keys` dell'utente remoto, o l'`from=` non corrisponde all'IP di questa macchina. Rifai il passo 3. |
| `could not download … REMOTE HOST IDENTIFICATION HAS CHANGED` | L'impronta della macchina remota è cambiata (reinstallata, o qualcuno si finge lei). Se sai perché, cancella la sua riga da `config/ssh/known_hosts`. |
| `could not download … No such file or directory` | `CERT_SYNC_REMOTE_DIR` o i nomi dei file sono sbagliati (il numero `npm-3`, per esempio). |
| `could not download … Permission denied` (senza `publickey`) | L'utente remoto non ha il permesso di leggere: rifai il passo 4. |
| `the certificate on … is already expired: it was not installed` | La sorgente ha ancora un certificato scaduto: il problema è lì, controlla perché non rinnova. |
| `the private key … does not belong to the certificate …: nothing was installed` | Il file della chiave e quello del certificato non sono la stessa coppia (nomi sbagliati, o rinnovo a metà). I file in uso non sono stati toccati. |
| `the new certificate is installed but the gateway did not restart` | Il certificato nuovo è al suo posto, ma `docker compose restart gateway` è fallito: lancialo a mano e guarda `docker compose logs gateway`. Se il cron non può usare docker, aggiungi il suo utente al gruppo `docker`. |

Per tornare alla coppia precedente: copia i due file di `config/tls/previous/` in `config/tls/` e `docker compose restart gateway`.

### 5.5 Cosa lo script non fa

- Non **rinnova** il certificato: lo copia da chi lo rinnova. Se il rinnovo fallisce alla sorgente, qui arriva sempre il vecchio; per accorgersene accendi l'avviso «sta per scadere» (§2).
- Non cambia i nomi nel certificato: se manca l'indirizzo che il gateway annuncia, l'avviso lo dice, ma il certificato va riemesso dalla sorgente.
- Non tocca la CA dei dispositivi (§3).
- Non usa `rrsync` per restringere ulteriormente la chiave: `rrsync` rifiuta di seguire i collegamenti simbolici, e sia Nginx Proxy Manager sia certbot tengono in `live/` dei collegamenti verso `archive/`.
  I limiti qui sono quelli del passo 3 (`restrict`, `from=`) e del passo 4 (sola lettura solo sui certificati).
