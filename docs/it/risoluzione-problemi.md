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

CPU e RAM: `scripts/resources.sh` elenca i container e Docker stesso e li somma. Se mostra 0 MB ovunque, il kernel non misura la memoria per container: su un Raspberry Pi aggiungi `cgroup_enable=memory cgroup_memory=1` all'unica riga di `/boot/firmware/cmdline.txt` e riavvia.

Ogni servizio conserva solo gli ultimi 30 MB di log (3 file da 10 MB): le righe più vecchie sono perse, quindi guarda presto dopo un problema.

## 1. Il gateway non parte

| Sintomo | Causa e rimedio |
|---|---|
| `refusing to start without TLS` | Manca il certificato. Esegui `scripts/docker-init.sh` o metti `fullchain.pem` + `privkey.pem` in `config/tls/`. |
| `refusing to start: wrong settings …` seguito da una riga per impostazione, per esempio `SPOOLSTEWARD_ESCL_WINDOW_SECONDS: Input should be greater than or equal to 60 (found: '30')` | Una riga di `.env` ha un valore che il programma non accetta (vedi [configurazione.md](configurazione.md)). Le righe sbagliate sono elencate tutte insieme, ma `docker compose up -d` non le mostra: `docker compose ps` mostra il portale che si riavvia e `docker compose logs --tail=20 gateway` stampa l'elenco. Correggi le righe ed esegui di nuovo `docker compose up -d`. |
| `SPOOLSTEWARD_RADIUS_HOST and SPOOLSTEWARD_RADIUS_SECRET: set both, or write none in both` | RADIUS è configurato a metà. Compila entrambi, o scrivi `none` in entrambi. |
| Permission denied su `config/tls/privkey.pem` | La chiave deve essere leggibile dall'utente `PUID:PGID` di `.env` (`chown`/`chmod 600`). |
| `Bind for 0.0.0.0:631 failed` | Un altro CUPS usa la 631. Tieni `PORTAL_PORT=8631` oppure ferma l'altro servizio. |
| "archive folder not usable" | `data/audit-archive` deve essere scrivibile dall'utente del contenitore (stesso uid di `PUID`). |
| A ogni riavvio tutti escono dalle sessioni, oppure la campanella dice **Nessuna chiave segreta impostata** | `SPOOLSTEWARD_SECRET_KEY` manca in `.env` o vale `none`: vedi [Nessuna chiave segreta impostata](#nessuna-chiave-segreta-impostata) più sotto. |
| `SPOOLSTEWARD_SECRET_KEY: still holds the placeholder of docker/env.example` | `docker/env.example` è stato copiato a mano in `.env`, quindi la chiave è ancora il pubblico `__GENERATED__`. Scrivi al suo posto l'output di `openssl rand -hex 32`, controlla `PUID`, `PGID` e `PROJECT_DIR` (li avrebbe compilati lo script), poi `docker compose up -d`. |
| `SPOOLSTEWARD_SECRET_KEY: is shorter than 64 characters` | Una chiave scritta a mano è troppo corta. Scrivi al suo posto l'output di `openssl rand -hex 32`, poi `docker compose up -d`. Tutti rifanno il login e i nomi dei file cifrati con la chiave vecchia compaiono come `[encrypted]`. |

### Nessuna chiave segreta impostata

Gli amministratori vedono un avviso nella campanella: `SPOOLSTEWARD_SECRET_KEY` manca in `.env` oppure vale `none`.

**Causa.** `.env` viene da un'installazione precedente o è stato scritto a mano, e non ha la riga o ha `none`. `scripts/docker-init.sh` scrive una chiave solo quando
crea `.env`; non tocca mai uno esistente (stampa un avviso che rimanda qui).

**Cosa vuol dire.** Il gateway crea una chiave casuale nuova a ogni avvio: dopo ogni riavvio tutti rifanno il login e `mdns` non annuncia nulla (senza la chiave
l'annunciatore non può provare chi è al gateway). Non si perde nulla: utenti, quote e audit restano come sono.

**Come si risolve.**

1. Apri `.env` nella cartella di SpoolSteward.
2. Se la riga è `SPOOLSTEWARD_SECRET_KEY=none`, sostituisci `none` con l'output di `openssl rand -hex 32`. Se la riga non c'è, aggiungila con il comando qui
   sotto (solo quando manca: il file deve avere una sola riga così):

   ```sh
   printf 'SPOOLSTEWARD_SECRET_KEY=%s\n' "$(openssl rand -hex 32)" >> .env
   ```

3. Esegui `docker compose up -d`. Tutti rifanno il login un'ultima volta; l'avviso si chiude da solo.
4. Copia la chiave in un posto sicuro, per esempio un password manager ([backup.md](backup.md#salva-la-chiave-segreta)).

## 2. Non riesco ad accedere

| Sintomo | Causa e rimedio |
|---|---|
| `docker compose logs gateway \| grep 'FIRST RUN'` non stampa nulla | Controlla in quest'ordine. **1.** `docker compose ps`: `gateway` deve essere *Up*; se si riavvia, leggi `docker compose logs gateway \| tail -40`. **2.** La riga compare solo finché l'amministratore non ha una password. Se questa cartella è già stata usata, il database (volume `spoolsteward_portal-data`) ha ancora l'account: il log dice *"administrator … already has a password"*; accedi su `https://<server>:8631/login`, oppure imposta una password nuova con `docker compose exec -it gateway spoolsteward-admin set-admin-password`. **3.** `SPOOLSTEWARD_BOOTSTRAP_ADMIN=false` spegne l'account del primo avvio. Per ripartire davvero da zero (questo **cancella utenti, quote e audit**): `docker compose down -v`, poi `docker compose up -d`. |
| Bloccato dopo password sbagliate | 5 password *diverse* sbagliate in 5 minuti bloccano la coppia (utente, IP) per 5 minuti, anche dopo un riavvio. Per toglierlo subito ferma il gateway, cancella `/data/login-lockout.json` nel volume e riavvia. |
| Un utente RADIUS non entra | RADIUS irraggiungibile ⇒ accesso negato. Controlla `SPOOLSTEWARD_RADIUS_*`, che questa macchina sia un *client* RADIUS con lo stesso segreto, e il log. |
| Obbligo di cambiare password | Normale per le password temporanee create o reimpostate da un amministratore. |
| Un utente disabilitato stampa ancora | Disabilitare un account nel portale blocca subito l'account e tutte le sue chiavi. Se la persona è stata tolta solo dal server RADIUS, le sue chiavi funzionano fino alla scadenza: disabilita l'account, o revoca le chiavi, nel portale. |

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
| La stampante risulta NON RAGGIUNGIBILE e manca nel modulo di stampa | Il dispositivo non risponde (spento, scollegato, USB in sospensione). Il rilevamento può ritardare ~30 s; per le code di rete è una prova TCP. |
| `:60000` non risponde | `ipp-usb` non gira o la stampante non è vista: `docker compose logs ipp-usb`; ricollega l'USB; assicurati che `ipp-usb`/SANE **dell'host** siano fermati e mascherati. |
| Una stampante USB nuova non compare | Attendi fino a un minuto o premi **Cerca ora** in Stampanti; controlla `docker compose logs gateway \| grep discovery`. Se la pagina dice "CUPS non ha un driver", aggiungine uno in `docker/stack/Dockerfile`. Assicurati che `ipp-usb`/SANE **dell'host** siano fermati e che `SPOOLSTEWARD_AUTO_DISCOVER` non sia `false`. |
| Una stampante scollegata è ancora in elenco | Mostra NON RAGGIUNGIBILE con un conto alla rovescia (code USB, `SPOOLSTEWARD_OFFLINE_REMOVE_HOURS`) e un pulsante **Elimina coda**. Le code di rete non vengono mai tolte da sole. |
| Una coda che ho eliminato non torna, anche se è in `CUPS_QUEUES` | È voluto: ogni coda di `CUPS_QUEUES` (e `virtual`) viene creata una sola volta. Per crearla di nuovo: `scripts/queue-restore.sh NOME --reboot` (senza `--reboot` la coda torna al prossimo avvio); `--list` mostra i nomi ricordati. La coda deve essere ancora in `CUPS_QUEUES`. |
| La coda non è stata creata | `CUPS_QUEUES` è sbagliato o la stampante ha risposto tardi. L'entrypoint riprova per 5 minuti; altrimenti `docker compose exec cups lpadmin -p NOME -E -v URI -m everywhere`. |
| Un "Create-Job" senza documento tiene la quota | La quota si libera solo quando CUPS lo annulla (di default dopo 3 h). |
| Limite pagine superato di un file | Il limite è *morbido* per i file di cui non si contano le pagine (formato sconosciuto, PDF cifrato, oltre 64 MB): vedi [limiti.md](limiti.md). |
| Stampa da browser: "file non supportato" | Si accettano solo PDF, JPEG, PNG, GIF e testo semplice, riconosciuti dal contenuto e non dal nome del file. |
| Stampa da browser: "più grande di N MB" | `SPOOLSTEWARD_WEB_UPLOAD_MB` (predefinito 25). |

## 4. Telefoni e computer

| Sintomo | Causa e rimedio |
|---|---|
| Avviso sul certificato, o il dispositivo rifiuta di collegarsi | Il dispositivo non si fida del certificato. Installa la CA, oppure usa un certificato firmato da una CA in `config/tls/`. Il nome o l'IP usato deve stare nel certificato. |
| Le stampanti non compaiono sul telefono | `mdns` deve girare con la rete host sulla stessa LAN (`docker compose logs mdns`; Docker Desktop non può). Imposta `SPOOLSTEWARD_MDNS_ADDRESSES` se la macchina ha più schede di rete e `SPOOLSTEWARD_MDNS_HOSTNAME` con un nome presente nel certificato. Una stampante nascosta o irraggiungibile, o la manutenzione, tolgono anche l'annuncio. Se il log dice **"mDNS is OFF"**, in `.env` manca `SPOOLSTEWARD_SECRET_KEY` ([sopra](#nessuna-chiave-segreta-impostata)). |
| Android: il servizio di sistema vede la stampante ma fallisce | Non sa autenticarsi. Installa **Mopria Print Service** o stampa dal portale. |
| Il desktop Linux mostra le stampanti ma non stampa | Le stampanti annunciate non hanno la chiave. GNOME non sa aggiungerla dalla sua finestra: esegui il comando `lpadmin` della pagina «Crea una chiave» o `scripts/add-printer-linux.sh`, che la salvano nell'indirizzo della coda. |
| Windows: «Operazione non riuscita» o «sto connettendo» troppo a lungo quando aggiungi la stampante (procedura completa in [windows.md](windows.md)) | Windows chiede se il lavoro andrebbe bene prima di avere una password, e la finestra «Aggiungi dispositivo» non sa chiederla. Aspetta 3-4 minuti; se non si collega: Pannello di controllo → Gestione credenziali → Credenziali Windows → Aggiungi credenziali Windows; indirizzo = nome o IP della stampante, utente = il tuo nome, password = la **chiave di stampa** (non la password dell'account). |
| macOS / iPhone | Non provati dal progetto. Prova `ipps://<server>:8631/printers/<coda>`, nome utente + chiave di stampa, e segnala cosa succede. |

## 5. Scansione

| Sintomo | Causa e rimedio |
|---|---|
| Nessuno scanner nel portale | Gli scanner si trovano se rispondono a eSCL sulla porta IPP della coda o sulla 80. Altrimenti aggiungi `SPOOLSTEWARD_SCAN_URL=nome=http://host:porta`. Uno scanner scollegato sparisce in ~30 s. |
| "scanner occupato" | Un'altra scansione è in corso, o il client non ha cancellato il job eSCL precedente. Il gateway riprova 3 volte se il dispositivo si sta svegliando. |
| Pagina bianca dal caricatore | Il caricatore dormiva e si dichiarava vuoto, quindi *Automatico* ha usato il vetro. Scegli *Caricatore* esplicitamente o rimetti i fogli. |
| Ho premuto il pulsante della finestra di scansione ma la scansione è rifiutata | La campanella dice «la finestra era per X, la richiesta arriva da Y»: IPv4 contro IPv6, NAT o un proxy ([scansione.md](scansione.md)). Apri il portale dal dispositivo su cui gira l'app di scansione, con lo stesso tipo di indirizzo; dietro un reverse proxy aprilo direttamente sulla porta 8631. |
| Non c'è il pulsante della finestra di scansione | `SPOOLSTEWARD_ESCL_WINDOW=false`, oppure nessuno scanner è collegato. |
| NAPS2 riceve 401 senza finestra | Non sa inviare le credenziali. Imposta `SPOOLSTEWARD_ESCL_ANONYMOUS=true` (solo su reti fidate) e inserisci IP e porta a mano. |
| Il risultato della scansione è sparito | I risultati restano in memoria per `SPOOLSTEWARD_SCAN_KEEP_MINUTES`, solo per il proprietario, e le più vecchie se ne vanno per prime oltre `SPOOLSTEWARD_SCAN_KEEP_MB`. Con `SPOOLSTEWARD_SCAN_DELETE_AFTER_DOWNLOAD=true` si scarica una volta sola. Rifai la scansione e scarica prima. |

## 6. Backup e audit

| Sintomo | Causa e rimedio |
|---|---|
| Il backup è rosso | È fallito o l'ultimo ha più di 48 h. Apri la pagina per il motivo; di solito la cartella non è scrivibile o il disco è pieno. |
| "Folder name not valid…", "Folder is outside the backup root", "Folder … is not writable by the gateway" | La cartella di un backup deve essere una sottocartella di `/backups` (sull'host `data/backups`) e scrivibile dall'utente `PUID`. Un disco USB o una cartella di rete va montata sull'host *dentro* quella cartella **prima** di avviare il gateway (riavvia il gateway dopo il montaggio). |
| Passphrase persa | I backup cifrati non si aprono senza (non esiste un reset). Crea una nuova chiave di recupero (`backup-key`) per i backup futuri. |
| Ripristino | Un file danneggiato, una passphrase sbagliata o un backup senza database vengono rifiutati e il database attuale non viene toccato. Per ripristinare: ferma `gateway` e `mdns`, esegui `docker compose run --rm -it --no-deps gateway spoolsteward-admin backup-restore /backups/<cartella>/<file>`, poi `docker compose up -d`. Controlla prima il file con `backup-verify`. |

## 7. Lingua del portale

| Sintomo | Causa e rimedio |
|---|---|
| Le pagine sono in inglese ma ti aspettavi l'italiano | Ordine: scelta dell'utente → lingua del browser → `SPOOLSTEWARD_LANGUAGE` → inglese. Scegli la lingua in *Impostazioni*. |
| Qualche testo non è tradotto | I testi che arrivano dalla stampante (nomi degli inchiostri, firmware, avvisi del dispositivo) o da CUPS si mostrano come arrivano. I motivi nell'audit sono in inglese di proposito. |
| I caratteri cinesi sono quadratini | Sul computer *del browser* manca un font CJK. |
| Le pagine di prova o di manutenzione stampate sono in inglese | È voluto: russo e cinese richiederebbero un font PDF incorporato ([lingue.md](lingue.md)). |

## 8. Aggiornamenti e certificati

| Sintomo | Causa e rimedio |
|---|---|
| `scripts/update.sh` dice `SPOOLSTEWARD_REGISTRY is empty` | La riga `SPOOLSTEWARD_REGISTRY=` di `.env` ha perso il suo valore. Riscrivila come in `docker/env.example` (`SPOOLSTEWARD_REGISTRY=ghcr.io/deh-aleh`) ed esegui di nuovo lo script. |
| `scripts/update.sh` dice «This folder is a git clone» | Lo script aggiorna una cartella creata dal pacchetto della release. Un clone git è una copia da sviluppatore e non si aggiorna così ([CONTRIBUTING.md](../../CONTRIBUTING.md)). |
| `refused: the portal only installs a version newer than …` | Il portale ha chiesto una versione che non è più recente di quella installata. Il pulsante *Aggiorna* non torna mai indietro: per tornare indietro esegui `scripts/update.sh <versione>` da terminale. |
| `the package does not match its sha256 sum` | Il file scaricato non è quello pubblicato (download interrotto, o una rete che lo modifica). Non è stato cambiato nulla: riprova; se persiste, scaricalo dal browser dalla pagina delle release. |
| `NOT healthy after the update: going back` | La versione nuova non è diventata sana in due minuti e lo script ha rimesso i file di prima. Guarda `docker compose logs --tail 50 gateway`. |
| Dopo l'aggiornamento non parte né la versione nuova né la vecchia | La nuova ha già cambiato il database. `update.sh` ti propone di ripristinare il backup preso all'inizio (`YES`); a mano: `scripts/backup.sh restore data/backups/pre-update/<file>`. |
| Il pulsante *Aggiorna* ha detto «rolled back» | La nuova versione non è partita e c'è di nuovo la vecchia. Il ripristino del backup, se serve, lo fai tu (stesso comando di sopra). |
| Avviso «Backup non eseguito» nella campanella | Il disco di rete o USB dei backup non è montato (o il backup è fallito per un altro motivo, scritto nell'avviso). Rimonta, poi *Esegui ora* ([backup.md](backup.md)). |
| Avviso sul certificato nella campanella | Vedi [certificati.md](certificati.md): ogni avviso dice cosa è successo e cosa fare. |
| `scripts/cert-sync.sh` scrive un errore | La tabella dei messaggi è in [certificati.md](certificati.md#54-se-qualcosa-va-storto). |
| I nomi dei file nell'audit sono `[encrypted]` | `SPOOLSTEWARD_SECRET_KEY` è stata cambiata dopo averli salvati. Rimetti la chiave di prima in `.env` e `docker compose up -d` ([protezione-dati.md](protezione-dati.md)). |

## 9. Ancora bloccato

Prima di chiedere aiuto raccogli `docker compose logs gateway`, `docker compose ps`, l'esito di `/healthz`, la versione del gateway
(`SPOOLSTEWARD_VERSION` in `.env`) e ciò che mostrava la pagina. Non incollare mai `.env` o la chiave TLS.
