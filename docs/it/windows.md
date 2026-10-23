# Stampare da Windows 11

[Indice della documentazione](../README.md) · [English](../en/windows.md) · Italiano

Guida passo passo per aggiungere una stampante del gateway a un PC Windows. Provata su Windows 11 Home 25H2: la stampante è stata aggiunta al primo tentativo, Windows chiede le credenziali quando si stampa e ha una casella per ricordarle.

Servono il **nome del server** (l'indirizzo del portale, per esempio `stampe.example.org`), il tuo **nome utente** e una **chiave di stampa**: non la password dell'account, ma un testo lungo fatto solo per stampare.

## 1. Crea una chiave di stampa

1. Entra nel portale (`https://<server>:8631/`) e apri **Le mie chiavi di stampa** (`/me/keys`).
2. Crea una chiave: scegli quanto dura e un'etichetta per questo PC (per esempio «PC ufficio»).
3. La pagina successiva mostra la chiave **una volta sola**: copiala in un gestore di password prima di lasciarla. Se la perdi, creane un'altra e revoca la vecchia.

## 2. Controlla che il PC si fidi del server

Un certificato di un'autorità pubblica (per esempio Let's Encrypt) non richiede nulla. Con un'autorità privata o un certificato autofirmato, installa il file del certificato che ti dà l'amministratore: doppio clic → *Installa certificato* → *Computer locale* → *Autorità di certificazione radice attendibili*.
Senza questo Windows rifiuta la connessione prima di inviare qualsiasi cosa, e nel log del gateway non compare nulla.

## 3. Aggiungi la stampante

1. *Impostazioni → Bluetooth e dispositivi → Stampanti e scanner → Aggiungi dispositivo*.
2. Dopo qualche secondo compaiono le stampanti del gateway come `<stampante> @ <nome del server>`. Clicca una e premi **Aggiungi**. Se Windows scrive «Sto connettendo…» per molto tempo, **aspetta fino a 3-4 minuti**.

Se non compare nessuna stampante: riavvia il PC; se non basta, in *Stampanti e scanner* attiva e disattiva *Modalità di stampa protetta di Windows* e riprova; se non compaiono mai, di solito il PC e il server sono in reti diverse (VLAN) e l'annuncio non passa: chiedi un ripetitore mDNS, oppure aggiungila a mano (punto 6).

## 4. Stampa e inserisci le credenziali

Stampa da un programma qualsiasi, scegliendo la stampante. Windows chiede il **nome utente** (il tuo del portale) e la **password**: la **chiave di stampa** del punto 1, non la password dell'account. Spunta **Ricorda le credenziali**; altrimenti Windows le richiede a ogni stampa.

## 5. Se la stampante non si collega

Se dopo 3-4 minuti non è ancora pronta, salva le credenziali prima di aggiungerla:

1. *Pannello di controllo → Account utente → Gestione credenziali → Credenziali Windows → Aggiungi credenziali Windows*.
2. **Indirizzo Internet o di rete**: il nome del server che usi per la stampante. **Nome utente**: il tuo del portale. **Password**: la **chiave di stampa**.
3. Rimuovi la stampante in *Stampanti e scanner* e ripeti il punto 3.

## 6. Aggiunta a mano

*Aggiungi dispositivo → Aggiungi manualmente → Seleziona una stampante condivisa per nome* e incolla `https://<server>:8631/printers/<coda>` (senza `:8631` dietro un proxy sulla porta 443). Windows sceglie da solo il «Microsoft IPP Class Driver». Questa strada non è ancora stata provata fino alla stampa.

## 7. Dopo

- **Chiave scaduta o revocata**: le stampe vengono rifiutate con un errore di accesso. Crea una chiave nuova (punto 1) e sostituisci quella salvata: *Gestione credenziali → Credenziali Windows*, apri la voce, cambia la password (da terminale: `cmdkey /list`, `cmdkey /delete:<voce>`).
- **Stampante rinominata dall'amministratore**: la vecchia voce smette di funzionare; toglila e aggiungi quella nuova (punto 3).
- **Stampa rifiutata**: la campanella (`/me/notices`) dice perché (quota, stampante od orario non consentiti…). Dal profilo puoi chiedere più quota o un permesso ([guida-utente.md](guida-utente.md)).
