# Limiti noti

[Indice della documentazione](../README.md) · [English](../en/limitations.md) · Italiano

Ciò che SpoolSteward **non** fa (ancora) o fa con delle riserve. La versione breve è nel [README](../../README.it.md#limiti).

## 1. Installazione

- Serve un **host Linux** per le stampanti USB e per mDNS (su Docker Desktop per Windows/macOS funzionano solo le stampanti di rete e non si annuncia nulla).
- Un gateway per stampante USB (`ipp-usb` la vuole in esclusiva); il suo contenitore vede tutti i dispositivi USB dell'host (necessario per il collegamento a caldo).
- Il database è SQLite (un file): un solo gateway, nessuna alta disponibilità, nessun database esterno. Le immagini pubblicate esistono solo per amd64 e arm64.
- `scripts/update.sh` sostituisce `docker-compose.yml` e gli script, mai `.env`, `config/` e `data/`, e non funziona in una cartella clonata con git. Le opzioni nuove di una versione non vengono scritte nel tuo `.env`: funzionano con il valore predefinito (confronta con `docker/env.example`).
- Il pulsante *Aggiorna* richiede il contenitore facoltativo `updater`, che tiene il socket di Docker (root sull'host) ed è spento di default. Aggiorna solo dentro la stessa versione maggiore e, se la nuova versione non parte, rimette quella vecchia senza ripristinare il backup.
- Backup su una condivisione di rete: i permessi dei file li decide il montaggio (usa la cifratura). Una condivisione smontata non riceve backup: «scrivi solo se montato» è attivo di default per i backup del portale, spento per quello *Generale* ([backup.md](backup.md)).
- Tornare a una versione precedente dopo una migrazione del database richiede il ripristino di un backup (`scripts/update.sh`).
- CUPS tiene i nomi dei suoi ultimi job (fino a 2.000) nel proprio volume: il pulsante e la retention dei nomi non li toccano.
- Certificati: i dispositivi devono fidarsi della CA aziendale; l'annuncio usa l'IP, quindi il certificato deve elencarlo ([certificati.md](certificati.md)).

## 2. Quota

- Il **limite pagine è morbido** per i file di cui non si possono contare le pagine: formati sconosciuti, PDF cifrati, file oltre 64 MB. Il job che supera il limite viene stampato; le sue pagine reali bloccano poi i successivi.
- Con `Create-Job` + `Send-Document` il conteggio arriva dopo l'accettazione. Un `Create-Job` senza documento libera la quota solo quando CUPS lo annulla (di default 3 h). Il conteggio vale solo se esiste un limite di pagine e il documento è leggibile.
- Se RADIUS distingue `Mario` da `mario`, per il gateway sono la stessa persona (stessa quota).
- I limiti per il formato carta non esistono. Colore, solo fronte, stampanti e orari ci sono ([quote.md](quote.md#5-permessi-avanzati)), con questi limiti:
  - **Il bianco e nero forzato può non funzionare** con alcune stampanti e driver, soprattutto con immagini che il telefono ha già trasformato in pixel a colori (iPhone / AirPrint). Per una garanzia usa `SPOOLSTEWARD_PERMISSION_MODE=refuse`; il fronte-retro usa l'attributo standard `sides` ed è più affidabile se la coda dichiara l'unità fronte-retro.
  - Con `refuse`, Windows e Android spesso rifiutano molti lavori, perché mandano solo fronte o colore in ogni lavoro. Il predefinito `adjust` lo evita.
  - L'annuncio mDNS è uguale per tutti: le stampanti che una persona non può usare si vedono comunque e il lavoro viene rifiutato. L'elenco dato dopo l'accesso le nasconde.

## 3. Client

- **Il servizio di stampa di sistema di Android non sa autenticarsi**: usa Mopria Print Service o il browser. Android conserva le credenziali per stampante e per sessione; il gateway può solo revocare la chiave.
- iPhone/iPad e macOS **non sono stati provati**; da Windows 11 si stampa ma non è verificato se il nome del file arrivi in `job-name`.
- Le chiavi di stampa dei client Linux/CUPS finiscono salvate sul client (revocabili e inutili altrove). Le stampanti annunciate compaiono ai desktop Linux (Avahi) senza chiave e non fanno nulla finché la coda non è aggiunta con la chiave. **GNOME** non sa aggiungere una coda il cui indirizzo contiene nome utente e chiave: usa `lpadmin` o `scripts/add-printer-linux.sh`.
- Una chiave monouso si consuma quando la stampa viene accettata: se poi CUPS o la stampante falliscono, serve un'altra chiave. Dopo la stampa vale ancora 2 minuti, solo per chiedere lo stato.
- La **finestra di scansione** è un ultimo rimedio: confronta solo l'indirizzo della connessione TCP, quindi NAT, reverse proxy, IPv4/IPv6 mescolati o un indirizzo che cambia la rompono ([scansione.md](scansione.md#limiti-della-finestra-di-scansione)).

## 4. Stampanti e scanner

- Rilevamento USB automatico: una stampante servita da `ipp-usb` non viene mai presa dal backend usb di CUPS; quelle senza IPP-su-USB richiedono un driver nell'immagine `cups` (brlaser, gutenprint, splix, foo2zjs). La convivenza delle due strade sullo stesso bus con più stampanti è provata con un dispositivo alla volta. Provate finora: HP Smart Tank 7300 (ipp-usb) e Brother DCP-7055 (backend usb, brlaser).
- Le code USB irraggiungibili da 8 h (configurabile) vengono tolte da sole; quelle di rete mai. Una coda di `CUPS_QUEUES` viene creata una sola volta: se la elimini, resta eliminata (`scripts/queue-restore.sh NOME --reboot`).
- Gli scanner si trovano da soli solo se rispondono a eSCL sulla porta IPP della coda o sulla 80; altrimenti `SPOOLSTEWARD_SCAN_URL`. Non provati i client eSCL nativi che sanno autenticarsi (sane-airscan, Image Capture). Una scansione consegnata conta come completata anche se non viene salvata. Un caricatore addormentato può essere dichiarato vuoto: in *Automatica* si usa il vetro.
- I contatori a vita vengono solo dal servizio web HP. Le pagine stampate dal gateway (prova, manutenzione) sono sempre in inglese. Se il gateway è spento, un intervallo di manutenzione perso non si recupera.
- Stampa dal browser, *Orizzontale*: i PDF con pagine verticali vengono ruotati e rimpiccioliti (il testo non viene riorganizzato); il testo semplice non si può ruotare; le foto le ruota CUPS ([stampa.md](stampa.md)).

## 5. Backup e sicurezza

- Backup integrati: solo SQLite; code CUPS non incluse (sono in `scripts/backup.sh`); i file di configurazione si ripristinano a mano; nessuna destinazione remota diretta; la passphrase non è recuperabile. `scripts/backup.sh restore` è provato solo a mano.
- L'audit e il database non sono cifrati di default (vedi [protezione-dati.md](protezione-dati.md)).

## 6. Lingue

Spagnolo, russo e cinese scritti senza revisione di un madrelingua. Date e numeri non seguono la lingua. Il testo dell'audit conservato e i log del server restano in inglese.

## 7. Stato delle funzionalità

Cosa è stato provato e cosa manca è nel [README](../../README.it.md#stato).
