# Backup e ripristino

[Indice della documentazione](../README.md) · [English](../en/backup.md) · Italiano

Esistono due meccanismi indipendenti:

| | Backup integrati (portale) | `scripts/backup.sh` (lato host) |
|---|---|---|
| Chi | ogni amministratore, più uno *generale* | chi gestisce il server |
| Cosa | database (copia coerente mentre si stampa) e archivio audit; con la cifratura anche `.env` e il certificato TLS | database, `config/` (con i segreti), `.env`, archivio audit e code CUPS |
| Quando | programmato, una volta al giorno | a mano |
| Serve per | la protezione di ogni giorno | **spostare la macchina** |

## 1. Backup integrati (menu *Backup*)

Ogni amministratore crea i **propri** backup programmati, con cartella, ora, copie da tenere (ultime N giornaliere, settimanali, mensili) e cifratura. Modifica ed elimina solo i propri; *Esegui ora* è disponibile a tutti. Il backup *Generale* si mostra in sola lettura: si imposta in `.env` (`SPOOLSTEWARD_BACKUP_*`).

![Un backup programmato: proprietario, cartella, ora, copie tenute, cifratura con passphrase, ultimo esito e i pulsanti Esegui ora, Modifica, Sospendi ed Elimina](../images/it/admin-backup.png)

- Un backup è scritto in `.partial`, **riletto e verificato**, rinominato; solo dopo si tolgono le copie vecchie (solo file con lo stesso nome di gateway e destinazione). *Esegui ora* aggiunge una copia senza cancellarne.
- Se un backup è fallito, o l'ultimo ha più di 48 h, la pagina lo mostra in rosso **e nella campanella compare un avviso** con il nome del backup (più sotto); si chiude al primo backup riuscito.
- Ogni file è `spoolsteward-<istanza>-t<destinazione>-<ora UTC>.tar.gz` (più `.enc` se cifrato) con un `MANIFEST.json` (sha256 e dimensione di ogni parte, versione dell'app, revisione dello schema). Senza cifratura il file **non contiene segreti** (chiave TLS, `SECRET_KEY`, segreto RADIUS).

### Dove

Solo dentro `data/backups` (`BACKUP_DIR`, montata su `/backups`); un amministratore sceglie una sottocartella (lettere, cifre, spazio, `. _ -` e `/`, al massimo 4 livelli) e non può uscirne (`..`, percorsi assoluti e link simbolici sono rifiutati). Per un NAS o un disco USB montalo **dentro quella cartella prima di avviare il gateway**.

### Salvare su un disco di rete (NAS, SMB/CIFS, NFS) o su un disco USB

1. **Montalo sul server, dentro `data/backups`**, con `/etc/fstab` perché torni al riavvio, per esempio:

   ```
   # SMB/CIFS: uid/gid = PUID/PGID di .env, così il gateway può scrivere. La password in un file leggibile solo da root.
   //nas.lan/backups  /srv/spoolsteward/data/backups/nas  cifs  credentials=/root/.nas-cred,uid=1000,gid=1000,vers=3.0,_netdev,nofail,x-systemd.automount  0 0
   # NFS: la cartella esportata deve essere scrivibile da quell'id utente (controlla "root squash" sul NAS)
   nas.lan:/export/backups  /srv/spoolsteward/data/backups/nas  nfs  _netdev,nofail,x-systemd.automount,soft,timeo=100  0 0
   ```
2. Montalo **prima** di avviare il gateway: un contenitore già in esecuzione non vede un montaggio fatto dopo (`docker compose restart gateway`).
3. Nel modulo scrivi la sottocartella (`nas/...`), lascia spuntato **«La cartella è un disco di rete o USB: scrivi solo se è montato»** (di default per i backup nuovi) e salva *mentre il disco è montato*. Il gateway mette un file `.spoolsteward-destination` e da quel momento **rifiuta il backup se non c'è**: altrimenti un disco che
   non si monta lascia una cartella vuota sul disco del server e i backup la riempiono in silenzio. Il backup Generale ha l'opzione spenta (`SPOOLSTEWARD_BACKUP_REQUIRE_MARKER=false`) perché la sua cartella esiste sempre nel contenitore; per un disco di rete metti `true` e crea il file una volta a disco montato: `touch data/backups/nas/<cartella>/.spoolsteward-destination`.

Con un disco di rete:

- Disco che non risponde: la pagina *Backup* si carica in circa 6 secondi e mostra il problema; un backup fallisce dopo 20 secondi («Il disco dei backup non ha risposto…»); al massimo 4 richieste aspettano su un disco morto. Con il marker attivo, un disco non montato non riceve mai un file («Il file segnaposto manca…»).
- Su SMB/CIFS e in alcune configurazioni NFS i permessi li decidono le opzioni di montaggio, **non** il gateway: `chmod 600` non ha effetto, quindi **usa la cifratura** per i backup su una condivisione. Usa montaggi `soft` (NFS: `soft,timeo=100`; CIFS: `soft`), mai `hard`, così un NAS morto non blocca il server.
- Un disco che smette di rispondere **mentre un backup viene scritto** può lasciarlo bloccato: i successivi dicono «Un altro backup è in corso» finché non esegui `docker compose restart gateway`. Quello già salvato non si perde.
- Un backup è «fuori dalla macchina» solo se la condivisione sta su un'altra macchina: un disco USB nel server protegge da un disco rotto, non da un incendio.

### Avvisi quando il backup non riesce

Nella campanella compare un avviso con il **nome vero** del backup. A disco non montato: **Backup non eseguito: la cartella di destinazione non è raggiungibile.** L'ultimo backup di «NAS ufficio» non è stato salvato, perché il disco di rete o USB non risulta collegato. Nessun file è stato scritto. Controlla che sia acceso e montato, poi premi «Esegui ora». Ultimo backup riuscito: 2026-10-12 03:00:04.
Per ogni altro errore (disco pieno, permessi, passphrase…) dice «Backup fallito» e il motivo. Il gateway ritenta ogni ora; l'avviso si chiude da solo al prossimo successo.

### Cifratura

- **Passphrase** (consigliata; **non** la password dell'account: il gateway non la conosce, cambia, e per RADIUS non è dell'utente),
- **chiave di recupero** creata sul server (`spoolsteward-admin backup-key`: la passphrase non passa dal portale),
- oppure **nessuna**.

Si crea una sola volta una coppia di chiavi. La metà pubblica cifra ogni file (X25519 effimera + HKDF + AES-256-GCM a blocchi di 1 MiB); la metà privata è conservata **avvolta** da scrypt della passphrase e copiata nell'intestazione del file, quindi *file + passphrase* bastano. Il gateway non conserva mai la passphrase: **senza di lei le copie non si aprono**,
nemmeno da un altro amministratore. Un nuovo file cifrato viene riletto con la chiave appena usata.

### Salva la chiave segreta

`SPOOLSTEWARD_SECRET_KEY` in `.env` firma le sessioni del portale e protegge il collegamento con `mdns`; se cifri i nomi dei file ([protezione-dati.md](protezione-dati.md)) apre anche quelli. Un backup **senza cifratura non la contiene**, apposta (non contiene segreti). Quindi se il server si rompe, ripristini un backup non cifrato su una macchina nuova e `scripts/docker-init.sh` crea una
chiave **nuova**: il database torna intero (utenti, quote, pagine, date, stampanti), tutti rifanno il login e i nomi dei file cifrati restano `[encrypted]` per sempre.

- Copia la riga `SPOOLSTEWARD_SECRET_KEY=...` di `.env` in un password manager o in un altro posto sicuro, **una volta**, dopo l'installazione. Oppure fai backup cifrati: includono `.env`, quindi la chiave viaggia insieme al database.
- Quando passi a un'altra macchina, copia anche `.env` (`scripts/backup.sh` lo fa).

Il gateway lo ricorda una volta: lo trova nella campanella l'amministratore del primo avvio (o tutti gli amministratori, se quell'account non esiste). Se poi parte con una chiave diversa, la campanella dice quanti nomi non si leggono più; rimettendo la vecchia chiave in `.env` e riavviando tornano leggibili.

