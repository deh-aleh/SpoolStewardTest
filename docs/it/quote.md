# Quote, richieste e concessioni

[Indice della documentazione](../README.md) · [English](../en/quotas.md) · Italiano

## 1. Limiti

Una **regola** dice: *questo utente o questo gruppo può stampare al massimo N **job** (o **pagine**) per **settimana** (o **mese**)*.

- Le regole appartengono a un gruppo, con **override per utente** facoltativi. Una regola utente sostituisce quella del gruppo per la
  stessa metrica.
- Metriche: **job** e **pagine**. Periodi: **settimanale** (lunedì–domenica) o **mensile** (mese di calendario), nel fuso
  `SPOOLSTEWARD_TIMEZONE`, anche attraverso il cambio dell'ora legale.
- Nessuna regola = nessun limite.
- Dove si impostano: *Gruppi* → modifica (limiti del gruppo), *Utenti* → modifica (limiti personali), oppure la CLI:
  `spoolsteward-admin group-limit staff 20 --period monthly`, `spoolsteward-admin user-limit mario 5 --metric pages`
  (`none` toglie un limite).

### Cosa conta

Contano i job nello stato `accepted`, `completed` o `held`. **I job rifiutati, falliti e annullati prima della stampa non consumano
quota.** L'utente vede consumo e prossimo azzeramento in *Profilo*.

### Pagine contate prima di stampare

Quando a un utente si applica un limite di pagine, il gateway legge il documento **prima** di stamparlo (fino a 64 MB) e ne conta le pagine:
PDF (pypdf), JPEG/PNG/GIF = 1, URF dall'intestazione, PostScript da `%%Pages`, testo semplice ≈ 60 righe per pagina; poi moltiplica per
le copie e per l'intervallo di pagine scelto. Se `usate + pagine > limite` il job è **rifiutato subito**:

> `would be exceeded (used 90, this document 200, limit 100)`

Altrimenti le pagine sono registrate al momento, così i job simultanei le vedono. Dopo la stampa il riconciliatore sostituisce la stima
con le **pagine reali** lette da CUPS. Un job con pagine non ancora note vale almeno 1 pagina.

### Il limite morbido

Il limite pagine è **rigido** solo per i documenti che il gateway sa contare. È **morbido** in questi casi:

- formato sconosciuto, **PDF cifrato** o file oltre 64 MB: il job che supera il limite *viene stampato* e le sue pagine reali
  bloccano poi i successivi;
- un job inviato con `Create-Job` + `Send-Document`: le pagine si contano al `Send-Document`, quando il job è già stato accettato;
- un `Create-Job` senza documento libera la quota solo quando CUPS lo annulla (di default dopo 3 ore).

### Riserva atomica

Il controllo del limite e la registrazione del job avvengono in un unico passaggio protetto da lock, quindi molti job simultanei non possono superare il limite. Le concessioni attive si sommano al limite **dentro** lo stesso controllo.

## 2. Richieste degli utenti

In *Profilo* un utente può chiedere:

1. **Più quota** — sceglie la metrica, la quantità, **temporanea** o **permanente**, e scrive un motivo. Può chiedere solo le metriche
   che hanno davvero un limite. Il **periodo non lo sceglie lui**: è quello della regola impostata dall'amministratore; il modulo lo
   mostra in sola lettura ("il tuo limite pagine è mensile: la deroga durerà fino al 1 novembre").
