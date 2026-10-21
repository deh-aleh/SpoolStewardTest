# Architettura

[Indice della documentazione](../README.md) · [English](../en/architecture.md) · Italiano

Componenti, confini di fiducia e percorso di una richiesta. Per il ruolo di ogni file sorgente vedi [funzionamento-applicazione.md](funzionamento-applicazione.md).

## 1. L'idea

CUPS stampa, ma non sa *chi* può stampare *quanto*. SpoolSteward è uno strato posto davanti a CUPS che aggiunge autenticazione,
quote, registro delle attività (audit) e un portale web, **senza modificare CUPS, ipp-usb o il firmware delle stampanti**:
restano semplici dipendenze, aggiornabili in modo indipendente.

Principio cardine: **il gateway è l'unico punto d'ingresso**. CUPS e la stampante non hanno porte pubblicate e il nome utente
che CUPS vede non viene mai dal client: lo decide il gateway dopo l'autenticazione.

```
                            ┌──────────────────────────────────────────┐
    client IPP  ──ipps──▶   │ GATEWAY (container, porta 8631)          │
    (PC, Mac, …)  TLS +     │  • TLS + HTTP Basic (nome + chiave)      │ ──▶ RADIUS (facoltativo)
    Basic auth              │  • identità imposta dal gateway          │
                            │  • quote/concessioni + audit  ◀──▶  DB   │
    browser  ───https────▶  │  • portale: /login /admin /me /docs      │
                            └──────────────┬───────────────────────────┘
                                           │ socket unix (volume condiviso)
                            ┌──────────────▼───────────────┐
                            │ CUPS 2.4 (container)         │  nessuna porta TCP
                            └──────────────┬───────────────┘
                                           │ rete interna docker
                            ┌──────────────▼───────────────┐
                            │ ipp-usb (container)          │  /dev/bus/usb
                            └──────────────┬───────────────┘
                                           │ USB
                                   stampante / scanner

   mdns (container, rete host): legge l'elenco delle code e lo annuncia sulla LAN (DNS-SD / AirPrint / Mopria)
```

Differenze consapevoli rispetto a uno schema "classico":

- il motore delle policy e il database stanno **dentro** il gateway, non sono servizi separati;
- la scansione passa per **eSCL** (ipp-usb lo espone già sulla stessa porta di IPP), non per SANE;
- il gateway IPP è un vero **reverse proxy che decodifica IPP**, non un filtro o backend di CUPS (che non riceverebbe un'identità affidabile).

## 2. Tecnologie e perché

| Componente | Scelta | Perché |
|---|---|---|
| Linguaggio | **Python 3.12+** | Facile da debuggare, molte librerie; il codec IPP è isolato e sostituibile |
| Web / proxy | **FastAPI + uvicorn**, **httpx** (asincrono, anche su socket unix) | Inoltra i documenti in streaming, senza caricarli in memoria |
| Database | **SQLAlchemy 2** + **SQLite** (WAL), migrazioni **Alembic** | Zero configurazione; un file solo, nessun server da gestire |
| Password locali | **argon2id** (`argon2-cffi`) | Segreti scelti da persone: hash lento |
| Chiavi di stampa | 192 bit casuali, **sha256** | Alta entropia: un hash veloce basta e non appesantisce ogni richiesta IPP |
| RADIUS | **pyrad** (PAP + Message-Authenticator) | Il server RADIUS esiste già in ufficio |
| Portale | Pagine **Jinja2** generate dal server, sessioni firmate (**itsdangerous**) | HTML dal server; JavaScript solo per piccoli aiuti (copia, suggerimenti, popup, aggiornamento automatico) |
| Traduzioni | **Babel** (cataloghi gettext `.po` letti in memoria) | Nessuna compilazione; vedi [lingue.md](lingue.md) |
| Configurazione | **pydantic-settings** (variabili `SPOOLSTEWARD_*`) | Validazione e valori predefiniti |
| Backend di stampa | **CUPS 2.4** | Dipendenza non modificata; filtri e driver già pronti |
| Ponte USB | **ipp-usb** | Espone la stampante USB come IPP/eSCL |
| Conteggio pagine | **pypdf** (+ intestazioni di immagini, URF, PostScript) | Il limite pagine deve valere anche per un file da 200 pagine |
| Documentazione nel portale | **markdown-it-py** | Mostra le pagine Markdown di `docs/` con l'HTML grezzo disattivato; i file restano leggibili su GitHub così come sono |
| PDF del report di utilizzo | **reportlab** | Tabelle con interruzioni di pagina e intestazione ripetuta, senza browser né programmi esterni |
| Backup | **cryptography** (X25519, HKDF, AES-256-GCM, scrypt) | Cifrati con una chiave di recupero, passphrase mai conservata |
| Annuncio sulla rete | **python-zeroconf** (servizio `mdns`, rete host) | Il multicast non attraversa la rete bridge di docker |
| Contenitori | **Docker Compose** | Nulla installato sull'host; test riproducibili |
| Test | **pytest** (+ asyncio); CUPS e FreeRADIUS reali in docker | Molti test girano contro servizi veri |

## 3. I contenitori

Le immagini sono due. `spoolsteward` contiene l'applicazione, CUPS e ipp-usb: i servizi `ipp-usb`, `cups`, `gateway` e `mdns` la avviano
tutti, ognuno con il suo comando, quindi hanno sempre la stessa versione. `spoolsteward-updater` è l'updater facoltativo.

| Servizio | Ruolo | Esposizione |
|---|---|---|
| `ipp-usb` | Unico proprietario di `/dev/bus/usb`; trasforma ogni dispositivo USB in un endpoint IPP + eSCL. Uno script lo riavvia quando cambia l'elenco USB (gli eventi udev non arrivano nei container). | nessuna (rete interna) |
| `cups` | Backend di stampa. Le code nascono all'avvio da `CUPS_QUEUES` e, per le stampanti USB, dalla scoperta automatica del gateway; configurazione persistente; solo socket unix. Vede `/dev/bus/usb` per le stampanti che ipp-usb non sa servire (serve un driver). | nessuna |
| `gateway` | L'applicazione: proxy IPP, proxy eSCL, portale, scheduler. Utente non root (`PUID:PGID`), filesystem in sola lettura, nessuna capability. | **porta 8631** (l'unica) |
| `mdns` | Annuncia code e scanner sulla LAN. Stessa immagine del portal, rete host. Chiede al gateway cosa annunciare (autenticato da un codice derivato da `SPOOLSTEWARD_SECRET_KEY`). | multicast |
| `radius` | RADIUS di prova, solo con `--profile radius-test` | interna |