## 2. Ripristino

```sh
docker compose stop gateway mdns
docker compose run --rm -it --no-deps gateway spoolsteward-admin backup-restore /backups/<cartella>/<file>
docker compose up -d
```

Il ripristino legge e controlla prima tutto il file; un file danneggiato o troncato, una passphrase sbagliata, un backup senza database o fatto da una versione più nuova vengono rifiutati e il database attuale non viene toccato. Altrimenti il vecchio database resta accanto come `spoolsteward.db.before-restore-…`; i file di configurazione finiscono in
`/data/restored-config` (da copiare in `./config` su una macchina nuova). `backup-verify <file>` controlla un file senza ripristinarlo; `backup-run [--id N]` lancia i backup a mano. Il ripristino è anche la via di ritorno dopo un aggiornamento che vuoi annullare.

## 3. Backup completo dal lato host

```sh
scripts/backup.sh backup [CARTELLA]        # scrive CARTELLA/spoolsteward-v<versione>-AAAAMMGG-HHMMSS.tar.gz  (predefinito ./backups)
scripts/backup.sh restore FILE.tar.gz      # ferma prima il gateway e chiede conferma
```

Include il database, `config/` (segreti compresi), `.env`, l'archivio audit e le code CUPS; non le immagini né lo spool dei job in corso. L'archivio contiene segreti: tienilo riservato quanto il server. `restore` riporta il `.env` del backup, quindi anche la sua versione: se i file della cartella sono di un'altra versione, ti dice di eseguire
`scripts/update.sh <versione>` ([installazione.md](installazione.md#5-aggiornamenti)). `scripts/backup.sh` non ha bisogno di Internet (usa l'immagine che lo stack sta già eseguendo), controlla tutto prima e ferma l'applicazione solo quando è sicuro di poter proseguire. `restore` è provato solo a mano.
