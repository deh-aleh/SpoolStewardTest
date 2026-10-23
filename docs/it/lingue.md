# Lingue (i18n)

[Indice della documentazione](../README.md) · [English](../en/languages.md) · Italiano

Il portale parla **inglese** (la lingua dei sorgenti), **italiano**, **spagnolo**, **russo** e **cinese semplificato**.

> Le traduzioni in spagnolo, russo e cinese non sono state riviste da madrelingua.

## 1. Quale lingua usa una pagina

In quest'ordine:

1. la scelta dell'utente in **Impostazioni → Lingua** (salvata con l'account, quindi lo segue su ogni dispositivo);
2. la lingua del browser (`Accept-Language`);
3. `SPOOLSTEWARD_LANGUAGE` (predefinito `en`);
4. inglese.

Un testo senza traduzione si mostra in inglese: non rompe mai la pagina. Le pagine seguono la lingua di chi le guarda: un amministratore
che apre l'audit lo vede nella sua lingua.

**La pagina di accesso** non ha ancora un utente, quindi usa sempre la lingua del browser (punto 2) e poi il predefinito. La frase di aiuto
dipende dall'installazione: con RADIUS configurato dice «Accedi con il tuo account (aziendale/RADIUS o locale).»; senza RADIUS solo «Accedi con
il tuo account locale.». Entrambe sono tradotte in ogni lingua; una frase nuova in quella pagina deve esserlo anch'essa.


## 2. Cosa *non* è tradotto, di proposito

- Il testo dell'audit scritto in `job_log` (i motivi di rifiuto restano in inglese perché li vedono anche i client IPP; il portale mostra
  la stessa frase tradotta quando rifiuta una stampa).
- I log del server, i comandi `spoolsteward-admin` e i messaggi da riga di comando di backup/ripristino.
- Le risposte HTTP di protocollo (403, 404…).
- I testi che arrivano da stampanti o CUPS (nomi degli inchiostri, firmware, avvisi del dispositivo).
- **Le pagine PDF stampate dal gateway** (prova e manutenzione): sempre in inglese. Russo e cinese non si possono scrivere con i font
  base dei PDF; servirebbe un font incorporato (`FONT_LANGUAGES` in `spoolsteward/policy/testpage.py`).
- Date e numeri non seguono la lingua (formato ISO fisso).

Come aggiungere o correggere una traduzione, e le regole per scrivere codice traducibile, sono in [CONTRIBUTING.md](../../CONTRIBUTING.md#4-translations-and-documentation) (in inglese).