Dati: un volume docker per il database (`/data`), la cartella `data/audit-archive` sull'host per l'audit compresso, la cartella
`data/backups` sull'host per i backup, `config/` (in sola lettura) per il certificato TLS, e `.env` (in sola lettura, per i backup cifrati).

## 4. Percorso di una richiesta IPP

1. Solo `application/ipp`, solo HTTPS.
2. Le **query di capacità** (`Get-Printer-Attributes` e `Validate-Job`, corpo dichiarato ≤ 64 KB, `SPOOLSTEWARD_ANONYMOUS_DISCOVERY`) ricevono risposta
   senza credenziali: i client le fanno prima di avere una password. Non creano utenti né righe di audit e non portano alcun documento.
   `Validate-Job` anonimo non viene controllato sulla quota.
3. **Autenticazione**: HTTP Basic, nome utente + **chiave di stampa**. La password di dominio non è accettata
   (`SPOOLSTEWARD_IPP_AUTH=keys`); `keys+radius` la riammette per gli utenti RADIUS durante una transizione, mai per gli account locali.
4. **Filtro operazioni**: l'utente può stampare, annullare, vedere job e stampanti; le operazioni amministrative di CUPS e
   `Print-URI` / `Send-URI` (rischio SSRF) sono vietate a tutti.
5. **Identità**: `requesting-user-name` è sovrascritto con `gw:<utente>`. Il prefisso impedisce che un nome come `root`
   coincida con un amministratore di CUPS.
6. **Coerenza**: il `printer-uri` nel corpo deve coincidere con il percorso HTTP.
7. **Blocchi e manutenzione**: una stampante bloccata o la manutenzione del gateway rifiutano il job (vedi [stampanti.md](stampanti.md)).
8. **Permessi** (colore, fronte singolo, stampante, orari): in modalità `adjust` `print-color-mode` / `sides` sono riscritti prima di CUPS, altrimenti
   il job è rifiutato; gli elenchi di stampanti sono filtrati ([quote.md](quote.md)). L'amministratore di emergenza è esente.
9. **Quota**: riserva atomica del posto ([quote.md](quote.md)); se superata, errore IPP con il motivo **e l'indirizzo del portale**
   per chiedere una deroga.
10. **Inoltro** a CUPS in streaming (tetto `SPOOLSTEWARD_MAX_REQUEST_MB`, default 200); gli URI interni vengono riscritti, `device-uri`
   nascosto, i rifiuti HTML di CUPS trasformati in errori IPP puliti.
