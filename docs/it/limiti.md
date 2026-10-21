# Limiti noti

[Indice della documentazione](../README.md) · [English](../en/limitations.md) · Italiano

Ciò che SpoolSteward **non** fa (ancora) o fa con delle riserve. La versione breve è nel
[README](../../README.it.md#limiti).

## 1. Installazione

- Serve un **host Linux** per le stampanti USB e per mDNS (il passaggio USB e la rete `host` non esistono su Docker Desktop per
  Windows/macOS: lì funzionano solo le stampanti di rete e non si annuncia nulla).
- Un gateway per stampante USB (`ipp-usb` la vuole in esclusiva). Il suo contenitore vede tutti i dispositivi USB dell'host (necessario per il collegamento a caldo).
- Il database è SQLite (un file): un solo gateway, nessuna alta disponibilità, nessun database esterno.
- Le immagini pubblicate esistono solo per amd64 e arm64 (niente ARM a 32 bit).
- `scripts/update.sh` scarica il pacchetto della release e sostituisce `docker-compose.yml` e gli script, mai `.env`, `config/` e `data/`. Non funziona in una cartella
  clonata con git (lì si usa `git pull`). Le opzioni nuove di una versione non vengono scritte nel tuo `.env`: funzionano con il valore predefinito; per vederle
  confronta `.env` con `docker/env.example`.
- Il pulsante *Aggiorna* del portale richiede il contenitore facoltativo `updater`, che tiene il socket di Docker (root sull'host): è spento di default. Il portale lo segnala solo quando esiste una release più nuova: nel menu *Backup*, riquadro *Aggiornamento*, il pulsante
  resta grigio e dice perché (la barra in cima suggerisce comunque `scripts/update.sh`). Senza una release nuova non compare nessun avviso. Aggiorna solo dentro la stessa versione maggiore e, se la nuova versione non parte, rimette quella vecchia ma non ripristina il backup da solo.
- Backup su una condivisione di rete: i permessi dei file li decide il montaggio (usa la cifratura). Una condivisione smontata non riceve backup: l'opzione
  «scrivi solo se montato» è attiva di default per i backup creati dal portale e il portale avvisa quando un backup salta. Il backup *Generale* di `.env` ha l'opzione spenta
  di default (va attivata e preparata a mano, vedi [backup.md](backup.md)).
- Tornare a una versione precedente dopo una migrazione del database richiede il ripristino di un backup: `scripts/update.sh` lo propone da solo quando la versione
  vecchia non parte. Le migrazioni nuove devono solo aggiungere (la versione vecchia continua a poter leggere il database), ma non è una garanzia per ogni caso.
- I **nomi dei file stampati sono cifrati** nel database e nell'archivio audit (chiave derivata da `SPOOLSTEWARD_SECRET_KEY`: se la cambi non si leggono più). Il resto del
  database non è cifrato (nomi utente, gruppi, nomi delle stampanti, registro admin): per proteggere tutto cifra il disco, vedi [protezione-dati.md](protezione-dati.md).
  Anche CUPS tiene i nomi dei suoi ultimi job (fino a 2.000) nel proprio volume: il pulsante e la retention dei nomi non li toccano.
- Certificati: i dispositivi devono fidarsi della CA aziendale; l'annuncio usa l'IP, quindi il certificato deve elencarlo (il portale avvisa se non è così).
  `scripts/cert-sync.sh` **copia** il certificato da chi lo rinnova ma non lo rinnova: per accorgersi di un rinnovo fallito serve l'avviso di scadenza, spento di default.
  Dettagli in [certificati.md](certificati.md).

## 2. Quota

- Il **limite pagine è morbido** per i file di cui non si possono contare le pagine: formati sconosciuti, PDF cifrati, file oltre 64 MB.
  Il job che supera il limite viene stampato; le sue pagine reali bloccano poi i successivi.
- Con `Create-Job` + `Send-Document` il conteggio arriva dopo l'accettazione del job.
- Un `Create-Job` senza documento libera la quota solo quando CUPS lo annulla (di default 3 h).
- Il conteggio delle pagine vale solo se esiste un limite di pagine e il documento è leggibile.
- Se RADIUS distingue `Mario` da `mario`, per il gateway sono la stessa persona (stessa quota).
- I limiti per il formato carta non esistono. Colore, solo fronte, stampanti e orari ci sono ([quote.md](quote.md#5-permessi-avanzati)), con questi limiti:
  - **Il bianco e nero forzato può non funzionare** con alcune stampanti e alcuni driver, soprattutto con le immagini che il telefono ha già trasformato in pixel a colori (iPhone / AirPrint). Per una garanzia usa `SPOOLSTEWARD_PERMISSION_MODE=refuse`. Il fronte-retro usa l'attributo standard `sides` ed è più affidabile, purché la coda dichiari la sua unità fronte-retro.
  - Con `refuse`, Windows e Android spesso rifiutano molti lavori, perché mandano solo fronte o colore in ogni lavoro. Il predefinito `adjust` lo evita.
  - L'annuncio in rete (mDNS) è uguale per tutti: le stampanti che una persona non può usare si vedono comunque, e il lavoro viene rifiutato. L'elenco delle stampanti che il gateway dà dopo l'accesso le nasconde.
  - Gli orari si controllano quando il lavoro viene inviato, non mentre stampa. C'è una sola fascia (giorni + due ore) per gruppo o utente.
  - La scansione non è limitata. L'amministratore di emergenza è esente dai permessi e dalle quote.
  - Un permesso chiesto viene concesso per sempre o fino a una data, non per un numero di stampe o di ore d'uso.

## 3. Client

- **Il servizio di stampa di sistema di Android non sa autenticarsi**: usa Mopria Print Service oppure stampa dal browser.
- Android conserva le credenziali per stampante e per sessione; il gateway può solo revocare la chiave.
- iPhone/iPad e macOS **non sono stati provati**; da Windows 11 si stampa ma non è verificato se il nome del file arrivi in `job-name`.
- Le chiavi di stampa dei client Linux/CUPS finiscono comunque salvate sul client (revocabili e inutili altrove).
- Una chiave monouso si consuma quando la stampa viene accettata: se poi CUPS o la stampante falliscono, la chiave è comunque spesa e se ne crea un'altra. Dopo la stampa vale ancora 2 minuti, solo per chiedere lo stato.
- Le stampanti annunciate compaiono anche ai desktop Linux (Avahi) senza chiave; non fanno nulla finché la coda non è aggiunta con la chiave.
- **GNOME** (Impostazioni → Stampanti) non sa aggiungere una coda il cui indirizzo contiene nome utente e chiave: usa il comando `lpadmin` della pagina «Crea una chiave» o `scripts/add-printer-linux.sh`.
- La **finestra di scansione** è un ultimo rimedio: confronta solo l'indirizzo della connessione TCP, quindi NAT, reverse proxy, IPv4/IPv6 mescolati o un indirizzo che cambia la rompono, e dietro un indirizzo condiviso lascia scansionare anche altri dispositivi finché è aperta ([scansione.md](scansione.md)).

## 4. Stampanti e scanner

- Rilevamento USB automatico: una stampante servita da `ipp-usb` non viene mai presa dal backend usb di CUPS. Le stampanti senza IPP-su-USB richiedono un driver presente nell'immagine `cups` (inclusi brlaser, gutenprint, splix, foo2zjs). La convivenza fra backend usb di CUPS e `ipp-usb` sullo stesso bus con più stampanti è provata solo con un dispositivo alla volta. Stampanti provate finora: HP Smart Tank 7300 (tramite ipp-usb) e Brother DCP-7055 (backend usb di CUPS, brlaser).
- Le code USB irraggiungibili da 8 h (configurabile) vengono tolte da sole; quelle di rete mai.

- Il rilevamento di un dispositivo scollegato ritarda fino a ~30 s; per le code di rete è una prova TCP (nessuna informazione sulla carta).
- Gli scanner si trovano da soli solo se rispondono a eSCL sulla porta IPP della coda o sulla 80; altrimenti `SPOOLSTEWARD_SCAN_URL`.
- I contatori a vita vengono solo dal servizio web HP; le altre marche mostrano i dati IPP.
- Una scansione consegnata sul client conta come completata anche se l'utente non salva il file.
- Un caricatore addormentato può essere dichiarato vuoto: in *Automatica* si usa il vetro (pagina bianca).
- Non provati i client eSCL nativi che sanno autenticarsi (sane-airscan, Image Capture).
- Le pagine stampate dal gateway (prova, manutenzione) sono sempre in inglese; russo/cinese richiedono un font incorporato.
- Se il gateway è spento, un intervallo di manutenzione perso non si recupera: la pagina parte appena riparte.

- Stampa dal browser, *Orizzontale*: i PDF con pagine verticali vengono ruotati e rimpiccioliti (il testo non viene riorganizzato come farebbe un programma di videoscrittura); i file di testo semplice non si possono ruotare; le foto le ruota CUPS. Dettagli in [stampa.md](stampa.md).
- Una coda di `CUPS_QUEUES` viene creata una sola volta: se la elimini, resta eliminata (`scripts/queue-restore.sh NOME --reboot` la rimette).

## 5. Backup e sicurezza

- Backup integrati: solo SQLite; code CUPS non incluse (sono in `scripts/backup.sh`); i file di configurazione si ripristinano a mano;
  nessuna destinazione remota diretta; la passphrase non è recuperabile. `scripts/backup.sh restore` è provato solo a mano.
- Il blocco dei login sopravvive al riavvio: i login falliti degli ultimi 5 minuti sono tenuti in `/data/login-lockout.json` (nel volume del gateway; nomi utente e indirizzi con gli orari, nessuna password). Se il file non si può scrivere il blocco resta solo in memoria e il log lo dice.
- L'audit e il database non sono cifrati, salvo i nomi dei file (vedi §1 e [protezione-dati.md](protezione-dati.md)).

## 6. Lingue

- Spagnolo, russo e cinese scritti senza revisione di un madrelingua.
- Date e numeri non seguono la lingua.
- Il testo dell'audit conservato e i log del server restano in inglese.

## 7. Stato delle funzionalità

Lo stato di ogni funzionalità è nel [README](../../README.it.md#funzionalità-e-stato).
