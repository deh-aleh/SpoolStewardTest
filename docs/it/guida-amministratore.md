# Guida per l'amministratore

[Indice della documentazione](../README.md) · [English](../en/admin-guide.md) · Italiano

Per chi gestisce l'installazione. Il portale è su `https://<server>:8631/` (accesso su `/login`, configurazione del primo avvio su `/setup`). Il menu in alto:

| Menu | Pagina | Cosa ci fai |
|---|---|---|
| **Audit** | `/admin/jobs` | Chi ha stampato o scansionato cosa; filtri, CSV, report d'uso (`/admin/report`), annullare job in coda → [audit.md](audit.md) |
| **Utenti** | `/admin/users` | Crea utenti locali o RADIUS, modifica, reimposta la password, revoca le chiavi, elimina |
| **Gruppi** | `/admin/groups` | Crea, rinomina, limiti, "richiedibile", elimina |
| **Richieste** | `/admin/requests` | Approva, modifica o rifiuta le richieste di quota, di gruppo e di permesso (contatore = in attesa); concessioni attive, quota extra o permesso dati o revocati direttamente, storico → [quote.md](quote.md) |
| **Azioni** | `/admin/actions` | Chi ha fatto cosa nel portale |
| **Stampanti** | `/admin/printers` | Stato, inchiostro, contatori; blocco, nascondimento, rinomina, sposta su/giù (il tuo ordine), stampanti di prova, stampa periodica e di prova → [stampanti.md](stampanti.md) |
| **Backup** | `/admin/backups` | I tuoi backup programmati → [backup.md](backup.md) |
| **Archivio** | `/admin/archive` | Audit vecchio compresso, consultabile e scaricabile |
| **Stampa / Scansiona / Profilo** | `/me…` | Le stesse pagine degli utenti → [guida-utente.md](guida-utente.md) |
| **Logo, nome e (utente)** | `/me/settings` | Il blocco in alto a sinistra apre le impostazioni: tema, lingua, aggiornamento |
| **Campanella** (solo amministratori) | `/admin/notices` | Cose che il gateway ha notato (il numero indica quelle aperte), vedi «Avvisi» sotto |
| **?** (tutti) | `/docs` | Questa documentazione, in italiano o inglese (selettore in cima a ogni pagina). Gli utenti che non sono amministratori vedono solo la guida utente e la guida Windows |
| **Manutenzione** (cono stradale rosso) | — | Ferma stampa e scansione per tutti tranne gli amministratori |
| **Porta con freccia** | — | Esci |

## 1. Primi passi

