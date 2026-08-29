# 🔴 RedNotepad

> A simple Windows notepad built with PyQt6.

RedNotepad is a small, straightforward text editor for Windows.  
The archive includes the normal edition and an optional encrypted edition.

## 📦 Files

- `red_notepad.py` — normal edition
- `red_notepad_sensitive.py` — optional sensitive-data edition
- `README.md` — project information
- `LICENSE` — MIT License

## 🚀 Install

Python 3.10 or newer is recommended.

```bash
pip install PyQt6 charset-normalizer pycryptodome
```

## ▶️ Run

Normal edition:

```bash
python red_notepad.py
```

Sensitive-data edition:

```bash
python red_notepad_sensitive.py
```

You can also pass a text file from the command line:

```bash
python red_notepad.py example.txt
```

## 🔐 Sensitive Data Edition

The sensitive-data edition can store encrypted documents using AES-256-GCM.

For normal text editing, the standard edition is recommended. Encrypted files are application-specific, so other editors and cloud services may not be able to preview or parse their contents.

Keep your password safe. Lost passwords cannot be recovered.

## 📄 License

MIT License.
