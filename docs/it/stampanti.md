# Stampanti: panoramica, controlli, stampa di manutenzione

[Indice della documentazione](../README.md) · [English](../en/printers.md) · Italiano

La pagina **Stampanti** (`/admin/printers`, per gli amministratori) mostra ciò che ogni stampante dice di sé accanto a ciò che il gateway ha registrato. Tutto si legge dal vivo e al meglio: una stampante che non offre una fonte non ha quella sezione.

## 1. Cosa mostra la scheda di una stampante

![La pagina Stampanti: stampanti di prova, la ricerca delle stampanti USB e la scheda di una multifunzione con livelli d'inchiostro, contatori della stampante e job contati dal gateway](../images/it/admin-printers.png)

- Stato e coda di ogni coda CUPS; un dispositivo che non risponde è **NON RAGGIUNGIBILE** (e sparisce dal modulo di stampa e dall'annuncio mDNS). Il rilevamento può ritardare ~30 s; per le code di rete è una prova TCP (non può sapere che la carta è finita); le code `usb:` / `file:` si assumono presenti.
- Accanto a «n in coda», il pulsante **…** mostra l'indirizzo della coda (`ipps://<server>/printers/<nome>`) con un pulsante per copiarlo: quello da scrivere per aggiungere la stampante a mano.
- **Inchiostro** (livelli e colori da CUPS).
- **Contatori della stampante** (a vita: pagine totali, bianco/nero, colore, fogli, inceppamenti, mancate prese) dal servizio web incorporato HP; le altre marche mostrano solo i dati IPP.
- Numero di serie, firmware, velocità, carta pronta, avvisi. Inceppamento, carta finita e inchiostro/toner esaurito o in esaurimento generano anche un avviso nella campanella ([guida-amministratore.md](guida-amministratore.md)).
- Stato e capacità dello **scanner** (eSCL), con un badge *MFP*.
- Ciò che il gateway ha registrato: job, pagine, scansioni, ultimo job.

Le letture sono parallele, tolleranti ai guasti e in cache per `SPOOLSTEWARD_PRINTERS_REFRESH_SECONDS`. L'interruttore **Aggiornamento automatico** sotto il titolo ricarica la pagina da solo a quell'intervallo (parte spento, lo ricorda il browser, è fermo mentre c'è un popup aperto o si scrive).
Nulla è legato a un modello; i testi che arrivano dal dispositivo (nomi degli inchiostri, firmware, avvisi) si mostrano come ricevuti.

## 2. Stampanti USB nuove e scollegate

**Le stampanti nuove compaiono da sole.** Ogni `SPOOLSTEWARD_DISCOVERY_INTERVAL` secondi il gateway guarda il lato USB e crea la coda CUPS di ogni dispositivo nuovo, entro circa un minuto dal collegamento. Due strade, in quest'ordine:

1. **ipp-usb** (preferita): una stampante con IPP-su-USB (la maggior parte delle recenti) è servita dal contenitore `ipp-usb` su una porta di `SPOOLSTEWARD_USB_BRIDGE_PORTS`; la sua coda punta a `ipp://ipp-usb:<porta>/ipp/print` e non richiede driver.
2. **Backend usb di CUPS**: per le stampanti senza IPP-su-USB (molte laser vecchie, per esempio la Brother DCP-7055): la coda punta a `usb://Marca/Modello?serial=…` e a CUPS serve un driver. L'immagine include brlaser, gutenprint, splix e foo2zjs; se CUPS non ne ha uno
   per quel modello la pagina dice «trovata, ma CUPS non ha un driver». Un dispositivo servito da ipp-usb non viene mai preso così, e solo dopo essere rimasto non servito per due controlli di seguito (ipp-usb si riavvia per qualche secondo a ogni collegamento o scollegamento USB).

   *Verificato con una Brother DCP-7055 via USB:* riconosciuta come stampante USB, coda creata con brlaser (`drv:///brlaser.drv/br7055.ppd`), la prima stampa dal portale ha funzionato senza cambiare impostazioni, anche *Orizzontale* ([stampa.md](stampa.md)).

La coda prende il nome dal modello, accorciato (al massimo 24 caratteri): si tolgono parole inutili come "series", "printer", "MFP" e un codice tra parentesi quadre, "Hewlett-Packard" diventa "HP" (`HP Smart Tank 7300 series [A7E76E]` diventa `HP_Smart_Tank_7300`,
`Brother DCP-7055` diventa `Brother_DCP-7055`). Il modello è quello che la stampante dichiara. Ogni stampante USB mostra un badge: **IPP-USB** oppure **USB** (pilotata direttamente da CUPS). Il pulsante **Cerca ora** esegue subito una ricerca.
`SPOOLSTEWARD_AUTO_DISCOVER=false` spegne tutto; `SPOOLSTEWARD_USB_DIRECT=false` salta la strada 2.

**Stampanti scollegate.** Una coda sopravvive al cavo, quindi la scheda mostra **NON RAGGIUNGIBILE** e:

- un pulsante **Elimina coda** (qualunque stampante che non risponde, USB o di rete; chiede conferma). Blocco, impostazioni della stampa di manutenzione e posizione nell'elenco vengono dimenticati;
- per le **code USB**, un **conto alla rovescia** («verrà rimossa automaticamente tra 7:41:23 se la stampante non torna»). A zero il gateway elimina la coda e scrive `printer.auto_remove` nell'audit degli amministratori (autore `system`; i dispositivi nuovi sono `printer.auto_add`). L'attesa è
  `SPOOLSTEWARD_OFFLINE_REMOVE_HOURS` (8; `0` = mai);
- **le code di rete non vengono mai rimosse da sole**: una stampante spenta per il fine settimana deve ritrovarsi com'era.

Se la stampante viene ricollegata più tardi, viene aggiunta di nuovo come nuova.

## 3. Ordine dell'elenco

In **Stampanti**, nel modulo di stampa di `/me/print` e dovunque compaiano le code: **prima le fisiche, la collegata da più tempo in alto**; in fondo le **virtuali / di prova** (CUPS-PDF, `debugprinter0`…), anch'esse dalla più vecchia.

**Spostare le stampanti.** Ogni scheda ha una freccia su e una giù: spostano la stampante di un posto nel **tuo** elenco (ogni amministratore ha il proprio ordine, che vale anche nel suo modulo *Stampa*). Una stampante si muove solo dentro il suo gruppo. Dopo averla spostata il suo posto è fisso;
una collegata dopo va dopo quelle che hai sistemato. Gli spostamenti sono registrati (`printer.move`).

**Rinominare una coda.** Il pulsante con la matita apre una finestra con un campo vuoto per il nuovo nome (l'ultima parte dell'indirizzo `…/printers/<nome>`: lettere, cifre e `. _ @ + -`). CUPS non sa rinominare una coda, quindi il gateway crea la nuova sullo stesso dispositivo (stesso driver, descrizione e posizione) e solo dopo
toglie la vecchia; se qualcosa fallisce la vecchia resta. Blocco, nascondimento da mDNS, impostazioni della stampa di manutenzione, conto alla rovescia, posizioni negli elenchi, stampanti consentite a gruppi e utenti ([quote.md](quote.md#5-permessi-avanzati)), permessi concessi e richieste in attesa seguono il nuovo nome;
l'audit conserva il vecchio nome per ciò che è già successo. Una finestra di conferma avverte che **i computer e i telefoni configurati a mano con il vecchio indirizzo smetteranno di stampare** finché non vengono configurati di nuovo, e ha una spunta, attiva di default, per **avvisare tutti gli utenti nelle loro notifiche**
(tranne chi ha rinominato). Si rifiuta una coda con job in attesa, un nome già in uso o una coda di cui il gateway non può conoscere il driver (per esempio `socket://`). Le opzioni impostate a mano in CUPS non vengono riportate. Registrata come `printer.rename`.

**Nomi usati in passato.** Il gateway ricorda i nomi di code eliminate, tolte automaticamente o rinominate. Nella finestra di rinomina:

- **mentre scrivi**, se un nome passato comincia con le lettere scritte, il resto compare in grigio nel campo, solo come promemoria;
- **sotto il campo** una riga dice «Usato in passato: nessuna coda ha questo nome dal …», «Esiste già una coda con questo nome» (*Rinomina* si disattiva), «È già il suo nome» (disattivato) o un avviso per i caratteri non ammessi;
- **Nomi usati in passato (n)** li elenca dal più recente (al massimo 20, senza code esistenti) con il giorno e il motivo; cliccando un nome lo si scrive nel campo.

Un nome smette di essere «usato in passato» quando una coda lo riprende. A cosa serve: una stampante si rompe, la sua coda viene tolta, ne arriva una sostitutiva con un altro nome; rinominala con il **vecchio nome** e i computer e telefoni configurati a mano tornano a stampare con le stesse chiavi, e i permessi che
elencavano quel nome valgono di nuovo. Un computer configurato con un driver specifico del vecchio modello può dover essere configurato di nuovo se il modello è diverso; quelli senza driver (IPP Everywhere) no.

CUPS non dice quando una coda è stata creata: il gateway annota la **prima volta che la vede** (ogni minuto). Una coda eliminata perde il posto. L'annuncio mDNS resta alfabetico.

## 4. Stampa periodica di manutenzione (solo stampanti fisiche)

Il gateway può stampare da solo una piccola pagina di manutenzione a intervalli regolari, perché una stampante ferma non asciughi l'inchiostro. Pulsante con la stampante e il timer, in alto a destra su ogni scheda (assente per le virtuali).

- **Un'impostazione per stampante, uguale per tutti gli amministratori.** Il popup ha un **interruttore di attivazione** (agisce subito), l'**intervallo** (da 1 ora a 90 giorni; suggerita 1 settimana), la **stampa a colori** (se la stampante ha il colore) e **Salva**, che registra i parametri senza attivare o disattivare il programma.
- Il conto parte dall'attivazione e poi dall'ultima pagina **riuscita**; le stampe degli utenti non lo azzerano.
- Se la stampante non risponde si riprova appena è raggiungibile; se CUPS rifiuta il job si riprova dopo un'ora e l'errore resta sulla scheda. Se il gateway era spento, la pagina parte al riavvio (l'intervallo perso non si recupera).
- La pagina va a CUPS come `gw:maintenance`: **non consuma quota**, **non compare nell'audit delle stampe** e **ignora il blocco e la modalità manutenzione**. Accendere, spegnere o salvare è nell'audit degli amministratori (`printer.keepalive_on/off/save`).

## 5. Stampa di prova con le statistiche

Pulsante con l'insetto: conferma con **Annulla**, **Stampa bianco e nero**, **Stampa a colori** (disabilitato senza colore); stampa le stesse statistiche della scheda. Registrata come `printer.test_print`. A4. Le pagine che il gateway stampa da sé sono sempre in inglese ([lingue.md](lingue.md)).

## 6. Blocco, nascondimento, manutenzione

- **Blocca una stampante** — non accetta job da nessuno, **amministratori compresi**. Per coda.
- **Nascondi da mDNS** — non viene più annunciata sulla rete; resta usabile con l'indirizzo.
- **Manutenzione del gateway** (pulsante rosso con il cono stradale, in alto) — ferma stampa **e scansione** per tutti tranne gli amministratori e ritira ogni annuncio mDNS. Un banner rosso lo mostra su ogni pagina. La finestra di conferma ha una spunta, attiva di default, per **avvisare tutti gli utenti nelle loro notifiche** che la stampa è sospesa o torna a funzionare.

L'annunciatore mDNS interroga il gateway (`/mdns/policy`, autenticato da un codice derivato da `SPOOLSTEWARD_SECRET_KEY`). **Senza quella chiave il gateway rifiuta di rispondere e non viene annunciato nulla**: la risposta elenca le stampanti che un amministratore ha nascosto. `docker compose logs mdns` dice allora *"mDNS is OFF"*;
imposta la chiave in `.env` ([risoluzione-problemi.md](risoluzione-problemi.md#nessuna-chiave-segreta-impostata)). Se il log dice che il gateway non ha accettato l'annunciatore, i due container leggono chiavi diverse: `docker compose up -d --force-recreate gateway mdns`.

## 7. Stampanti di prova

In **Stampanti** un amministratore può creare ed eliminare **stampanti di prova** (`debugprinter0`, `1`, …): code CUPS-PDF che scrivono PDF e non usano carta. Per quote e audit si comportano come una stampante, stanno in fondo all'elenco e non hanno i pulsanti di manutenzione né di stampa di prova.
`CUPS_VIRTUAL_QUEUE=1` crea una coda `virtual` per lo stesso scopo.
