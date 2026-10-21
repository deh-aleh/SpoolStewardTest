# Stampa e collegamento dei dispositivi

[Indice della documentazione](../README.md) · [English](../en/printing.md) · Italiano

Ci sono due modi di stampare: attraverso l'**indirizzo IPP del gateway** (computer e app) oppure dal **browser** (qualunque telefono,
senza installare nulla). Entrambi passano dalle stesse quote, audit e identità (`gw:<utente>`).

## 1. Stampare dal browser

*Stampa* nel menu (`/me/print`), con la sessione aperta, senza bisogno di chiave.

- File accettati: PDF, JPEG, PNG, GIF, testo semplice — riconosciuti dal loro **contenuto**, non dal nome o da ciò che dichiara il browser.
- Dimensione massima `SPOOLSTEWARD_WEB_UPLOAD_MB` (predefinito 25); copie fino a `SPOOLSTEWARD_WEB_MAX_COPIES` (predefinito 20).
- Le opzioni dipendono dalla stampante scelta: formato carta (solo quelli elencati dalla coda), orientamento, colore / bianco e nero
  (se la coda è a colori), **intervallo di pagine** (`1,5`, `2-4`, `3-`), copie e un **commento** (max 50 caratteri) mostrato sotto il
  nome del file nell'audit.
- **Orientamento.** *Automatico* segue il file: un PDF con pagine già larghe si stampa da solo in orizzontale. *Orizzontale* su un
  PDF con pagine verticali ruota ogni pagina di 90° e la rimpicciolisce per farla stare sullo stesso foglio (lo fa il portale, perché i
  filtri PDF di CUPS capovolgono la pagina verticale su alcune stampanti, per esempio le Brother). Non riorganizza il testo come fa
  LibreOffice quando cambi lo stile di pagina in orizzontale: per quello imposta l'orizzontale nel programma ed esporta di nuovo il PDF.
  Le foto le ruota CUPS. I file di testo semplice non si possono ruotare (il portale lo dice e non stampa nulla).
