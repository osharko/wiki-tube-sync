#!/usr/bin/env bash
# Simplifica l'uso della pipeline. Delega a manage.py.
# Uso: ./ingest.sh <subcomando> [<url>]
#   fetch <url>        -> scarica/importa un video (o playlist con list=)
#   sync  <channel>    -> importa i nuovi video di un canale
#   detect <channel>   -> elenca i video del canale
#   build              -> rigenera tutte le pagine dai dati raw
set -euo pipefail
cd "$(dirname "$0")"

if [ "$#" -eq 0 ]; then
  exec ./venv/bin/python manage.py --help 2>/dev/null || exec ./manage.py
fi

exec ./venv/bin/python manage.py "$@"
