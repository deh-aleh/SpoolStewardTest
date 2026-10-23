<picture>
  <source media="(prefers-color-scheme: dark)" srcset="spoolsteward/admin/static/icon-light.svg">
  <img src="spoolsteward/admin/static/icon-dark.svg" alt="Icona di SpoolSteward" width="96" align="right">
</picture>

# SpoolSteward

[English](README.md) · **Italiano**

> [!NOTE]
> **Beta (0.x):** gli aggiornamenti possono contenere modifiche incompatibili. Leggi l'[avviso prima di installare](#installazione-docker).

Su una rete condivisa, una stampante lascia stampare chiunque quanto vuole e poi nessuno sa chi ha stampato cosa. SpoolSteward si mette tra le persone e la stampante: ognuno accede con il proprio account, ogni persona o gruppo ha un limite (pagine, colore, solo fronte) e ogni stampa e scansione viene registrata. Serve a case, uffici e scuole che condividono una stampante e vogliono decidere chi stampa quanto e vedere cosa è stato stampato.
Per saperne di più: [docs/it/architettura.md](docs/it/architettura.md)

## Cosa fa

- **Account**: locali (argon2id) e/o RADIUS; **chiavi di stampa** personali e revocabili, così la password di dominio non resta mai su un PC o un telefono.
- **Quote e permessi**: job e pagine per settimana o mese, colore, stampa solo fronte, quali stampanti e in quali orari, per gruppo con override per utente; gli utenti chiedono di più, gli amministratori approvano.
- **Stampa e scansione**: IPP/IPPS per i computer, stampa dal browser per qualunque telefono, annuncio sulla LAN (AirPrint/Mopria), eSCL attraverso il gateway e dal portale.
- **Audit e backup**: chi ha stampato/scansionato cosa (pagine reali), conservato per sempre con archivi compressi. Backup programmati per amministratore, cifrati con una chiave di recupero, ripristinabili da riga di comando.
- **Stampanti**: stato, inchiostro e contatori, blocco/nascondimento, stampanti di prova, stampa periodica di manutenzione e stampa di prova per le fisiche.
- **Portale in 5 lingue**: inglese, italiano, spagnolo, russo, cinese semplificato; scelta per utente. La documentazione (inglese e italiano) si legge dentro il portale.

## Stato

**Provato e funzionante:** stampa attraverso il gateway (IPPS, chiavi di stampa, stampa dal browser), quote e deroghe, permessi per gruppo/utente (colore, solo fronte, stampanti, orari; verificati sulle due stampanti qui sotto), audit e archivi, scansione (eSCL, portale, finestra di scansione), pagina Stampanti, backup programmati (anche su una condivisione SMB), stack docker con script di aggiornamento e pulsante Aggiorna del portale (amd64; arm64 su un Raspberry Pi 4B), campanella degli avvisi.

**Provato solo in parte:**

- Stampanti: finora due via USB, una HP Smart Tank 7300 (tramite ipp-usb) e una Brother DCP-7055 (backend usb di CUPS, driver brlaser: trovata, coda creata e prima stampa riuscita senza alcuna impostazione). Qualunque altra stampante supportata da CUPS dovrebbe funzionare, ma non è stata provata; vale anche per i permessi, verificati solo su queste due.
- Annuncio sulla LAN (DNS-SD): visto da Android con Mopria; iPhone non provato.
- `scripts/cert-sync.sh`: provato con simulazioni, non ancora con un vero Nginx Proxy Manager.
- Traduzioni in spagnolo, russo e cinese: non riviste da madrelingua.

**Non ancora fatto:** autenticazione a due fattori per gli admin; stampa trattenuta con rilascio successivo (pull printing); client eSCL nativi che sanno autenticarsi; notifiche email/webhook per le richieste in attesa.