2. **Cambio di gruppo** — solo verso gruppi marcati *richiedibili*.
3. **Un permesso** — colore, stampa solo fronte, una stampante in più o stampare fuori orario (vedi [Chiedere un permesso](#chiedere-un-permesso)).

Limiti sulle richieste: al massimo `SPOOLSTEWARD_MAX_OPEN_REQUESTS` (3) richieste aperte per utente, il motivo è facoltativo, a meno che `SPOOLSTEWARD_REQUEST_REASON_REQUIRED=true` (sempre trattato come testo),
le richieste scadono dopo `SPOOLSTEWARD_REQUEST_TTL_DAYS` (14). L'utente può annullare una richiesta in attesa.

## 3. Approvazione e concessioni

Gli amministratori vedono le richieste in attesa in **Richieste** (con un contatore nel menu) e possono **approvarle, modificarle o
rifiutarle**. Approvando possono cambiare quantità e tipo (anche trasformare una permanente in temporanea) e aggiungere una nota.

L'approvazione produce una **concessione** che si **somma** al limite:

`limite effettivo = regola + concessioni attive`

- Una concessione **temporanea** vale fino alla fine della finestra del periodo **in cui avviene l'approvazione** (non quella della
  richiesta: una richiesta fatta domenica sera e approvata lunedì non deve nascere già scaduta).
- Una concessione **permanente** si applica a ogni periodo successivo ("+20 pagine ogni mese") finché non viene revocata.
- Il cambio di gruppo sposta subito l'utente; da quel momento valgono i limiti del nuovo gruppo.
- Un'approvazione ripetuta (due amministratori, un doppio clic) concede una volta sola.
- Le concessioni non toccano mai lo storico (`job_log` non viene modificato).

Gli amministratori possono anche concedere o revocare quota direttamente in **Richieste**. Tutto finisce nell'audit degli
amministratori ([audit.md](audit.md)). L'elenco utenti mostra le concessioni attive come `+N` colorato.

## 4. Motivi mostrati agli utenti

Quando un job è rifiutato, il client IPP riceve il motivo in inglese più l'indirizzo del portale per chiedere una deroga. Il portale
mostra la stessa frase tradotta ([lingue.md](lingue.md)). L'audit conserva il motivo in inglese.

## 5. Permessi avanzati

Le quote dicono *quanto*. I permessi dicono *cosa*, *dove* e *quando*. Si impostano con il pulsante **Permessi avanzati** in fondo
alla pagina del gruppo (*Gruppi* → modifica) e a quella dell'utente (*Utenti* → modifica). Il blocco è chiuso quando tutto è al
valore predefinito, ed è aperto quando qualcosa è diverso.

![La pagina di modifica di un gruppo: nome, limiti di job e pagine, periodo, richiedibile, e il blocco Permessi avanzati aperto con colore consentito e solo fronte non consentito](../images/it/admin-group-edit.png)

| Permesso | Se non è concesso |
|---|---|
| **Colore** | Le stampe escono in bianco e nero. |
| **Solo fronte** | Le stampe escono in fronte-retro (lato lungo), sulle stampanti che hanno l'unità fronte-retro. |
| **Stampanti** | Si possono usare solo le stampanti dell'elenco. "Tutte le stampanti" è il predefinito. |
| **Orari** | Si stampa solo nei giorni scelti, tra le due ore. "Sempre" è il predefinito. |

Di predefinito è tutto consentito: un gruppo senza nulla di impostato, e un utente senza nulla di impostato, possono fare tutto.

Ogni permesso è una riga di pulsanti, di cui se ne sceglie uno: *consentito* / *non consentito* per colore e solo fronte, *tutte* /
*solo alcune* per le stampanti, *sempre* / *solo in certi orari* per gli orari. Le stampanti da spuntare compaiono solo dopo aver scelto
*solo alcune*, giorni e ore solo dopo *solo in certi orari*. Nella pagina di un utente ogni riga comincia con *come il gruppo (…)*, che
mostra il valore del gruppo. Sul telefono i pulsanti stanno uno sotto l'altro.

**Gruppo e utente.** Il gruppo dà i valori. Nella pagina di un utente ogni campo è "come il gruppo" (il predefinito) oppure ha un
valore proprio, campo per campo: un utente può avere un permesso colore suo e continuare a seguire il gruppo per stampanti e orari. La
pagina mostra, accanto a ogni campo, cosa dice il gruppo. Se cambi i permessi del gruppo cambiano per tutti gli utenti che lo seguono, e
a loro non arriva nessun avviso; se cambi i permessi propri di un utente, a lui arriva un avviso (vedi "Cosa vede l'utente"). Un
amministratore è limitato come tutti gli altri. L'unica eccezione è l'**amministratore di emergenza** (`SPOOLSTEWARD_BOOTSTRAP_ADMIN_USERNAME`,
un account locale): ha tutti i permessi, nella sua pagina il blocco è spento ("tutti i permessi, non limitabile") e il server ignora
quello che viene inviato per lui. Non ha nemmeno gruppo né quote: non sta nel gruppo "administrator", non ha limiti di job o pagine e non può chiedere di cambiare gruppo. Quote, limiti e permessi del gruppo "administrator" valgono per tutti gli altri amministratori. **La scansione non è limitata** da niente di tutto questo:
un utente può scansionare con una stampante su cui non può stampare.

### Stampanti

L'elenco contiene nomi di coda, scritti in minuscolo (CUPS non distingue le maiuscole). Tre regole da ricordare:

- Una stampante aggiunta **dopo** che l'elenco è stato fatto **non** è consentita a chi ha un elenco, finché non la spunti.
- Se una coda viene **rinominata** ([stampanti.md](stampanti.md)), il suo nome cambia da solo in tutti gli elenchi. Se l'elenco aveva già
  il nuovo nome, lo ha una volta sola.
- Se una coda viene **tolta**, il suo nome resta negli elenchi (la pagina lo mostra come "non presente"). Una stampante sostitutiva a cui
  si dà quel nome ritrova i permessi.

### Orari

Una sola fascia: i giorni della settimana e due ore (`HH:MM`), nel fuso del gateway (`SPOOLSTEWARD_TIMEZONE`, ora legale compresa). L'ora di
fine non è inclusa: da `08:00` a `17:00` passa un lavoro inviato alle 16:59, non alle 17:00. Se la fine è prima dell'inizio (da `22:00` a
`06:00`) la fascia passa la mezzanotte e appartiene al giorno in cui comincia: da venerdì 22:00 a sabato 06:00 è la fascia di venerdì.
Inizio e fine non possono essere uguali. Gli orari si controllano **quando il lavoro viene inviato**: un lavoro già accettato non viene
fermato quando la fascia si chiude.

