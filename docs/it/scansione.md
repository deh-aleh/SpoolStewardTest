# Scansione

[Indice della documentazione](../README.md) · [English](../en/scanning.md) · Italiano

La scansione usa **eSCL** (AirScan): `ipp-usb` lo serve sulla stessa porta di IPP, quindi stampa e scansione condividono il dispositivo USB senza litigare — **ipp-usb è l'unico proprietario** (`ipp-usb` e SANE dell'host vanno fermati). Una stampa inviata durante una scansione funziona.
SANE in rete è stato scartato: non autentica per utente e riporterebbe il conflitto sul dispositivo.

## 1. Scoperta degli scanner

Gli scanner si **trovano a runtime**: a ogni coda CUPS di rete si chiedono le capacità eSCL (ipp-usb dà a ogni stampante USB la sua porta), più gli indirizzi in `SPOOLSTEWARD_SCAN_URL` (`nome=url`, separati da virgola; l'immagine preimposta `http://ipp-usb:60000`; `none` = solo scoperta automatica).
Gli scanner rispondono a eSCL sulla porta IPP della coda o sulla 80; gli altri casi vanno in `SPOOLSTEWARD_SCAN_URL`. Uno scanner scollegato sparisce entro ~30 s. Il servizio `mdns` annuncia ogni scanner come `_uscans._tcp`.

## 2. Scansionare dal portale

*Scansiona* (`/me/scan`): nulla da installare, funziona da qualunque telefono.

- Scelta dello **scanner** se ce n'è più di uno. **Sorgente**: *Automatica* (caricatore se lo scanner lo dichiara carico, altrimenti vetro), *Vetro* o *Caricatore* (legge **tutti** i fogli, massimo 100).
- Colore / grigi / bianco e nero; **qualità** e **formato carta** secondo ciò che lo scanner dichiara per quella sorgente.
- Uscita: **PDF** (più fogli uniti) o **JPEG** (più fogli: ZIP); campo **nome del file**. I browser Chromium aprono la scelta della cartella; gli altri salvano nei download.
- La scansione gira come **attività di fondo del server**: puoi lasciare la pagina e scaricare dopo (una scansione web alla volta). Se lo scanner risponde 409/503 (si sta svegliando) il gateway riprova 3 volte.
- Le scansioni finite aspettano **nella memoria del server**, visibili solo al proprietario (una pagina a colori a 300 dpi pesa circa 1 MB; un caricatore da 100 pagine 100 MB). Tre impostazioni la tengono sotto controllo: `SPOOLSTEWARD_SCAN_KEEP_MINUTES` (10), `SPOOLSTEWARD_SCAN_KEEP_MB`
  (50, tutte le persone insieme: **le più vecchie finite vengono tolte per prime**, la più recente resta sempre) e `SPOOLSTEWARD_SCAN_DELETE_AFTER_DOWNLOAD` (`true`: si scarica una volta sola). Una scansione tolta va rifatta: scaricala presto.

![Una scansione dal vetro in corso: il pulsante Scansiona è grigio e la barra sotto dice che la scansione è in corso](../images/it/scan-running.png)

## 3. Scansione con client eSCL

Il gateway espone `/eSCL/...`. `ScannerCapabilities` e `ScannerStatus` sono pubblici; tutto il resto richiede HTTP Basic con **nome utente + chiave di stampa**, con la stessa autenticazione e lo stesso throttling della stampa. Percorsi ammessi: `ScanJobs` (POST),
`ScanJobs/<id>/NextDocument|ScanImageInfo` (GET), `ScanJobs/<id>` (DELETE); il resto è 404.

- Ogni scansione è una riga dell'audit con `kind=scan`; il dispositivo è la coda CUPS dello scanner. Stati: `accepted` → `completed` (1 pagina per `NextDocument`), `failed` (scanner occupato o che non risponde), `canceled` (solo prima di consegnare la pagina).
- Un utente non può leggere né annullare la scansione di un altro (404). Il job eSCL resta associato all'utente finché il client lo cancella con DELETE — è questo che libera lo scanner.
- **Client senza credenziali** (NAPS2 non sa inviarle): `SPOOLSTEWARD_ESCL_ANONYMOUS=true` accetta scansioni senza login, registrate come l'account locale senza password `unknown-guest` (nome in `SPOOLSTEWARD_ESCL_GUEST_USERNAME`; non può entrare nel portale) con l'IP di chi chiama nell'audit.
  Credenziali sbagliate restano 401. Finché l'opzione è spenta, una richiesta senza credenziali viene rifiutata (401) e compare come avviso ([guida-amministratore.md](guida-amministratore.md)). **Predefinito spento**: chiunque nella rete raggiunga la porta 8631 potrebbe scansionare, quindi usalo solo su rete fidata.
  NAPS2 non ha trovato l'annuncio mDNS: inserisci IP e porta a mano.

### Finestra di scansione

**Finestra di scansione** (`SPOOLSTEWARD_ESCL_WINDOW`, accesa di default; un ultimo rimedio, vedi i limiti più sotto): un pulsante in alto a destra in *Scansione* dice al gateway «la mia app sta per scansionare». Il gateway si segna l'indirizzo che vede per quella connessione e, per `SPOOLSTEWARD_ESCL_WINDOW_SECONDS`
(60–900, predefinito 180), accetta richieste **senza credenziali solo da quell'indirizzo**, a nome di chi ha premuto: valgono la sua quota e i suoi permessi e l'audit riporta il suo nome e l'indirizzo. Finché è aperta si possono fare quante scansioni si vuole.

La finestra decide solo se può partire una scansione **nuova**: una già iniziata viene consegnata, e può essere chiusa con DELETE, anche se la finestra scade. Le finestre stanno in memoria, quindi un riavvio le chiude. Una persona ha una sola finestra; se due persone premono dallo stesso indirizzo vale l'ultima. Con
`SPOOLSTEWARD_ESCL_ANONYMOUS=true` la finestra cambia solo a chi va la scansione (alla persona invece che a `unknown-guest`). Con `SPOOLSTEWARD_ESCL_WINDOW=false` il pulsante non è proprio nella pagina.

Quando una richiesta senza credenziali arriva da un altro indirizzo mentre c'è una finestra aperta, viene rifiutata (401). L'audit riceve una riga *negata* per il proprietario con i due indirizzi e la sua campanella dice «scansione rifiutata: la finestra era per X, la richiesta arriva da Y» (al massimo una volta al minuto per coppia).

### Limiti della finestra di scansione

La finestra è un **ultimo rimedio**, per le app che non sanno inviare né nome utente né chiave e quando vuoi comunque ogni scansione nell'audit. Si regge su una cosa debole, l'indirizzo della connessione TCP; tutto ciò che lo cambia la rompe:

- **NAT**: se il browser e l'app di scansione stanno dietro lo stesso router per il server (un'altra rete, una VPN, il Wi-Fi ospiti), il server vede l'indirizzo del router per *entrambi* e la finestra accetta anche ogni altro dispositivo dietro quell'indirizzo, finché è aperta. Con NAT diversi gli indirizzi differiscono e la scansione è rifiutata.
- **IPv4 e IPv6 insieme**: se il computer raggiunge il portale in IPv6 e l'app si collega in IPv4 (o il contrario), gli indirizzi non coincidono. Apri il portale con la famiglia di indirizzi usata dall'app (il riquadro che si apre premendo il pulsante dice quale indirizzo ha visto il server). `::ffff:192.168.1.5` vale come IPv4.
- **Reverse proxy**: se apri il portale *attraverso* il proxy, la finestra si apre per l'indirizzo del proxy, mentre l'app di norma si collega alla porta 8631 con il proprio, quindi la scansione è rifiutata. Peggio: se anche l'app passa dal proxy, *qualsiasi* dispositivo dietro il proxy può scansionare durante la finestra.
  Il server si fida di `X-Forwarded-For` solo da `127.0.0.1` e da `FORWARDED_ALLOW_IPS` ([certificati.md](certificati.md#41-dietro-un-reverse-proxy)). Quello che funziona: premi il pulsante da una pagina aperta **direttamente** sul gateway (`https://<server>:8631/me/scan`); il riquadro avvisa se la richiesta portava intestazioni di proxy.
- **Un altro dispositivo**: premi il pulsante dal computer su cui gira l'app, non da un telefono.
- **Indirizzo che cambia** (DHCP, roaming Wi-Fi) durante la finestra interrompe la corrispondenza.

Se l'indirizzo nel riquadro non è quello del dispositivo che scansiona, la finestra non può funzionare su quella rete: usa `SPOOLSTEWARD_ESCL_ANONYMOUS=true` su una rete fidata, oppure un'app che sa inviare nome utente e chiave di stampa. La guida utente ripete questi limiti in breve.

## 4. Note

- Una scansione consegnata sul client conta come completata anche se l'utente non salva il file.
- Se il caricatore dorme lo scanner può dichiararlo vuoto: in *Automatica* si finisce sul vetro (pagina bianca). Scegli *Caricatore* oppure togli e rimetti i fogli.
- La modalità manutenzione ferma anche la scansione ([stampanti.md](stampanti.md)). Non ancora provati: client eSCL nativi che sanno autenticarsi (sane-airscan, Image Capture).
