# Risoluzione dei problemi

[Indice della documentazione](../README.md) · [English](../en/troubleshooting.md) · Italiano

Prima di tutto, raccogli i fatti:

```sh
docker compose ps                                  # i servizi sono tutti su? cups è "healthy"?
docker compose logs --tail=100 gateway             # il log dell'applicazione
docker compose logs --tail=100 cups ipp-usb mdns
curl -sk https://<server>:8631/healthz             # 200 = gateway e database ok, 503 = no
```

Per più dettaglio metti `SPOOLSTEWARD_LOG_LEVEL=DEBUG` in `.env` e poi `docker compose up -d`.

## 1. Il gateway non parte

| Sintomo | Causa e rimedio |
|---|---|
| `refusing to start without TLS` | Manca il certificato. Esegui `scripts/docker-init.sh` o metti `fullchain.pem` + `privkey.pem` in `config/tls/`. |
| `refusing to start: wrong settings …` seguito da una riga per impostazione, per esempio `SPOOLSTEWARD_ESCL_WINDOW_SECONDS: Input should be greater than or equal to 60 (found: '30')` | Una riga di `.env` ha un valore che il programma non accetta: un numero fuori dal suo intervallo o che non è un numero, una parola che non è nell'elenco, una riga vuota (`NOME=`: scrivi un valore, o `none` dove l'impostazione può restare non impostata), un booleano che non è `true` / `false` in minuscolo, `LOG_LEVEL` non in maiuscolo. Le righe sbagliate sono elencate tutte insieme. Con `docker compose up -d` non lo vedi a schermo: `docker compose ps` mostra il portale che si riavvia o è uscito, e `docker compose logs --tail=20 gateway` stampa l'elenco. Correggi le righe ed esegui di nuovo `docker compose up -d`. |
| `SPOOLSTEWARD_RADIUS_HOST and SPOOLSTEWARD_RADIUS_SECRET: set both, or write none in both` | RADIUS è configurato a metà. Compila entrambi, o scrivi `none` in entrambi. |
| Permission denied su `config/tls/privkey.pem` | La chiave deve essere leggibile dall'utente `PUID:PGID` di `.env` (`chown`/`chmod 600`). |
| `Bind for 0.0.0.0:631 failed` | Un altro CUPS usa la 631. Tieni `PORTAL_PORT=8631` oppure ferma l'altro servizio. |
| "archive folder not usable" | `data/audit-archive` deve essere scrivibile dall'utente del contenitore (stesso uid di `PUID`). |
| A ogni riavvio tutti escono dalle sessioni, oppure la campanella dice **Nessuna chiave segreta impostata** | `SPOOLSTEWARD_SECRET_KEY` manca in `.env` o vale `none`: vedi [Nessuna chiave segreta impostata](#nessuna-chiave-segreta-impostata) più sotto. |
| `SPOOLSTEWARD_SECRET_KEY: still holds the placeholder of docker/env.example` | `docker/env.example` è stato copiato a mano in `.env`, quindi la chiave è ancora `__GENERATED__`, un valore che tutti conoscono: il gateway non parte. Scrivi l'output di `openssl rand -hex 32` al posto di `__GENERATED__`, controlla `PUID`, `PGID` e `PROJECT_DIR` (li avrebbe compilati lo script), poi `docker compose up -d`. |
| `SPOOLSTEWARD_SECRET_KEY: is shorter than 64 characters` | Una chiave scritta a mano è troppo corta per essere sicura. Scrivi al suo posto l'output di `openssl rand -hex 32` (64 caratteri), poi `docker compose up -d`. I nomi dei file salvati con la chiave vecchia compaiono poi come `[encrypted]` e tutti rifanno il login. |

### Nessuna chiave segreta impostata

Gli amministratori vedono questo avviso nella campanella:

> **Nessuna chiave segreta impostata.** `SPOOLSTEWARD_SECRET_KEY` manca in `.env` oppure vale `none`: i nomi dei file nell'audit sono
> salvati senza cifratura e dopo ogni riavvio tutti devono rifare il login. Scrivi un valore casuale (l'output di
> `openssl rand -hex 32`) dopo `SPOOLSTEWARD_SECRET_KEY=` in `.env`, esegui `docker compose up -d` e salva la chiave in un posto sicuro.
> Passo per passo.

**Cause.**

- `.env` viene da un'installazione precedente, o è stato scritto a mano, e non ha la riga `SPOOLSTEWARD_SECRET_KEY=`.
- `scripts/docker-init.sh` scrive una chiave solo quando crea `.env`. Non tocca mai un `.env` esistente, quindi eseguirlo di nuovo non
  aggiunge la chiave: stampa un avviso che rimanda qui.
- La riga c'è, con il valore `none`.

**Cosa vuol dire.**

- I nomi dei file stampati e scansionati sono salvati nell'audit senza cifratura ([protezione-dati.md](protezione-dati.md)).
- Il gateway crea una chiave casuale nuova a ogni avvio: dopo ogni riavvio tutti rifanno il login al portale, e il canale tra `gateway` e
  `mdns` non è protetto.
- Non si perde nulla. I nomi salvati senza chiave restano leggibili dopo che l'hai aggiunta: al primo avvio con la chiave il gateway li cifra.

**Come si risolve.**

1. Nella cartella di SpoolSteward apri `.env` con un editor.
2. Se c'è una riga `SPOOLSTEWARD_SECRET_KEY=none`, sostituisci `none` con l'output di `openssl rand -hex 32` (64 caratteri). Se la riga
   non c'è, aggiungila in fondo, con lo stesso tipo di valore. Il comando qui sotto la aggiunge senza mostrare la chiave a schermo; usalo
   solo quando la riga manca, perché il file deve avere una sola riga `SPOOLSTEWARD_SECRET_KEY=`:

   ```sh
   printf 'SPOOLSTEWARD_SECRET_KEY=%s\n' "$(openssl rand -hex 32)" >> .env
   ```

3. Esegui `docker compose up -d`. Tutti rifanno il login un'ultima volta; l'avviso si chiude da solo.
4. Copia la chiave in un posto sicuro, per esempio un password manager ([backup.md](backup.md#salva-la-chiave-segreta)). Senza di essa i
   nomi dei file di un backup non cifrato, una volta ripristinato, non si possono leggere.

## 2. Non riesco ad accedere

| Sintomo | Causa e rimedio |
|---|---|
| `docker compose logs gateway \| grep 'FIRST RUN'` non stampa nulla | Controlla in quest'ordine. **1.** `docker compose ps`: `gateway` deve essere *Up*; se si riavvia o si ferma, leggi `docker compose logs gateway \| tail -40`. **2.** La riga compare solo finché l'amministratore non ha una password. Se questa cartella è già stata usata, il database (il volume Docker `spoolsteward_portal-data`) ha ancora l'account con la vecchia password: il log dice allora *"administrator … already has a password"*; accedi su `https://<server>:8631/login`, oppure imposta una password nuova con `docker compose exec -it gateway spoolsteward-admin set-admin-password`. **3.** `SPOOLSTEWARD_BOOTSTRAP_ADMIN=false` in `.env` spegne l'account del primo avvio. Per ripartire davvero da zero (questo **cancella utenti, quote e audit**): `docker compose down -v`, poi `docker compose up -d`. |
| Bloccato dopo password sbagliate | 5 password *diverse* sbagliate in 5 minuti bloccano la coppia (utente, IP). Attendi 5 minuti. Il blocco sopravvive al riavvio (`/data/login-lockout.json`); per toglierlo subito ferma il gateway, cancella quel file nel volume e riavvia. |
| Un utente RADIUS non entra | RADIUS irraggiungibile ⇒ accesso negato (fail closed). Controlla `SPOOLSTEWARD_RADIUS_*`, che questa macchina sia un *client* RADIUS con lo stesso secret, e il log. Un account locale con lo stesso nome **non** viene mai aperto dal RADIUS. |
| Obbligo di cambiare password | Normale per le password temporanee create o reimpostate da un amministratore. |
| Un utente disabilitato stampa ancora | Disabilitare un account nel portale blocca subito l'account e tutte le sue chiavi. Se la persona è stata tolta solo dal server RADIUS, le sue chiavi di stampa funzionano fino alla scadenza (al massimo `SPOOLSTEWARD_KEY_TTL_DAYS_RADIUS`): disabilita l'account, o revoca le chiavi, nel portale. |

## 3. Stampa

| Sintomo | Causa e rimedio |
|---|---|
| Il client chiede sempre la password | La **password di dominio non vale per stampare**: serve una chiave di stampa da *Profilo* (`SPOOLSTEWARD_IPP_AUTH=keys`). Oppure la coppia (utente, IP) è bloccata. |
| `Forbidden` / HTML 426 da CUPS | La policy di CUPS non è quella del gateway: serve `docker/stack/cups/cupsd.conf` (`DefaultAuthType None`). |
| 502 dal gateway | CUPS fermo o `SPOOLSTEWARD_CUPS_URL` errato. `docker compose logs cups`. |
| Job rifiutato: «la stampa è consentita solo…», «la stampa a colori non è consentita…», «non può usare quella stampante» | I permessi del gruppo dell'utente o la sua regola ([quote.md](quote.md)). Il profilo dell'utente elenca cosa è consentito e permette di fare una richiesta. |
| Job rifiutato con un messaggio di quota | L'utente ha raggiunto un limite; il messaggio contiene il link al portale per chiederne altra. Vedi [quote.md](quote.md). |
| "printer disabled…" / "gateway in maintenance…" | Un amministratore ha bloccato la stampante o ha attivato la manutenzione ([stampanti.md](stampanti.md)). Durante la manutenzione gli amministratori possono comunque stampare. |
| Un job resta `accepted` per sempre | Il riconciliatore non raggiunge CUPS: cerca `reconcile:` nel log. |
| La stampante risulta NON RAGGIUNGIBILE e manca nel modulo di stampa | Il dispositivo non risponde (spento, scollegato, USB in sospensione). Il rilevamento può ritardare fino a ~30 s. Per le code di rete è una prova TCP: non sa se manca la carta. |
| `:60000` non risponde | `ipp-usb` non gira o la stampante non è vista: `docker compose logs ipp-usb`; ricollega l'USB; assicurati che `ipp-usb`/SANE **dell'host** siano fermati e mascherati. |
| Una stampante USB nuova non compare | Attendi fino a un minuto o premi **Cerca ora** in Stampanti. Controlla `docker compose logs gateway \| grep discovery`. Se la pagina dice "CUPS non ha un driver", il modello non ha driver nell'immagine `cups` (aggiungine uno in `docker/stack/Dockerfile`). Assicurati che `ipp-usb`/SANE **dell'host** siano fermati e che `SPOOLSTEWARD_AUTO_DISCOVER` non sia `false`. |
| Una stampante scollegata è ancora in elenco | Mostra NON RAGGIUNGIBILE con un conto alla rovescia (code USB, `SPOOLSTEWARD_OFFLINE_REMOVE_HOURS`, default 8 h) e un pulsante **Elimina coda**. Le code di rete non vengono mai tolte da sole. |
| Una coda che ho eliminato non torna, anche se è in `CUPS_QUEUES` | È voluto: ogni coda di `CUPS_QUEUES` (e `virtual`) viene creata una sola volta. Per crearla di nuovo: `scripts/queue-restore.sh NOME --reboot` (toglie il nome dall'elenco delle code create e riavvia il contenitore `cups`; senza `--reboot` dimentica solo il nome e la coda torna al prossimo avvio). `scripts/queue-restore.sh --list` mostra i nomi ricordati. La coda deve essere ancora in `CUPS_QUEUES` (oppure essere `virtual` con `CUPS_VIRTUAL_QUEUE=1`). |
| La coda non è stata creata | `CUPS_QUEUES` è sbagliato o la stampante ha risposto tardi. L'entrypoint riprova per 5 minuti; altrimenti `docker compose exec cups lpadmin -p NOME -E -v URI -m everywhere`. |
| Un "Create-Job" senza documento tiene la quota | La quota si libera solo quando CUPS lo annulla (di default dopo 3 h). |
| Limite pagine superato di un file | Il limite è *morbido* per i file di cui non si contano le pagine (formato sconosciuto, PDF cifrato, oltre 64 MB): vedi [limiti.md](limiti.md). |
| Stampa da browser: "file non supportato" | Si accettano solo PDF, JPEG, PNG, GIF e testo semplice, riconosciuti dal contenuto e non dal nome del file. |
| Stampa da browser: "più grande di N MB" | `SPOOLSTEWARD_WEB_UPLOAD_MB` (predefinito 25). |

## 4. Telefoni e computer

| Sintomo | Causa e rimedio |
|---|---|
| Avviso sul certificato, o il dispositivo rifiuta di collegarsi | Il dispositivo non si fida del certificato. Installa la CA, oppure usa un certificato firmato da una CA in `config/tls/`. Il nome o l'IP usato deve stare nel certificato. |
| Le stampanti non compaiono sul telefono | `mdns` deve girare con la rete host sulla stessa LAN; `docker compose logs mdns`. Imposta `SPOOLSTEWARD_MDNS_ADDRESSES` se la macchina ha più schede di rete e `SPOOLSTEWARD_MDNS_HOSTNAME` con un nome presente nel certificato. Docker Desktop non può farlo. Una stampante nascosta, irraggiungibile, o la modalità manutenzione, tolgono anche l'annuncio. |
| Android: il servizio di sistema vede la stampante ma fallisce | Non sa autenticarsi. Installa **Mopria Print Service** o stampa dal portale. |
| Il desktop Linux mostra le stampanti ma non stampa | Le stampanti annunciate non hanno la chiave. GNOME non sa aggiungerla dalla sua finestra: esegui il comando `lpadmin` della pagina «Crea una chiave» o `scripts/add-printer-linux.sh`, che la salvano nell'indirizzo della coda. |
| Windows: «Operazione non riuscita» o «sto connettendo» troppo a lungo quando aggiungi la stampante (procedura completa in [windows.md](windows.md)) | Windows chiede al gateway se il lavoro andrebbe bene (`Validate-Job`) prima di avere una password, e la finestra «Aggiungi dispositivo» non sa chiederla. Il gateway risponde a questa domanda senza password, mentre la stampa vera chiede sempre il login. Aspetta 3-4 minuti; se non si collega: Pannello di controllo → Account utente → Gestione credenziali → Gestione credenziali Windows → Aggiungi credenziali Windows; come indirizzo scrivi lo stesso nome o IP della stampante, come utente il tuo nome e come password la **chiave di stampa** (non la password dell'account). |
| macOS / iPhone | Non provati dal progetto. Prova `ipps://<server>:8631/printers/<coda>`, nome utente + chiave di stampa, e segnala cosa succede. |

## 5. Scansione

| Sintomo | Causa e rimedio |
|---|---|
| Nessuno scanner nel portale | Gli scanner si trovano se rispondono a eSCL sulla porta IPP della coda o sulla 80. Altrimenti aggiungi `SPOOLSTEWARD_SCAN_URL=nome=http://host:porta`. Uno scanner scollegato sparisce in ~30 s. |
| "scanner occupato" | Un'altra scansione è in corso, o il client non ha cancellato il job eSCL precedente. Il gateway riprova 3 volte se il dispositivo si sta svegliando. |
| Pagina bianca dal caricatore | Il caricatore dormiva e si dichiarava vuoto, quindi *Automatico* ha usato il vetro. Scegli *Caricatore* esplicitamente o rimetti i fogli. |
| Ho premuto il pulsante della finestra di scansione ma la scansione è rifiutata | La campanella dice «la finestra era per X, la richiesta arriva da Y». I due indirizzi differiscono: IPv4 contro IPv6, NAT o un proxy (vedi i limiti in [scansione.md](scansione.md)). Apri il portale dal dispositivo su cui gira l'app di scansione, con lo stesso tipo di indirizzo che usa l'app; dietro un reverse proxy aprilo direttamente sulla porta 8631. L'audit ha una riga *negata* con i due indirizzi. |
| Non c'è il pulsante della finestra di scansione | `SPOOLSTEWARD_ESCL_WINDOW=false`, oppure nessuno scanner è collegato. |
| NAPS2 riceve 401 senza finestra | Non sa inviare le credenziali. Imposta `SPOOLSTEWARD_ESCL_ANONYMOUS=true` (solo su reti fidate) e inserisci IP e porta a mano. |
| Il risultato della scansione è sparito | I risultati restano 15 minuti in memoria, solo per il proprietario. |

## 6. Backup e audit

| Sintomo | Causa e rimedio |
|---|---|
| Il backup è rosso | È fallito o l'ultimo ha più di 48 h. Apri la pagina per il motivo; di solito la cartella non è scrivibile o il disco è pieno. |
| "Folder name not valid…", "Folder is outside the backup root", "Folder … is not writable by the gateway" | La cartella di un backup deve essere una sottocartella di `/backups` (sull'host `data/backups`) e scrivibile dall'utente `PUID`. Un disco USB o una cartella di rete va montata sull'host *dentro* quella cartella **prima** di avviare il gateway (riavvia il gateway dopo il montaggio). |
| Passphrase persa | I backup cifrati non si aprono senza (non esiste un reset). Crea una nuova chiave di recupero (`backup-key`) per i backup futuri. |
| Ripristino | Un file danneggiato o troncato, una passphrase sbagliata o un backup senza database vengono rifiutati con un messaggio e il database attuale non viene toccato. Per ripristinare: ferma `gateway` e `mdns`, poi `docker compose run --rm -it --no-deps gateway spoolsteward-admin backup-restore /backups/<cartella>/<file>`, poi `docker compose up -d`. Controlla prima il file con `backup-verify`. |
| Il database è grande | L'audit non si cancella mai: viene archiviato in `data/audit-archive` quando supera `SPOOLSTEWARD_AUDIT_MAX_MB`. Abbassa il valore per archiviare prima. |

## 7. Lingua del portale

| Sintomo | Causa e rimedio |
|---|---|
| Le pagine sono in inglese ma ti aspettavi l'italiano | L'ordine è: scelta dell'utente → lingua del browser → `SPOOLSTEWARD_LANGUAGE` → inglese. Imposta `SPOOLSTEWARD_LANGUAGE=it` o scegli la lingua in *Impostazioni*. |
| Qualche testo non è tradotto | I testi che arrivano dalla stampante (nomi degli inchiostri, firmware, avvisi del dispositivo) o da CUPS si mostrano come arrivano. I motivi nell'audit sono in inglese di proposito. |
| I caratteri cinesi sono quadratini | Sul computer *del browser* manca un font CJK. |
| Le pagine di prova o di manutenzione stampate sono in inglese | È voluto: russo e cinese richiederebbero un font PDF incorporato ([lingue.md](lingue.md)). |

## 8. Aggiornamenti e certificati

| Sintomo | Causa e rimedio |
|---|---|
| `scripts/update.sh` dice `SPOOLSTEWARD_REGISTRY is empty` | La riga `SPOOLSTEWARD_REGISTRY=` di `.env` ha perso il suo valore. Riscrivila come in `docker/env.example` (`SPOOLSTEWARD_REGISTRY=ghcr.io/deh-aleh`) ed esegui di nuovo lo script. |
| `scripts/update.sh` dice «This folder is a git clone» | Lo script aggiorna una cartella creata dal pacchetto della release. Un clone git è una copia da sviluppatore e non si aggiorna così ([CONTRIBUTING.md](../../CONTRIBUTING.md)). |
| `the package does not match its sha256 sum` | Il file scaricato non è quello pubblicato (download interrotto, o una rete che lo modifica). Non è stato cambiato nulla: riprova; se persiste, scaricalo dal browser dalla pagina delle release. |
| `NOT healthy after the update: going back` | La versione nuova non è diventata sana in due minuti. Lo script ha rimesso i file e la versione di prima. Guarda `docker compose logs --tail 50 gateway` per capire perché. |
| Dopo l'aggiornamento non parte né la versione nuova né la vecchia | La nuova ha già cambiato il database. `update.sh` ti propone di ripristinare il backup preso all'inizio (`YES`); a mano: `scripts/backup.sh restore data/backups/pre-update/<file>`. |
| Il pulsante *Aggiorna* ha detto «rolled back» | Dal portale la nuova versione non è partita e c'è di nuovo la vecchia. Il ripristino del backup, se serve, lo fai tu: `scripts/backup.sh restore data/backups/pre-update/<file>`. |
| Una variabile nuova della versione non è in `.env` | Normale: l'aggiornamento non tocca `.env` e la variabile usa il suo valore predefinito. Per cambiarla copia la riga da `docker/env.example`. |
| Avviso «Backup non eseguito» nella campanella | Il disco di rete o USB dei backup non è montato (o il backup è fallito per un altro motivo, scritto nell'avviso). Rimonta, poi *Esegui ora* ([backup.md](backup.md)). |
| Avviso sul certificato nella campanella | Vedi [certificati.md](certificati.md): ogni avviso dice cosa è successo e cosa fare. |
| `scripts/cert-sync.sh` scrive un errore | La tabella dei messaggi è in [certificati.md](certificati.md#54-se-qualcosa-va-storto). |
| I nomi dei file nell'audit sono `[encrypted]` | `SPOOLSTEWARD_SECRET_KEY` è stata cambiata dopo averli salvati. Rimetti la chiave di prima in `.env` e `docker compose up -d` ([protezione-dati.md](protezione-dati.md)). |

## 9. Ancora bloccato

Prima di chiedere aiuto raccogli `docker compose logs gateway`, `docker compose ps`, l'esito di `/healthz`, la versione del gateway
(`SPOOLSTEWARD_VERSION` in `.env`) e ciò che mostrava la pagina. Non incollare mai `.env` o la chiave TLS.