1. Imposta la password di `administrator` con il codice monouso ([installazione.md](installazione.md#36-primo-amministratore)).
2. Crea il **tuo** account amministratore (Utenti → crea → amministratore) e usa quello ogni giorno. `administrator` è l'account di emergenza: non appartiene a nessun gruppo e nulla lo limita (né quote né permessi), quindi tienilo per il recupero.
3. Modifica i limiti del gruppo **default** (Gruppi) e crea gruppi per chi ha bisogno di limiti diversi.
4. Crea gli utenti, o lascia entrare quelli RADIUS. Di' a tutti di creare una chiave di stampa ([guida-utente.md](guida-utente.md)).
5. Imposta un backup ([backup.md](backup.md)).

## 2. Utenti

L'elenco è in sola lettura: limite proprio o ereditato (in corsivo), concessioni attive come `+N` colorato, consumo e chiavi attive.
**Modifica** apre la pagina con tutti i campi, che mostra l'**origine** dell'account (locale o RADIUS, solo informativa), ed **Elimina** (scrivi il nome per confermare).

- Crea **locale**: si mostra una password temporanea una volta; l'utente la cambia al primo accesso.
- Crea **RADIUS**: scegli il gruppo; la persona entra con la password d'ufficio.
- **Reimposta password** (locale): nuova password temporanea, mostrata una volta.
- **Chiavi**: l'elenco per utente è in `/admin/users/<id>/keys`. Per ogni chiave puoi **disattivarla** (la scadenza continua a scorrere; poi la riattivi), **prolungarla** (la nuova scadenza parte da oggi, il tempo rimasto non si somma) o **revocarla**; «Revoca tutte» ferma ogni dispositivo dell'utente. L'utente riceve una notifica per ogni azione (non per le sue). Le chiavi revocate e scadute spariscono dopo `SPOOLSTEWARD_KEY_HISTORY_DAYS` giorni (14).
- **Flag admin**: la promozione sposta l'utente nel gruppo `administrator`, il declassamento in `default`.
- Protetti: non puoi disattivare, declassare o eliminare te stesso, l'account di emergenza o l'ultimo amministratore.

Eliminare conserva lo storico ([account.md](account.md#7-eliminare-un-utente)).

## 3. Gruppi e limiti

Un gruppo ha dei limiti (job / pagine, settimanali / mensili) e un flag **richiedibile**. I gruppi di sistema `default` e `administrator`
non si eliminano; eliminare un altro gruppo sposta i suoi utenti in `default`. I limiti personali si impostano nella pagina di modifica
dell'utente. Dettagli in [quote.md](quote.md).

**Permessi avanzati.** In fondo alla pagina del gruppo e a quella dell'utente, il pulsante **Permessi avanzati** apre colore, stampa solo
fronte, quali stampanti, e quali giorni e orari. Il campo di un utente è "come il gruppo" oppure ha un valore proprio. L'amministratore di
emergenza è esente e nella sua pagina il blocco è spento. `SPOOLSTEWARD_PERMISSION_MODE` decide se un client che chiede in modo esplicito il
colore o una stampa solo fronte non consentiti viene corretto (`adjust`, predefinito) o rifiutato (`refuse`). Leggi le note in
[quote.md](quote.md#5-permessi-avanzati) prima di scegliere: con `refuse` molti lavori da Windows e Android vengono rifiutati. All'utente
arriva un avviso nella campanella quando cambi i suoi permessi propri, e quando un suo lavoro viene rifiutato o corretto.

## 4. Richieste e concessioni

È tutto in una pagina, *Richieste*, divisa in tre parti:

- **Richieste in attesa**: modifica quantità o tipo prima di approvare, aggiungi una sola nota (l'utente la vede, sia che approvi sia che rifiuti). Una richiesta di permesso (colore, solo fronte, una stampante, fuori orario) si approva *permanente* oppure *fino alla fine del* giorno che scegli. Se due amministratori approvano insieme, o con un doppio clic, la concessione è una sola.
- **Concessioni attive**: la quota extra e i permessi che contano adesso; li revochi, o ne dai uno direttamente con una nota (nel modulo scegli *quota* o *un permesso*).
- **Storico** (chiuso finché non lo apri): le ultime decisioni e le concessioni che non contano più (finite o revocate).
  *Elimina le concessioni scadute* le toglie dall'elenco; quelle attive non vengono toccate, e il registro degli amministratori
  (*Azioni*) conserva chi ha concesso cosa e chi le ha eliminate. Toglie anche le concessioni di permesso finite.
  Altri dettagli in [quote.md](quote.md#chiedere-un-permesso).

Tutto viene registrato.

## 5. Stampanti

Blocca una stampante (nessuno stampa, amministratori compresi), nascondila da mDNS, crea stampanti di prova, imposta la stampa periodica
di manutenzione, invia una stampa di prova con le statistiche, attiva la **manutenzione del gateway** (pulsante rosso con il cono stradale). Vedi
[stampanti.md](stampanti.md).

## 6. Audit e archivio

Usa i filtri e l'esportazione CSV; annulla i job in coda; leggi i file archiviati; verifica la loro integrità. Nulla si può cancellare
([audit.md](audit.md)).

## 7. Riga di comando (`spoolsteward-admin`)

Dentro il contenitore: `docker compose exec gateway spoolsteward-admin <comando>` (aggiungi `-it` quando chiede una password).

| Comando | Scopo |
|---|---|
| `set-admin-password [utente]` | crea o aggiorna un amministratore **locale** (predefinito `administrator`) e ne imposta la password |
| `make-admin <utente>` | concede i diritti di amministratore (crea l'utente se serve) |
| `set-group <utente> <gruppo>` | sposta un utente in un gruppo (lo crea) |
| `group-limit <gruppo> <limite\|none> [--period weekly\|monthly] [--metric jobs\|pages]` | imposta un limite di gruppo; `none` lo toglie |
| `user-limit <utente> <limite\|none> [--period …] [--metric …]` | imposta un limite personale |
| `jobs [-n N]` | mostra le ultime righe dell'audit (e avvisa se esistono archivi) |
| `audit-archive [--force]` | archivia subito le righe vecchie dell'audit |
| `backup-key` | crea una chiave di recupero dei backup da una passphrase |
| `backup-run [--id N]` | esegue subito i backup abilitati |
| `backup-verify <file>` | controlla un file di backup |
| `backup-restore <file>` | ripristina database e file dell'audit (ferma prima il gateway) |

## 8. Avvisi (la campanella)

La campanella nella barra in alto (solo amministratori) elenca ciò che il gateway ha notato e nessuno ha chiesto. Ogni avviso mostra
l'ultima volta, la fonte e quante volte è successo; *Chiudi* lo chiude (la chiusura resta registrata in *Azioni*).
I tipi di avviso:

| Avviso (come appare nella campanella) | Significato | Si chiude da solo quando |
|---|---|---|
| Una richiesta di scansione senza nome utente e password da IP è stata rifiutata | `SPOOLSTEWARD_ESCL_ANONYMOUS` è spento e un client non ha inviato credenziali ([scansione.md](scansione.md)) | mai (chiudilo tu) |
| **Backup non eseguito: la cartella di destinazione non è raggiungibile** | Manca il file marcatore della destinazione (disco non montato); non è stato scritto nulla ([backup.md](backup.md)) | il backup successivo riesce |
| **Backup fallito** | Qualunque altro errore, con il motivo; entrambi gli avvisi riportano il nome vero del backup | il backup successivo riesce |
| **Il certificato TLS è scaduto** | I client non si fidano più del gateway ([certificati.md](certificati.md)) | c'è un certificato valido e il gateway è riavviato |
| **Non è stato possibile aggiornare il certificato** | L'ultima esecuzione di `scripts/cert-sync.sh` è fallita | l'esecuzione successiva riesce |
| **L'indirizzo annunciato non è nel certificato TLS** | I client che trovano le stampanti via mDNS ricevono un errore di certificato | il SAN elenca l'indirizzo |
| **Il certificato TLS scade il (data)** | Solo con `SPOOLSTEWARD_CERT_EXPIRY_WARN_ENABLED=true` | il certificato è rinnovato |
| **Inceppamento carta** / **Carta finita** | Una stampante fisica lo segnala (controllo ogni minuto, segnalato dopo due controlli consecutivi) | la stampante smette di segnalarlo |
| **Inchiostro o toner esaurito** / **in esaurimento** | Esaurito: livello 0, oppure lo dice lo stato della stampante. In esaurimento: la soglia «basso» della stampante. Entrambi indicano i materiali | la stampante smette di segnalarlo |
| **Salva la chiave segreta in un posto sicuro** | Una volta, dopo il primo avvio: copia `SPOOLSTEWARD_SECRET_KEY` da `.env`. Arriva all'amministratore del primo avvio (`SPOOLSTEWARD_BOOTSTRAP_ADMIN_USERNAME`) nella sua campanella, o a tutti gli amministratori se quell'account non esiste o `SPOOLSTEWARD_BOOTSTRAP_ADMIN=false`. Non si ripete a un riavvio; si ripete una volta se la chiave cambia | mai (chiudilo tu) |
| **La chiave segreta è cambiata** | `SPOOLSTEWARD_SECRET_KEY` è diversa da quella dell'avvio precedente; dice quanti nomi di file non si leggono più | mai (chiudilo tu) |
| **Nessuna chiave segreta impostata** | `SPOOLSTEWARD_SECRET_KEY` è `none` o manca: i nomi dei file non sono cifrati e le sessioni finiscono a ogni riavvio | la chiave viene impostata |

**Problemi di stampante e utenti.** Gli avvisi sulle stampanti qui sopra sono sempre per gli amministratori. Con `SPOOLSTEWARD_PRINTER_ALERTS_USERS=true` (predefinito) chi ha una
stampa in attesa su quella stampante riceve un avviso anche nella propria campanella (inceppamento, carta e inchiostro finiti; non «in esaurimento»), una volta per problema e solo
lui, non tutti. Si chiude da solo quando la stampante torna a posto. Con `false` vengono avvisati solo gli amministratori.

**Una sola campanella.** Un amministratore ha una sola campanella: `/admin/notices` mostra prima gli avvisi sul proprio account («Per te»: chiavi, gruppo, quota, richieste)
e poi quelli del gateway, e il numero rosso somma entrambi. `/me/notices` manda gli amministratori lì.

Perché un client che riprova 100 volte non riempia l'elenco, lo stesso avviso dalla stessa fonte viene scritto di nuovo solo dopo un
intervallo; le ripetizioni nel frattempo vengono contate. L'intervallo dipende da `SPOOLSTEWARD_LOG_LEVEL` e lo stesso testo viene scritto
nel log del container con lo stesso limite:

| `SPOOLSTEWARD_LOG_LEVEL` | Stesso avviso, stessa fonte, segnalato di nuovo dopo |
|---|---|
| `DEBUG` | 1 minuto |
| `INFO` (predefinito) | 1 giorno |
| `WARNING` | 3 giorni |
| `ERROR` | 7 giorni |

`SPOOLSTEWARD_NOTICE_INTERVAL` (secondi) sostituisce la tabella. Si tengono al massimo 200 avvisi aperti per tipo; quelli chiusi vengono rimossi dopo 90 giorni.

## 9. Operatività

- Salute: `GET /healthz` → 200 o 503, corpo `{"status":"ok"}`. Non richiede login. `SPOOLSTEWARD_HEALTHZ_INFO=version,printers` aggiunge la versione e il numero di code (usb / rete / virtuali / totale); il predefinito è solo lo stato. Log: `docker compose logs gateway`.
- Le migrazioni partono da sole all'avvio. Fai un backup prima di aggiornare un database importante.
- Nomi dei file nell'audit: il pulsante verde/rosso in alto a destra nella pagina *Audit* smette di salvarli, `.env` può cancellarli dopo qualche giorno e sono conservati cifrati ([protezione-dati.md](protezione-dati.md)). Non cambiare `SPOOLSTEWARD_SECRET_KEY`.
- Aggiornamenti: in cima al portale compare un avviso quando esiste una release più nuova. Aggiorna con `scripts/update.sh`, oppure con il pulsante *Aggiorna* nel menu *Backup* quando gira il contenitore facoltativo updater e c'è un backup programmato funzionante ([installazione.md](installazione.md#5-aggiornamenti)). L'updater controlla Docker, cioè tutto il server: tienilo spento se non ti serve.
- Non lasciare account o stampanti di prova su un'installazione vera.
- Proteggi `config/`, `.env`, `data/` e il volume: contengono segreti e dati personali.