- Le pagine contate per il limite sono quelle scelte × copie; un intervallo oltre la fine del documento è rifiutato prima di riservare quota.
- Una stampante che non risponde non viene offerta; nemmeno una bloccata. L'ordine dell'elenco è descritto in [stampanti.md](stampanti.md).
- **Permessi** ([quote.md](quote.md#5-permessi-avanzati)). Compaiono solo le stampanti che l'utente può usare. La scelta del colore è
  nascosta senza il permesso colore. Una **scelta fronte-retro** (predefinito della stampante / solo fronte / lato lungo / lato corto) compare
  quando la stampante ha l'unità fronte-retro e l'utente può stampare solo fronte; chi non può, stampa in fronte-retro (lato lungo). Il modulo
  chiede una scelta, ma decide il server con le stesse regole del gateway IPP, quindi un modulo modificato a mano ottiene lo stesso risultato.
  Fuori dagli orari consentiti un avviso in cima dice quando si può stampare, e un lavoro inviato lo stesso viene rifiutato.

## 2. Stampare con l'indirizzo IPP

Indirizzo: `ipps://<server>:8631/printers/<coda>`. Il client si autentica con HTTP Basic: **nome utente + chiave di stampa**
(creata in *Profilo*; vedi [account.md](account.md)). La password di dominio non stampa.

| Dispositivo | Come collegarlo | Stato |
|---|---|---|
| **Linux** (GNOME, KDE, CUPS) | **Impostazioni → Stampanti di GNOME non sa aggiungere una coda il cui indirizzo contiene nome utente e chiave**: usa il comando `lpadmin` mostrato nella pagina che compare dopo «Crea una chiave» (contiene nome utente e chiave), oppure `scripts/add-printer-linux.sh <server> <coda> <utente>`, che chiede la chiave senza mostrarla e non la lascia nella cronologia della shell. Entrambi salvano la chiave nell'indirizzo della coda (`/etc/cups/printers.conf`, leggibile solo da root e dal gruppo `cups`) e impostano `auth-info-required=none`: il computer non chiede più nulla. L'indirizzo `ipps://utente:chiave@server/printers/coda` si può incollare anche in KDE (Impostazioni → Stampanti → Aggiungi → URI) o nell'interfaccia web di CUPS (`http://localhost:631`); queste due vie non sono state provate dal progetto. | ✅ provato con `lpadmin` e con lo script |
| **Qualunque dispositivo con browser** | `/me/print` | ✅ provato su Android |
| **Android + Mopria Print Service** | Vede le stampanti annunciate, chiede nome e chiave per ogni stampante e li ricorda. | ✅ provato (Android 13) |
| **Servizio di stampa di sistema di Android** | Vede le stampanti ma non sa chiedere le credenziali: davanti al 401 rinuncia. L'aggiunta manuale accetta solo un IP, quindi la chiave non si può mettere nell'indirizzo. | ❌ non utilizzabile — usa Mopria o il browser |
| **iPhone / iPad** | L'annuncio porta il sottotipo AirPrint e `air=username,password`; non è noto se iOS invii le credenziali. | ⬜ non provato |
| **Windows 11** | «Aggiungi dispositivo» trova le stampanti annunciate e le aggiunge al primo tentativo; Windows chiede le credenziali quando stampi, con una casella «ricorda». Procedura nel paragrafo [Windows 11](windows.md) qui sotto. | ✅ provato su Windows 11 25H2 |
| **macOS** | Aggiungi una stampante di rete con l'indirizzo IPP e inserisci nome utente + chiave; "ricorda". | ⬜ non provato |

Il dispositivo deve fidarsi del certificato del server. Le stampanti annunciate da mDNS compaiono anche sui desktop Linux (Avahi) senza
chiave: usarle non produce nulla finché la coda non viene aggiunta con la chiave nell'indirizzo.

Script: `scripts/print-test.sh SERVER[:PORTA] CODA [UTENTE]` invia una pagina di testo (consuma un foglio), per esempio `scripts/print-test.sh print.example.lan:8631 ufficio mario`. Server e coda sono obbligatori; l'utente, se manca, è il tuo nome di login. Puoi scrivere il server una volta sola nella variabile d'ambiente `GATEWAY` e passare solo `CODA [UTENTE]`.

### Windows 11

Guida completa passo passo: [windows.md](windows.md). In breve: *Impostazioni → Stampanti e scanner → Aggiungi dispositivo*, premi *Aggiungi* sulla stampante annunciata e alla prima stampa inserisci nome utente e **chiave di stampa** con *Ricorda le credenziali* spuntato. Il PC deve fidarsi del certificato del server ([certificati.md](certificati.md)).

## 3. Cosa fa il gateway con ogni operazione IPP

Consentite: stampare (`Print-Job`, `Create-Job` + `Send-Document`), annullare, vedere job e stampanti, query di capacità. Vietate a
tutti: le operazioni amministrative di CUPS, `Print-URI`, `Send-URI` (rischio SSRF). Le query di capacità (`Get-Printer-Attributes` e `Validate-Job`,
corpi piccoli) ricevono risposta **senza credenziali** perché i client le fanno prima di averne (`SPOOLSTEWARD_ANONYMOUS_DISCOVERY=true`):
non creano utenti né righe di audit e non inoltrano nulla. Il percorso completo è in
[architettura.md](architettura.md#4-percorso-di-una-richiesta-ipp).

Il gateway dichiara ai client `uri-security-supported=tls` e `uri-authentication-supported=basic` (CUPS direbbe `none`): senza,
Mopria non invia le credenziali.

### Permessi e IPP

Per `Print-Job`, `Create-Job` e `Validate-Job` il gateway applica i permessi dell'utente (colore, solo fronte, stampanti, orari) dopo il
controllo della stampante bloccata e prima della quota. Per correggere un lavoro riscrive `print-color-mode` e `sides` nella richiesta
prima che vada a CUPS, e toglie le opzioni in stile PPD `ColorModel`, `ColorMode` e `Duplex`. `Validate-Job` non fallisce per colore o lati
in modalità `adjust`. `CUPS-Get-Printers` e `CUPS-Get-Classes` lasciano fuori le stampanti che un utente limitato non può usare. Un telefono
o un computer che ha trovato la stampante con l'annuncio in rete la vede comunque, perché l'annuncio è uguale per tutti; il lavoro viene
rifiutato. Dettagli e avvertenze: [quote.md](quote.md#5-permessi-avanzati).

## 4. Annuncio sulla rete (DNS-SD)

Il servizio `mdns` annuncia ogni coda come `_ipps._tcp` con il sottotipo `_universal` (AirPrint) e i record TXT `rp`, `pdl`, `URF`,
`Color`, `Duplex`, `TLS`, `air`, aggiornati ogni `SPOOLSTEWARD_MDNS_INTERVAL` secondi. Il nome del server annunciato è `<nome>.local`
(Android risolve per multicast solo quei nomi) e il suo indirizzo deve stare nel certificato TLS. Le stampanti bloccate-nascoste, i
dispositivi irraggiungibili e la modalità manutenzione ritirano l'annuncio. Gli scanner sono annunciati come `_uscans._tcp`
([scansione.md](scansione.md)).

## 5. Job rifiutati, annullamento e stati

Se un job è rifiutato il client riceve un errore IPP con il motivo e l'indirizzo del portale. Un utente può annullare i propri job;
un amministratore può annullare i job in coda dall'audit ("Annulla") usando l'identità del proprietario (nessun privilegio CUPS in più).
Gli stati e il loro significato sono spiegati nella pagina *Audit* ([audit.md](audit.md)).
