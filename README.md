# Regen Column GUI

Local Flask GUI for running dual Reglo ICC pumps with an optional Valco selector valve.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python app.py
```

The app starts on the first available port beginning at `5001`, then prints the local URL.
