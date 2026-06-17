"""
Test da riga di comando, senza Telegram.

    export ANTHROPIC_API_KEY=...
    python -m contraddittorio.cli "testo del messaggio da verificare"
    # oppure
    echo "testo lungo..." | python -m contraddittorio.cli
"""

from __future__ import annotations

import os
import sys

from .pipeline import analyze_message


def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Manca ANTHROPIC_API_KEY nell'ambiente.")

    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
    else:
        text = sys.stdin.read()

    text = text.strip()
    if len(text) < 40:
        raise SystemExit("Testo troppo corto: incolla il messaggio completo.")

    model = os.environ.get("CONTRADDITTORIO_MODEL", "claude-sonnet-4-6")
    print(analyze_message(text, model))


if __name__ == "__main__":
    main()