### Cosa succede a un lavoro

Il gateway controlla in quest'ordine: stampante bloccata o manutenzione (come sempre), poi i permessi, poi la quota.

| Cosa ha chiesto il client | Non consentito | Risultato |
|---|---|---|
| Una stampante fuori dall'elenco | | **Rifiutato**, sempre |
| Un'ora fuori dalla fascia | | **Rifiutato**, sempre |
| Colore non indicato (niente, o `auto`) | niente colore | Stampato in bianco e nero. Nessun avviso |
| Colore indicato (`color`) | niente colore | `SPOOLSTEWARD_PERMISSION_MODE=adjust` (predefinito): stampato in bianco e nero, all'utente arriva un avviso. `refuse`: rifiutato |
| Lati non indicati | niente solo fronte | Stampato in fronte-retro (lato lungo). Nessun avviso |
| `one-sided` indicato | niente solo fronte | `adjust`: stampato in fronte-retro, avviso. `refuse`: rifiutato |

Le regole guardano `print-color-mode`, `sides` e anche le opzioni che alcuni driver mandano con i nomi del PPD (`ColorModel`,
`ColorMode`, `Duplex`), nella parte del lavoro e in quella dell'operazione della richiesta.

Una stampante senza colore non viene mai toccata dalla regola sul colore. Una stampante senza unità fronte-retro (elenca solo
`one-sided`) non viene mai toccata dalla regola sul solo fronte: il lavoro esce solo fronte e non viene rifiutato. Il gateway chiede alla
coda cosa sa fare (una sola domanda, ricordata per un minuto). Se CUPS non risponde non si forza nulla alla cieca: si corregge solo ciò
che il client ha indicato in modo esplicito.

Un lavoro rifiutato finisce nell'audit come `denied` con il motivo, **non consuma quota né pagine**, e all'utente arriva un avviso
(ogni rifiuto rinnova quello aperto e ne alza il contatore, così un programma che riprova non riempie la campanella). Il client IPP riceve il motivo e l'indirizzo della
pagina del profilo, dove l'utente vede i suoi permessi e può chiedere quello che manca; non gli suggerisce di chiedere più quota.

### Da leggere prima di scegliere la modalità

1. **Windows e Android spesso mandano `sides=one-sided` o il colore in ogni lavoro**, anche se la persona non ha mai toccato
   l'opzione. Con `refuse` molti lavori di questi utenti vengono rifiutati finché non cambiano l'opzione nella finestra di stampa. Per
   questo il predefinito è `adjust`: il lavoro esce, corretto, e l'utente viene avvisato una volta.
2. **Il bianco e nero forzato può non funzionare** con alcune stampanti e alcuni driver, soprattutto con le immagini che il telefono
   ha già trasformato in pixel a colori (iPhone / AirPrint). Il gateway chiede il bianco e nero; la stampante decide. Se serve una
   garanzia, usa `refuse`. Il fronte-retro usa l'attributo standard `sides`, che CUPS traduce da solo nell'opzione del driver: è più
   affidabile, purché la coda dichiari l'unità fronte-retro.
3. **L'annuncio in rete (mDNS) è uguale per tutti.** Le stampanti che una persona non può usare si **vedono** comunque sul suo telefono
   o computer, e un lavoro mandato lì viene rifiutato. L'elenco delle stampanti che il gateway dà dopo l'accesso le lascia fuori.
