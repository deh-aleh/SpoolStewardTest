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
  base dei PDF; servirebbe un font incorporato (`FONT_LANGUAGES` in `app/policy/testpage.py`).
- Date e numeri non seguono la lingua (formato ISO fisso).

## 3. Aggiungere o correggere una traduzione

I cataloghi sono file gettext: `app/locales/<lingua>/messages.po` (`msgid` = testo inglese, `msgstr` = traduzione). Si leggono
all'avvio e si compilano in memoria con Babel — nessun file `.mo`, nessuna compilazione. I segnaposto `%(nome)s` restano uguali e un `%`
letterale si scrive `%%`.

```sh
python scripts/i18n.py extract    # aggiorna i cataloghi dal codice
python scripts/i18n.py missing    # cosa manca ancora
python scripts/i18n.py check      # completezza e segnaposto
pytest tests/test_i18n.py         # fallisce se un catalogo è incompleto o ha segnaposto sbagliati
```

Lingua nuova: aggiungila a `LANGUAGES` in `app/i18n.py` e a `LANGS` in `scripts/i18n.py`, poi esegui `extract`.

## 4. Regole per chi scrive il codice

- Ogni testo visibile è inglese dentro `_("...")` (Python: `from app.i18n import _`; template: `{{ _("...") }}`).
- Le voci di menu sono corte di proposito e possono differire dai titoli delle pagine: usano `_p("menu", "Backup")` (stesso testo, contesto `menu`). Per tradurne una, dai a `scripts/i18n.py add` la chiave `"menu::Backup"`.
- I valori sono sempre nominati (`_("Key %(label)s revoked", label=x)`), mai concatenati.
- I plurali usano `ngettext` (il russo ha tre forme).
- Nei template, un testo per JavaScript usa `_js(...)` (non formattato, `%(nome)s` sostituito dallo script) o `_t(...)|tojson` (per `confirm()`).
- I dati in cache (stati delle code, etichette dei contatori) sono *chiavi* inglesi tradotte solo al momento di mostrarle. Gli enum si
  traducono con `_(valore)` (elenco in `VALUE_LABELS`).
- `Decision.reason` (audit, client IPP) resta inglese; `Decision.text()` è la frase localizzata per il portale.

## 5. Come funziona

`app/i18n.py` tiene la lingua della richiesta in una `ContextVar` fissata da un middleware ASGI che sta dentro quello delle sessioni. La
`ContextVar` segue la richiesta nei thread dei gestori sincroni e nelle attività che avvia (scansione dal portale), quindi `_()` funziona
ovunque dentro una richiesta. Fuori da una richiesta (scheduler) vale il valore predefinito. Nei template `_()` restituisce Markup fidato;
`_t()` restituisce testo semplice. `user.language` (migrazione 0007) si scrive al login e si riallinea se cambia da un altro dispositivo.
