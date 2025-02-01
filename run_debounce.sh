#!/bin/bash
# Tentukan direktori skrip secara dinamis
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Jalankan aplikasi dalam mode background tanpa GUI (tanpa ampersand)
sudo python3 "$DIR/debounce_keyboard.py" --nogui
if [ $? -eq 0 ]; then
    echo "Success: Aplikasi debounce keyboard telah dijalankan dalam mode background."
else
    echo "Error: Gagal menjalankan aplikasi debounce keyboard." >&2
fi
