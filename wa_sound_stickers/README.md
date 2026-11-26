# WhatsApp Voice Sticker Studio (Django)

Create and send WhatsApp **Voice / Sound Stickers** (including Malayalam Memes & BGM, live voice recording, or synced video audio) directly from WhatsApp Web—even without WhatsApp Beta.

## Quick Setup & Run

```bash
cd ~/Downloads/wa_sound_stickers
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver 127.0.0.1:8765 --noreload
```

Then open **http://127.0.0.1:8765/** in your browser.