4. **Gli orari si controllano quando il lavoro viene inviato**, non mentre stampa.
5. **La scansione non è limitata.**
6. **L'amministratore di emergenza è esente** dai permessi e dalle quote: non appartiene a nessun gruppo.

### Cosa vede l'utente

- *Profilo*: il riquadro "I tuoi permessi" con colore, solo fronte, stampanti e orari, e il link per chiedere quello che manca. L'elenco delle stampanti disponibili e gli
  indirizzi pronti dopo aver creato una chiave mostrano solo le stampanti che la persona può usare.
- *Stampa*: solo le stampanti consentite; niente scelta del colore senza il permesso colore; una scelta fronte-retro (solo fronte / lato
  lungo / lato corto) sulle stampanti che hanno l'unità fronte-retro, solo per chi può stampare solo fronte; un avviso in cima con gli
  orari consentiti quando è fuori orario, e il lavoro viene rifiutato.
- La campanella: gli avvisi "rifiutato", "quota raggiunta" e "modificato" quando succede (quello "modificato" è *secondario*: si legge da solo dopo il tempo scelto dall'utente in `/me/settings`, 20 minuti di base), e uno quando un amministratore cambia i permessi propri della persona.

Il registro degli amministratori (*Azioni*) annota ogni modifica ai permessi di un gruppo o di un utente con i valori vecchi e nuovi.

### Chiedere un permesso

Chi non ha un permesso lo chiede nel riquadro "I tuoi permessi" del suo profilo: il link **Chiedi** accanto a colore, solo fronte e
orari, e **Chiedi un'altra stampante** per le stampanti. Il modulo offre solo ciò che la persona non ha, e solo le stampanti che esistono
adesso. Il motivo è obbligatorio solo con `SPOOLSTEWARD_REQUEST_REASON_REQUIRED=true` (al massimo 500 caratteri). La richiesta viene rifiutata se la persona ha già quel permesso, oppure
se ha già chiesto la stessa cosa e nessuno ha ancora risposto. Valgono gli stessi freni delle richieste di quota: al massimo
`SPOOLSTEWARD_MAX_OPEN_REQUESTS` richieste aperte (di qualsiasi tipo, insieme), e una richiesta senza risposta scade dopo
`SPOOLSTEWARD_REQUEST_TTL_DAYS`. La persona può annullarla dal profilo.

L'amministratore la vede in **Richieste**, con una scelta: **permanente**, oppure **fino alla fine del** giorno scelto. La data vale per
tutto quel giorno nel fuso orario del portale (`SPOOLSTEWARD_TIMEZONE`) e deve essere oggi o dopo. Approvare crea una *concessione di permesso*
che **allarga** ciò che danno il gruppo e la regola propria della persona; non toglie mai nulla:

| Richiesto | La concessione dà |
|---|---|
| Colore | il colore è permesso |
| Solo fronte | la stampa solo fronte è permessa |
| Una stampante | quella stampante entra nell'elenco della persona (se la persona non ha un elenco, tutte le stampanti sono già permesse e non c'è nulla da chiedere) |
| Fuori orario | la fascia oraria non si applica |

Un'approvazione ripetuta concede una volta sola, come per le quote, e un'approvazione rifiutata (per esempio una data nel passato) lascia la richiesta
in attesa. Le concessioni di permesso attive stanno nello stesso elenco *Concessioni attive* di quelle di quota, con *Revoca*; quelle
finite o revocate vanno nello storico, e *Elimina le concessioni scadute* toglie entrambi i tipi. Il registro degli amministratori
annota ogni approvazione, concessione diretta, revoca ed eliminazione (l'eliminazione con quante ne sono state tolte per tipo). La
pagina di modifica dell'utente elenca le sue concessioni di permesso attive.

Un amministratore può anche concedere un permesso senza richiesta: in *Concedi senza richiesta* sceglie **un permesso**, quale, e
permanente o fino a una data. Scelto l'utente, il modulo propone solo ciò che quell'utente non ha già (una stampante che può già usare
non è nell'elenco); se ha già tutto lo dice e il pulsante è spento. Il server rifiuta comunque un permesso che l'utente ha già. Se
JavaScript è spento il modulo mostra tutti i campi e il server legge solo quelli del tipo scelto.

La campanella dice alla persona quando la richiesta viene approvata (con la data di fine) o rifiutata, quando un amministratore
concede o revoca un permesso direttamente e, entro l'ora dopo la data di fine, che un permesso temporaneo è finito. Un amministratore
non riceve avvisi per ciò che fa al proprio account. L'amministratore di emergenza ha tutti i permessi e non ha nulla da chiedere.
