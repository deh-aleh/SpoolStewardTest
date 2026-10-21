# Pictures of the documentation

The pages in `docs/en` and `docs/it` take their pictures from this folder. The same Markdown is read on GitHub and in
the portal (`/docs`), so a picture is written once, with a relative path, and works in both places.

## Where a picture goes

| Picture | Folder | Written in a page as |
|---|---|---|
| Diagram without text, or with text that does not need translating | `docs/images/` | `![Architecture](../images/architecture.png)` |
| Screenshot of the portal in English, English diagram | `docs/images/en/` | `![The print key on the profile page](../images/en/me-print-key.png)` |
| Screenshot of the portal in Italian, Italian diagram | `docs/images/it/` | `![La chiave di stampa nella pagina del profilo](../images/it/me-print-key.png)` |

- A page of `docs/en` uses only `images/en/` and `images/`; a page of `docs/it` only `images/it/` and `images/`.
- The same screenshot has the same file name in `en/` and `it/`.
- `README.md` at the top of the repository writes `docs/images/en/...`, `README.it.md` writes `docs/images/it/...`.
- At most one more folder inside `en/` or `it/` (for example `en/windows/step-1.png`).

On GitHub the relative path leads to the file. In the portal the page renderer rewrites it to `/docs/images/...`; that
address serves only this folder.

## Rules

- **Formats**: PNG for screenshots, PNG or SVG for diagrams, JPEG or WebP for photos. Nothing else is served.
- **Names**: lower case, words joined by `-`, no spaces: `admin-printers.png`, `windows-add-printer-2.png`.
- **Alt text is required**: `![What the picture shows](...)`, in the language of the page. It is read by screen
  readers and shown when the picture cannot load.
- **Size**: at most 1 MiB per file (the portal refuses larger files). Aim for under 300 KB.
- **Width**: take screenshots of a browser window about 1280 px wide, at 100% zoom; crop to the part the text talks
  about. Phone screenshots: crop the status bar.
- **Background**: diagrams need an opaque background (white). A transparent one is unreadable in the dark theme of
  GitHub.
- **No personal data**: test users, `printer.example.lan`, `192.0.2.x` addresses. No real names, keys or passwords.
- **Only Markdown images**: an HTML `<img>` tag is shown as text in the portal.
- **SVG**: no scripts, no external fonts or links; the portal serves it with a policy that blocks them anyway.

## Keeping files small

When the tools are installed:

```sh
pngquant --quality 70-90 --skip-if-larger --strip --ext .png --force docs/images/en/*.png
oxipng -o 4 --strip safe docs/images/en/*.png
```

`pngquant` reduces the colours (screenshots barely change), `oxipng` recompresses without loss.

## Checks

`tests/test_docs_images.py` fails when a page points to a missing picture, uses an empty alt text, a picture of the
other language, a picture outside this folder or a file that is too large; it also fails for a picture in this folder
that no page uses.

This file is not a page of the documentation: the portal lists only `docs/en/*.md` and `docs/it/*.md`.