11. **Registrazione** dell'esito; se l'upload si interrompe la riga passa a `failed` e il posto si libera.
12. In seguito il **riconciliatore** sostituisce la stima delle pagine con quelle realmente stampate e libera la quota dei job mai stampati. Un job di CUPS è associato alla sua riga di audit dal numero del job *e* da `time-at-creation` (CUPS riparte da 1 se il suo volume viene ricreato).

La stampa dal browser (`/me/print`) passa dagli stessi controlli (quota, audit, identità `gw:<utente>`); cambia solo
l'autenticazione, che è la sessione del portale.

## 5. Attività in background

Tutte avviate dal ciclo di vita del gateway ([`spoolsteward/main.py`](../../spoolsteward/main.py)):

| Attività | Intervallo | Cosa fa |
|---|---|---|
| Riconciliatore | `SPOOLSTEWARD_RECONCILE_INTERVAL` (30 s) | Allinea `job_log` con CUPS: pagine reali, quota restituita ai job annullati, riserve orfane liberate dopo 1 h |
| Archiviatore audit | `SPOOLSTEWARD_AUDIT_CHECK_INTERVAL` (1 h) | Sposta le righe vecchie in file compressi verificati quando le tabelle superano `SPOOLSTEWARD_AUDIT_MAX_MB` |
| Scheduler dei backup | 60 s | Esegue i backup in scadenza |
| Scheduler della stampa periodica | 60 s | Stampa le pagine di manutenzione e annota la prima volta che ogni coda è stata vista |
| Scoperta delle stampanti | `SPOOLSTEWARD_DISCOVERY_INTERVAL` (30 s) | Crea le code delle stampanti USB nuove (prima ipp-usb, poi backend usb di CUPS) e toglie le code USB irraggiungibili da `SPOOLSTEWARD_OFFLINE_REMOVE_HOURS` |
| Annunciatore mDNS | `SPOOLSTEWARD_MDNS_INTERVAL` (30 s) | Contenitore a parte: rilegge l'elenco delle code e dice al gateway quale indirizzo annuncia |
| Nomi dei file ([`spoolsteward/titles.py`](../../spoolsteward/titles.py)) | all'avvio, poi 1 giorno | Cifra i nomi scritti prima che ci fosse la chiave; con la retention accesa cancella i nomi più vecchi di `SPOOLSTEWARD_JOB_TITLE_RETENTION_DAYS` |
| Controllo del certificato ([`spoolsteward/certwatch.py`](../../spoolsteward/certwatch.py)) | 6 h | Legge il certificato TLS e `config/tls/sync-status.json`, e apre o chiude gli avvisi sul certificato |
| Controllo stampanti ([`spoolsteward/printerwatch.py`](../../spoolsteward/printerwatch.py)) | 1 min | Inceppamento, carta finita, inchiostro finito/in esaurimento come avvisi (confermati da due controlli consecutivi) |
| Manutenzione delle chiavi ([`spoolsteward/keywatch.py`](../../spoolsteward/keywatch.py)) | 1 h | Elimina le chiavi di stampa concluse, avvisa di quelle in scadenza e dei permessi temporanei terminati |
| Controllo aggiornamenti ([`spoolsteward/updates.py`](../../spoolsteward/updates.py)) | 24 h (la prima volta 2 min dopo l'avvio) | Chiede a GitHub l'ultima release (`SPOOLSTEWARD_UPDATE_CHECK`) |

## 6. Modello dei dati

Tabelle (Alembic applica le migrazioni all'avvio; un test fallisce se un modello cambia senza migrazione):

| Tabella | Contenuto |
|---|---|
| `user`, `group` | Persone e gruppi. Ogni utente ha un `provider` (`local` o un provider delegato come RADIUS), un gruppo, un tema e una lingua |
| `policy_rule` | Limite per un utente o un gruppo: risorsa, metrica (**job** o **pagine**), quantità, periodo (settimanale / mensile) |
| `job_log` | Audit di stampe e scansioni: utente, dispositivo, nome file, esito, pagine, motivo, opzioni |
| `print_key` | Chiavi di stampa personali (solo l'hash), scadenza, revoca, ultimo uso |
| `quota_request`, `quota_grant` | Richieste degli utenti (quota, gruppo, permesso) e concessioni di quota prodotte dalle approvazioni |
| `permission_rule`, `permission_grant` | Colore / solo fronte / stampanti / orari di un gruppo o utente, e permessi aggiunti a un utente dopo una richiesta (permanenti o fino a una data) |
| `admin_audit` | Chi ha fatto cosa nel portale |
| `audit_archive` | Registro dei file di audit compressi (percorso, intervallo, righe, sha256) |
| `printer_control` | Blocco e nascondimento mDNS per coda |
| `printer_keepalive` | Impostazioni della stampa periodica, una riga per coda |
| `printer_seen` | Prima volta che ogni coda è stata vista (ordine predefinito dell'elenco) |
| `printer_order` | Dove ogni amministratore ha messo una stampante nel proprio elenco (frecce su/giù) |
| `printer_offline` | Da quando una coda USB non risponde (conto alla rovescia per la rimozione automatica) |
| `printer_former` | Nomi passati delle code, proposti nella finestra di rinomina |
| `notice` | La campanella: avvisi per gli amministratori e per i singoli utenti |
| `gateway_setting` | Modalità manutenzione, id dell'istanza, interruttore dei nomi dei file, impronta della chiave segreta |
| `backup_target` | Destinazioni dei backup, una per amministratore oppure quella generale |

## 7. Decisioni di progetto da conoscere

- **I nomi utente sono unici fra tutte le origini.** A CUPS e all'audit arriva solo `gw:<nome>`: due persone con lo stesso nome in
  origini diverse condividerebbero identità e quota. Il metodo di accesso lo decide la *riga*, non chi risponde "sì": un account
  locale non può mai essere aperto dal RADIUS.
- **Chiavi di stampa, non password di dominio, sui dispositivi.** I sistemi operativi salvano le credenziali di stampa in chiaro. Una
  chiave è casuale, per dispositivo, revocabile e valida solo per stampare; non apre mai il portale.
- **Le concessioni si sommano, non riscrivono lo storico.** `limite effettivo = regola + concessioni attive`, calcolato dentro lo
  stesso controllo sotto lock che riserva la quota. `job_log` non viene mai modificato per dare più spazio a qualcuno.
- **L'audit si aggiunge soltanto e si conserva per sempre.** Nessuna funzione del portale cancella righe o archivi; solo l'archiviatore
  sposta le righe, e solo dopo aver riletto e verificato la copia compressa.
- **Riserva atomica.** Il controllo del limite e l'inserimento avvengono sotto lock (thread + `BEGIN IMMEDIATE` su SQLite): job simultanei non possono superare il limite.
- **Nessuna ipotesi sull'hardware.** Stampanti, colore, inchiostro, contatori, scanner e formati carta si leggono dai dispositivi a
  runtime. I contatori del costruttore vengono dal servizio web HP quando c'è; le altre marche mostrano solo i dati IPP.
- **Fail closed.** RADIUS irraggiungibile ⇒ accesso negato; niente TLS ⇒ il gateway non parte; RADIUS configurato a metà ⇒ non parte.
- **L'inglese è la lingua dei sorgenti** di ogni testo visibile; le altre lingue sono cataloghi ([lingue.md](lingue.md)).

## 8. Sicurezza in breve

- CUPS non è raggiungibile dalla rete né da altri utenti locali; l'utente verso CUPS è `gw:<utente>`, quindi nessun nome RADIUS
  (per esempio `root`) può diventare amministratore CUPS. Le operazioni amministrative IPP sono bloccate.
- TLS obbligatorio. Credenziali mai in chiaro su HTTP.
- Disabilitare un utente nel portale blocca subito login e chiavi. Chi è stato tolto solo dal server RADIUS continua a stampare con le chiavi già create finché non scadono.
- Le chiavi si salvano solo come hash, si mostrano una volta e non aprono il portale; le password locali sono argon2id (minimo 12 caratteri).
- Blocco dei login: 5 password *diverse* sbagliate in 5 minuti per coppia (utente, IP), più un tetto per IP. Lo stesso errore ripetuto
  conta una volta (libcups riprova) e un estraneo non può bloccare un altro utente.
- Portale: sessioni firmate di 8 ore, token CSRF su ogni POST, cookie `Secure` + `SameSite=strict`, HTML sempre escapato, CSV protetto
  da iniezione di formule, pagine con segreti con `Cache-Control: no-store`.
- Il database **non è cifrato** e contiene i nomi dei file stampati: proteggilo come dato personale.

## 9. Qualità

I test automatici coprono il codec (andata e ritorno su risposte reali), le finestre delle policy (anche attraverso l'ora legale),
la concorrenza (16 connessioni indipendenti non superano un limite, due admin concedono una volta sola), l'autenticazione con un
RADIUS vero, il gateway contro un CUPS vero, il portale in ogni lingua, le migrazioni (compreso un database pre-Alembic con dati),
chiavi, richieste e concessioni, archiviazione e interruzioni. Verifiche di mutazione sulle regole di sicurezza: togliendo un
controllo almeno un test diventa rosso.
