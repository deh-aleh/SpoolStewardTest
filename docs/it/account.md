# Account, gruppi e chiavi di stampa

[Indice della documentazione](../README.md) · [English](../en/accounts.md) · Italiano

## 1. Chi è chi

Ogni persona è **una riga** nella tabella `user` con un *provider*:

- **local** — la password (argon2id) sta nel database del gateway e si gestisce dal portale;
- **RADIUS** (facoltativo) — la password la verifica il server RADIUS d'ufficio; il gateway non la conserva.

**I nomi utente sono unici fra tutte le origini.** A CUPS e all'audit arriva solo `gw:<nome>`: due persone con lo stesso nome
condividerebbero identità e quota. Il metodo di accesso lo decide la *riga*, mai chi risponde "sì": un account locale non può essere
aperto dal RADIUS nemmeno se il RADIUS conosce lo stesso nome. Un utente RADIUS sconosciuto viene creato al primo accesso al portale
(nel gruppo predefinito) se `SPOOLSTEWARD_AUTO_CREATE_USERS=true`; se il nome è già locale, no.

RADIUS è facoltativo. Con `SPOOLSTEWARD_RADIUS_HOST` e `SPOOLSTEWARD_RADIUS_SECRET` impostati a `none` esistono solo account locali. Se il RADIUS non risponde,
l'accesso è negato (fail closed). I nomi RADIUS si confrontano senza distinguere maiuscole: `Mario` e `mario` sono la stessa persona.

## 2. Il primo amministratore

`administrator` esiste senza password. Al primo avvio il gateway scrive nei log un **codice monouso**
(`docker compose logs gateway | grep "FIRST RUN"`); `/setup` lo chiede insieme alla nuova password e poi si disattiva.
Il codice è protetto da throttling. In alternativa: `spoolsteward-admin set-admin-password`. Di proposito non esiste una pagina aperta
a tutti per diventare amministratore.

L'account di emergenza non si può disattivare, declassare, eliminare né cambiargli origine, e il suo nome non si può dare a nessun altro account. È l'unico account senza gruppo: niente lo limita (nessuna quota, nessuna regola di permessi) e non può chiedere di cambiare gruppo. Gli altri amministratori stanno nel gruppo "administrator", di cui puoi modificare limiti e permessi (il gruppo però non si può eliminare). Nessuno può disattivare, declassare o
eliminare se stesso, né l'ultimo amministratore.

## 3. Password (account locali)

Minimo 12 caratteri, diversa dal nome utente. Una password creata o reimpostata da un amministratore è **temporanea**: si mostra una
volta e l'utente deve cambiarla al primo accesso. Cambiare password chiude le altre sessioni dell'utente. Il login spende sempre il costo
di un hash, anche per nomi inesistenti, e non rivela se il nome esiste.

## 4. Gruppi

I limiti di quota si impostano per **gruppo**, con override facoltativi per utente ([quote.md](quote.md)). Esistono sempre due gruppi di
sistema, che non si eliminano:

- `default` — dove partono tutti e dove vanno gli utenti quando il loro gruppo viene eliminato o smettono di essere amministratori;
- `administrator` — solo per gli amministratori. La promozione sposta l'utente lì; il declassamento lo riporta in `default`. Un
  non amministratore non può esservi spostato a mano.

Un gruppo si può marcare **richiedibile**: gli utenti possono allora chiedere di entrarvi ([quote.md](quote.md)). I gruppi amministrativi non lo sono mai.

## 5. Chiavi di stampa

I sistemi operativi tengono le password di stampa in chiaro (per esempio `printers.conf` su Linux). Perciò i client **non usano mai** la
password di dominio: usano una **chiave di stampa personale**.

- La crea l'utente in *Profilo → Crea una chiave* (con un'etichetta per dispositivo). Si mostra **una volta**; si conserva solo
  come hash sha256 di 192 bit casuali.
- Vale **solo per stampare e scansionare** (gli endpoint IPP ed eSCL); non apre mai il portale.
- **La durata la sceglie l'utente** con una tendina nel modulo di creazione: 1 mese, 3 mesi, 6 mesi, **1 anno (proposto)**, 1 anno e 6 mesi,
  2 anni, oppure **una sola stampa**. Il massimo dipende dal tipo di account: `SPOOLSTEWARD_KEY_TTL_DAYS_RADIUS` (90 giorni) per gli utenti RADIUS e
  `SPOOLSTEWARD_KEY_TTL_DAYS_LOCAL` (730 giorni) per i locali; la tendina mostra solo le durate che ci stanno. Un utente RADIUS ha quindi 30 o 90
  giorni e deve rifare il login nel portale per averne un'altra — così chi lascia l'azienda smette di stampare alla scadenza.
