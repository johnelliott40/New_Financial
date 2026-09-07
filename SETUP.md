# SETUP.md

## Create your local environment

A Python virtual environment (`.venv`) is not shipped in this folder — venvs are OS-specific binaries and won't run on a different machine than the one that built them, so a Linux `.venv` built during development is useless on your machine. Create your own locally, it takes seconds:

```bash
cd financial-model
python -m venv .venv
```

Activate it:

- macOS/Linux: `source .venv/bin/activate`
- Windows (PowerShell): `.venv\Scripts\Activate.ps1`
- Windows (cmd): `.venv\Scripts\activate.bat`

Install dependencies:

```bash
pip install -r requirements.txt
```

## Run the app

```bash
streamlit run app.py
```

## Run tests

```bash
pytest
```

## Notes

- Everything here was developed and tested against Python 3.10+ using this same `requirements.txt`.
- `.venv/` should stay out of version control (see `.gitignore`).
