# kb-locale

**La tua base di conoscenza privata da video, audio, PDF e slide. Tutto sul tuo computer, niente cloud.**

Hai ore di video, registrazioni di call, webinar, PDF e presentazioni sparsi in cartelle?
`kb-locale` li trasforma in un archivio in cui cerchi **per significato** e ricevi la risposta
**con la citazione esatta**: il file, la sezione e il **minuto** del video (o la slide).

```
$ python kb.py search "come preparare il video sorgente per un avatar"
[
 {
  "score": 0.71,
  "citazione": "Webinar avatar › 03 - Registrazione › Il video sorgente › 12:40–14:05",
  "tipo": "parlato",
  "testo": "…luce frontale morbida, sfondo fermo, bocca chiusa a fine frase…"
 }
]
```

Lo uso ogni giorno come "memoria" per i miei agenti AI: quando scrivono o pianificano, prima
cercano qui cosa ho già studiato, poi citano la fonte.

## Cosa fa

| Passo | Strumento (tutto locale) |
|---|---|
| Video e audio → testo con minutaggi | [whisper.cpp](https://github.com/ggml-org/whisper.cpp) server (consigliato `large-v3-turbo`, meglio su GPU) |
| Slide dentro i video | ffmpeg (cambio scena + 1 fotogramma al minuto) → OCR con Tesseract |
| PDF, DOCX, PPTX, HTML, EPUB | [Docling](https://github.com/docling-project/docling) → Markdown |
| Significato del testo | `bge-m3` (multilingue, ottimo in italiano) con [Ollama](https://ollama.com) |
| Indice e ricerca | [Qdrant](https://qdrant.tech) in Docker, solo su `127.0.0.1` |
| Registro | SQLite: un file già indicizzato (stesso contenuto) non viene rifatto |

## Requisiti

- Python 3.11+, `ffmpeg`, `tesseract` (con `tesseract-ocr-ita`), Docker
- Ollama con `ollama pull bge-m3`
- whisper.cpp in modalità server, per esempio:
  `./build/bin/whisper-server -m models/ggml-large-v3-turbo.bin --host 127.0.0.1 --port 8080`

## Avvio in 5 minuti

```bash
git clone https://github.com/francescogruner/kb-locale && cd kb-locale
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
docker compose up -d                      # Qdrant su 127.0.0.1:6333
cp .env.example .env && set -a && . ./.env && set +a

python kb.py process ~/Video/webinar-avatar --raccolta "Webinar avatar"
python kb.py list
python kb.py search "come scelgo il microfono" --raccolta "Webinar avatar"
```

## Usarlo dai tuoi agenti

`python kb.py serve` avvia una piccola API di sola ricerca (`GET /search?q=...`) protetta da token
(`KB_API_TOKEN`). Di default ascolta solo su `127.0.0.1`: se la esponi in rete, fallo solo su una
rete privata o dietro un proxy con HTTPS.

## Numeri reali (mini-PC con Ryzen AI Max+ 395, 128 GB)

- Trascrizione: circa 2–4 minuti di GPU per ogni ora di video (`large-v3-turbo`)
- Spazio derivato: 150–300 MB per ora di video (testi, fotogrammi, vettori)
- Un corso da 60 video e 60 PDF: circa 2.000 pezzi indicizzati, ricerca sotto il secondo

## Uso responsabile

Indicizza solo materiale tuo o su cui hai i diritti. L'archivio resta privato sul tuo computer:
non è pensato per ripubblicare contenuti di altri.

## Licenza

MIT — Francesco Gruner. Se ti è utile, una ⭐ aiuta.
Parlo di AI pratica su [francescogruner.it](https://francescogruner.it) e su YouTube.