Il dettaglio di ogni area è nella [documentazione](#documentazione).

## Limiti

L'elenco completo è in [docs/it/limiti.md](docs/it/limiti.md). Quelli da conoscere prima di iniziare:

- **Il limite di pagine è morbido** per i file di cui non si possono contare le pagine (formato sconosciuto, PDF cifrato, oltre 64 MB): il job che supera il limite *viene stampato* e le sue pagine reali bloccano poi quelli successivi.
- **Serve un host Linux** per le stampanti USB e l'annuncio mDNS (Docker Desktop su Windows/macOS: solo stampanti di rete, senza rilevamento).
- **Il servizio di stampa di sistema di Android non sa autenticarsi**: usa Mopria Print Service o stampa dal browser. Windows 11 25H2 è provato ([windows.md](docs/it/windows.md)); iPhone e macOS non sono provati.
- **Un gateway per ogni stampante USB** e **uno per ogni database SQLite** (nessuna alta disponibilità).
- **Il TLS è obbligatorio**: installa il certificato autofirmato sui client o usa quello della tua CA.
- I **nomi dei file** stampati sono cifrati nel database (chiave da `SPOOLSTEWARD_SECRET_KEY`: non cambiarla); il resto no. Vedi [protezione-dati.md](docs/it/protezione-dati.md).

## Installazione (Docker)

> [!WARNING]
> **SpoolSteward è in beta (versioni 0.x).** Un aggiornamento può contenere **modifiche incompatibili**
> (*breaking changes*): impostazioni di `.env` rinominate o tolte, modifiche al database che una versione precedente non sa leggere,
> una procedura di installazione o di aggiornamento diversa. Ogni release le elenca nelle sue note su GitHub. Leggi le note prima di
> aggiornare, tieni un backup (`scripts/update.sh` ne fa uno prima di ogni aggiornamento) e non usare una beta su un server da cui
> dipendono altre persone senza essere pronto a ripristinarlo.

Requisiti: un server **Linux**, Docker Engine con Compose, `openssl`, `curl`, `tar`, `python3`, una stampante USB o di rete e una porta TCP
raggiungibile dai client (predefinita 8631). Sull'host non si installa nulla.

```sh
# 0. scarica il pacchetto della release (ultima versione: https://github.com/deh-aleh/SpoolSteward/releases)
VERSION=w.x.y #w.x.y-beta.z se vuoi installare una versione in beta
curl -LO https://github.com/deh-aleh/SpoolSteward/releases/download/v$VERSION/spoolsteward-$VERSION.tar.gz
tar xzf spoolsteward-$VERSION.tar.gz && cd spoolsteward-$VERSION      # (nella guida c'è anche come controllare il download)

# 1. nomi / IP con cui i client raggiungeranno il server (finiscono nel certificato TLS)
scripts/docker-init.sh print.lan 192.168.1.50      # crea .env (tutte le impostazioni), config/tls/, data/

# 2. impostazioni: RADIUS è facoltativo (scrivi none in host e secret per usare solo account locali)
$EDITOR .env

# 3. stampanti: quelle USB ricevono la coda da sole (prima ferma ipp-usb sull'host, vedi la guida);
#    una stampante di rete va in .env:  CUPS_QUEUES="UFFICIO=ipp://192.168.1.60/ipp/print"
docker compose up -d

# 4. leggi il codice monouso e scegli la password dell'amministratore "administrator"
docker compose logs gateway | grep "FIRST RUN"
#    poi apri https://<server>:8631/setup
```

Guida completa, con certificati, stampanti, aggiornamenti e una prova senza stampante né RADIUS: [docs/it/installazione.md](docs/it/installazione.md).
Poi utenti e gruppi: [guida-amministratore.md](docs/it/guida-amministratore.md); collegare computer e telefoni: [stampa.md](docs/it/stampa.md).

## Come è stato sviluppato

SpoolSteward nasce per risolvere un mio problema e l'ho pubblicato perché può servire ad altri. Ho usato strumenti di intelligenza artificiale (IA) in modo rilevante, e preferisco dirlo apertamente:

- **Documentazione, test automatici e commenti nel codice**: scritti per lo più con l'aiuto dell'IA.
- **Codice dell'applicazione**: scritto in parte da me e in parte con l'aiuto dell'IA.
- **Traduzioni**: fatte con DeepL e poi uniformate con strumenti di IA. Quelle in spagnolo, russo e cinese non sono state riviste da madrelingua.

Se cerchi un software scritto interamente da una persona, questo non è il repository adatto.

Cosa puoi controllare da solo: i test automatici (`tests/`), la sezione Stato (cosa è stato provato su hardware reale) e l'elenco dei limiti noti. Il codice non ha avuto una revisione indipendente: se lo usi dove conta, guarda con attenzione le parti che riguardano la sicurezza (autenticazione, chiavi di stampa, certificati).

## Documentazione

| Argomento | Documento |
|---|---|
| **Installazione** (Docker, stampanti, TLS, aggiornamenti) | [installazione.md](docs/it/installazione.md) |
| **Risoluzione dei problemi** | [risoluzione-problemi.md](docs/it/risoluzione-problemi.md) |
| Riferimento: ogni impostazione di `.env` | [configurazione.md](docs/it/configurazione.md) |
| Windows 11 passo per passo | [windows.md](docs/it/windows.md) |
| Architettura e tecnologie | [architettura.md](docs/it/architettura.md) |
| Come è fatta l'applicazione (ogni file sorgente, `.env`, script) | [funzionamento-applicazione.md](docs/it/funzionamento-applicazione.md) |
| Guida utente · Guida amministratore (CLI compresa) | [guida-utente.md](docs/it/guida-utente.md) · [guida-amministratore.md](docs/it/guida-amministratore.md) |
| Account, gruppi, chiavi di stampa | [account.md](docs/it/account.md) |
| Quote, richieste, concessioni | [quote.md](docs/it/quote.md) |
| Stampa e collegamento dei dispositivi | [stampa.md](docs/it/stampa.md) |
| Stampanti: panoramica, controlli, stampa di manutenzione | [stampanti.md](docs/it/stampanti.md) |
| Scansione | [scansione.md](docs/it/scansione.md) |
| Audit e archivio | [audit.md](docs/it/audit.md) |
| Backup e ripristino | [backup.md](docs/it/backup.md) |
| Certificati TLS: avvisi, file della CA, sincronizzazione automatica | [certificati.md](docs/it/certificati.md) |
| Protezione dei dati: nomi dei file, cifratura, retention | [protezione-dati.md](docs/it/protezione-dati.md) |
| Lingue e traduzioni | [lingue.md](docs/it/lingue.md) |
| Limiti noti | [limiti.md](docs/it/limiti.md) |

Tutto è disponibile anche in inglese (lingua principale): [docs/en](docs/en) · [docs/README.md](docs/README.md).

## Licenza

SpoolSteward è rilasciato con la [licenza Apache 2.0](LICENSE). Puoi usarlo, modificarlo e distribuirlo, anche a scopo commerciale, purché mantieni
l'avviso di copyright e il file [NOTICE](NOTICE) (crediti ad Alessio Paccino) e indichi le modifiche che fai. Copyright 2026 Alessio Paccino.