- **Chiave monouso**: vale per una sola stampa. Se nessuno la usa smette di valere dopo `SPOOLSTEWARD_KEY_ONCE_HOURS` ore (24). Appena la stampa è
  accettata la chiave sparisce dall'elenco; per `SPOOLSTEWARD_KEY_ONCE_GRACE_SECONDS` secondi (120) può ancora servire a chiedere lo stato del lavoro,
  non a stamparne un altro. Una stampa respinta dalla quota non la consuma; una stampa accettata ma poi non riuscita sì (se ne crea un'altra).
- Una chiave è personale: chi la possiede stampa e scansiona come quell'utente, sulla sua quota e nel suo audit. Cederla a un altro è responsabilità del titolare; se è successo, o il dispositivo è perso, va revocata.
- L'ultimo uso (ora e IP) è annotato al massimo una volta al minuto.
- Si revoca una chiave, o tutte, in *Le mie chiavi* (`/me/keys`) oppure, per un amministratore, in `/admin/users/<id>/keys`.
- L'amministratore, per ogni chiave, può **disattivarla** (non funziona, ma la scadenza continua a scorrere; si può riattivare), **prolungarla**
  (nuova scadenza = adesso + la durata scelta, entro il massimo dell'account; il tempo rimasto **non** si somma: una chiave che scade tra 2 mesi, prolungata di «1 anno», scade tra 1 anno e non tra 1 anno e 2 mesi) o **revocarla**. Ogni azione finisce nell'audit (`key.disable`,
  `key.enable`, `key.extend`, `key.revoke`) e **l'utente la trova nelle sue notifiche** (la campanella, `/me/notices`), con un testo fisso e senza
  il nome dell'amministratore. Se l'amministratore agisce su una chiave propria non c'è nessuna notifica.
- Oltre alle chiavi, l'utente trova negli avvisi (stessa campanella) quando un amministratore **cambia il suo gruppo** (anche quando un gruppo viene eliminato e i suoi membri passano a
  `default`), **lo rende amministratore o gli toglie il diritto**, **gli dà o gli ritira quota extra**, oppure quando una **richiesta è approvata o rifiutata** (con la nota, se c'è).
  Non c'è nessun avviso per le modifiche che fai a te stesso, e cambiare i limiti di un gruppo non manda avvisi. Neppure la riga di comando (`set-group`) ne manda.
- Le chiavi **revocate** e quelle **scadute** spariscono dall'elenco dopo `SPOOLSTEWARD_KEY_HISTORY_DAYS` giorni (14; 0 = restano per sempre). Finché ci
  sono, l'elenco dice quando se ne andranno.
- Facoltativo, spento di default: `SPOOLSTEWARD_KEY_EXPIRY_WARN_ENABLED=true` avvisa l'utente nella campanella `SPOOLSTEWARD_KEY_EXPIRY_WARN_DAYS` giorni
  (7) prima della scadenza, una volta sola per chiave.

`SPOOLSTEWARD_IPP_AUTH=keys` (predefinito) accetta per stampare solo le chiavi. `keys+radius` accetta anche la password RADIUS per gli utenti
RADIUS durante una transizione (mai per gli account locali): incoraggia a salvare la password di dominio sui client, quindi usala solo
temporaneamente. Tutto ciò che inizia con `cpk-` è preso per una chiave di stampa: se non è valida (scaduta, revocata, disattivata) viene rifiutato
subito e **non viene mai inviato a RADIUS**, così una vecchia chiave rimasta su un telefono non aggiunge accessi falliti all'account di dominio
(che potrebbe bloccarsi). Unica conseguenza: una password di dominio che inizia con `cpk-` non si può usare per stampare.

## 6. Protezione dei login

- 5 password *diverse* sbagliate in 5 minuti per la stessa coppia (utente, IP) bloccano quella coppia; c'è anche un tetto per IP. La
  stessa password sbagliata ripetuta da un client conta una volta (libcups riprova 3–5 volte) e un estraneo non può bloccare un altro utente.
- Il blocco è salvato in un piccolo file (`/data/login-lockout.json`, solo nomi utente, indirizzi e orari), quindi un riavvio non lo azzera. Blocca la coppia (utente, IP) per 5 minuti e **non** disattiva l'account: lo stesso utente da un altro
  indirizzo non è toccato. Durante il blocco la pagina di accesso risponde "Troppi tentativi falliti, riprova più tardi" anche se poi la password è giusta.
  Ripetere la stessa password sbagliata non blocca mai: ne servono cinque diverse.
- Disabilitare un utente nel portale blocca subito login e chiavi.
- Sessioni del portale: firmate, 8 ore, token CSRF su ogni POST, cookie `Secure` + `SameSite=strict`. Le pagine che mostrano un segreto
  sono inviate con `Cache-Control: no-store`.

## 7. Eliminare un utente

Rimuove account, chiavi, richieste, concessioni (di quota e di permesso), limiti personali e permessi personali, ma **non lo storico**: le righe di audit conservano il nome utente e
perdono solo il legame con l'id; le righe dell'audit amministrativo conservano già il nome di chi ha agito. L'amministratore scrive il
nome dell'utente per confermare.

## 8. L'origine di un account

La pagina di modifica dell'utente mostra, come semplice testo, da dove l'account fa l'accesso (locale o RADIUS): dal portale non si
può cambiare. Per spostare una persona da un'origine all'altra si elimina l'account e lo si ricrea dall'altra parte (lo storico delle
stampe conserva il nome utente).
