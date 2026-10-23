# Lingue (i18n)

[Indice della documentazione](../README.md) · [English](../en/languages.md) · Italiano

Il portale parla **inglese** (la lingua dei sorgenti), **italiano**, **spagnolo**, **russo** e **cinese semplificato**.

> Le traduzioni in spagnolo, russo e cinese non sono state riviste da madrelingua.

## 1. Quale lingua usa una pagina

In quest'ordine: la scelta dell'utente in **Impostazioni → Lingua** (salvata con l'account, quindi lo segue); la lingua del browser (`Accept-Language`); `SPOOLSTEWARD_LANGUAGE` (predefinito `en`); inglese.
Un testo senza traduzione si mostra in inglese e non rompe mai la pagina. Le pagine seguono la lingua di chi le guarda: un amministratore che apre l'audit lo vede nella sua lingua.

**La pagina di accesso** non ha ancora un utente, quindi usa la lingua del browser e poi il predefinito. La frase di aiuto dipende dall'installazione («Accedi con il tuo account (aziendale/RADIUS o locale).» con RADIUS, «Accedi con il tuo account locale.» senza); entrambe sono tradotte in ogni lingua, e una frase nuova in quella pagina deve esserlo anch'essa.

## 2. Cosa *non* è tradotto, di proposito

- Il testo dell'audit in `job_log` (i motivi di rifiuto restano in inglese perché li vedono anche i client IPP; il portale mostra la stessa frase tradotta quando rifiuta una stampa).
- I log del server, i comandi `spoolsteward-admin` e i messaggi da riga di comando di backup/ripristino; le risposte HTTP di protocollo (403, 404…).
- I testi che arrivano da stampanti o CUPS (nomi degli inchiostri, firmware, avvisi del dispositivo).
- **Le pagine PDF stampate dal gateway** (prova e manutenzione): sempre in inglese; russo e cinese richiederebbero un font incorporato (`FONT_LANGUAGES` in `spoolsteward/policy/testpage.py`).
- Date e numeri (formato ISO fisso).

Come aggiungere o correggere una traduzione, e le regole per scrivere codice traducibile: [CONTRIBUTING.md](../../CONTRIBUTING.md#4-translations-and-documentation) (in inglese).
