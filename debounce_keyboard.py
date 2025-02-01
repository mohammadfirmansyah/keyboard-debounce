#!/usr/bin/env python3
"""
Script ini memastikan library yang diperlukan (evdev, python-uinput) terinstal.
Aplikasi dapat dijalankan dengan GUI (default) atau dalam mode background (--nogui).
Konfigurasi (threshold dan bahasa) serta log akan disimpan secara persistens dalam file teks
di folder yang sama. Aplikasi hanya dijalankan satu kali; jika dijalankan lagi, instance sebelumnya
akan dihentikan dan aplikasi direstart.
"""

import sys
import os
import json
import subprocess
import threading
import time
import atexit
import signal

# Pastikan dijalankan dengan Python 3
if sys.version_info[0] < 3:
    print("Script ini harus dijalankan dengan Python 3.")
    sys.exit(1)

try:
    import pip
except ImportError:
    print("pip tidak ditemukan. Silakan instal pip untuk Python 3.")
    sys.exit(1)

def install_package(package, pip_name=None):
    pip_name = pip_name or package
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--break-system-packages", pip_name])
    except Exception as e:
        print(f"Terjadi kesalahan saat menginstal {pip_name}: {e}")
        sys.exit(1)

try:
    import evdev
except ImportError:
    install_package("evdev")

try:
    import uinput
except ImportError:
    install_package("python-uinput", "python-uinput")

# --------------------------------------------------------------------
#                   IMPOR YANG DIPERLUKAN
# --------------------------------------------------------------------
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from evdev import InputDevice, categorize, ecodes, list_devices

# --------------------------------------------------------------------
#           DEFINISI FOLDER DASAR (untuk file konfigurasi & log)
# --------------------------------------------------------------------
base_dir = os.path.dirname(os.path.realpath(__file__))
config_file = os.path.join(base_dir, "config.txt")
log_input_file = os.path.join(base_dir, "log_input.txt")
log_chatter_file = os.path.join(base_dir, "log_chatter.txt")
pid_file = os.path.join(base_dir, "debounce_keyboard.pid")

# --------------------------------------------------------------------
#         SINGLE INSTANCE: PENGECEKAN DAN PENULISAN FILE PID
# --------------------------------------------------------------------
def remove_pid_file():
    if os.path.exists(pid_file):
        os.remove(pid_file)
# Fungsi ini hanya akan dipanggil saat aplikasi benar-benar berhenti.
atexit.register(remove_pid_file)

if os.path.exists(pid_file):
    try:
        with open(pid_file, "r") as f:
            old_pid = int(f.read().strip())
        # Cek apakah proses dengan old_pid masih berjalan
        os.kill(old_pid, 0)
        # Jika masih berjalan, hentikan proses tersebut
        os.kill(old_pid, signal.SIGTERM)
        time.sleep(1)
        print("Instance sudah berjalan. Instance sebelumnya telah dihentikan dan aplikasi akan direstart.")
    except Exception as e:
        pass

with open(pid_file, "w") as f:
    f.write(str(os.getpid()))

# --------------------------------------------------------------------
#                   KONFIGURASI DAN STRUKTUR DATA
# --------------------------------------------------------------------
debounce_time = 0.05  # 50 ms dalam detik
current_language = 'id'  # 'id' atau 'en'
last_valid_down_time_per_key = {}
valid_keys = {}
last_global_down_time_for_logging = None
input_events = []
chatter_events = []

# --------------------------------------------------------------------
#             PEMETAAN TEKS GUI (UI) DALAM 2 BAHASA
# --------------------------------------------------------------------
LANG_UI = {
    'id': {
        'window_title': "Penyaring Papan Ketik (Per-Tombol)",
        'tab_input_log': "Log Masukan",
        'tab_chatter_log': "Log Chattering",
        'btn_apply': "Terapkan",
        'btn_id': "ID",
        'btn_en': "EN",
        'label_threshold': "Batas Chattering (ms):"
    },
    'en': {
        'window_title': "Keyboard Debounce (Per-Key)",
        'tab_input_log': "Input Log",
        'tab_chatter_log': "Chattering Log",
        'btn_apply': "Apply",
        'btn_id': "ID",
        'btn_en': "EN",
        'label_threshold': "Debounce Threshold (ms):"
    }
}

# --------------------------------------------------------------------
#         PEMETAAN TEKS UNTUK LOG (MSG_ID) DALAM 2 BAHASA
# --------------------------------------------------------------------
LANG_MSG = {
    'device_found': {
        'id': "Perangkat keyboard terdeteksi: {device_name} ({device_path})",
        'en': "Keyboard device found: {device_name} ({device_path})"
    },
    'no_device': {
        'id': "Tidak ditemukan perangkat keyboard!",
        'en': "No keyboard device found!"
    },
    'inject_down_first_global': {
        'id': "Memproses tombol {key} DOWN (pertama secara global, tanpa jeda)",
        'en': "Processing {key} DOWN (global first event, no delay)"
    },
    'inject_down_subsequent': {
        'id': "Memproses tombol {key} DOWN (jeda: {delay} ms dari penekanan sebelumnya)",
        'en': "Processing {key} DOWN (delay: {delay} ms from previous press)"
    },
    'inject_up': {
        'id': "Memproses tombol {key} UP",
        'en': "Processing {key} UP"
    },
    'inject_hold': {
        'id': "Memproses tombol {key} HOLD",
        'en': "Processing {key} HOLD"
    },
    'threshold_update': {
        'id': "Batas chattering diubah menjadi {threshold} ms",
        'en': "Debounce threshold updated to {threshold} ms"
    },
    'inject_down_error': {
        'id': "Terjadi kesalahan saat inject DOWN untuk {key}: {error}",
        'en': "Error injecting DOWN for {key}: {error}"
    },
    'inject_up_error': {
        'id': "Terjadi kesalahan saat inject UP untuk {key}: {error}",
        'en': "Error injecting UP for {key}: {error}"
    },
    'chatter_detected': {
        'id': "Chattering terdeteksi pada {key}: jeda {delay} ms (< {threshold} ms)",
        'en': "Chattering detected on {key}: delay {delay} ms (< {threshold} ms)"
    }
}

# --------------------------------------------------------------------
#      FUNGSI MEMBUAT PERANGKAT VIRTUAL DENGAN UINPUT
# --------------------------------------------------------------------
def create_uinput_device():
    available_keys = [getattr(uinput, attr) for attr in dir(uinput) if attr.startswith("KEY_")]
    return uinput.Device(available_keys, name="Virtual Keyboard")

uinput_device = create_uinput_device()

# --------------------------------------------------------------------
#            FUNGSI LOAD & SAVE KONFIGURASI
# --------------------------------------------------------------------
def load_config():
    global debounce_time, current_language
    if os.path.exists(config_file):
        with open(config_file, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("threshold="):
                    try:
                        val = float(line.split("=")[1])
                        debounce_time = val / 1000.0
                    except:
                        pass
                elif line.startswith("language="):
                    current_language = line.split("=")[1].strip()

def save_config():
    with open(config_file, "w") as f:
        f.write(f"threshold={int(debounce_time*1000)}\n")
        f.write(f"language={current_language}\n")

# --------------------------------------------------------------------
#            FUNGSI LOAD LOG DARI FILE (LINE-DL JSON)
# --------------------------------------------------------------------
def load_logs():
    global input_events, chatter_events
    if os.path.exists(log_input_file):
        with open(log_input_file, "r") as f:
            for line in f:
                try:
                    evt = json.loads(line)
                    input_events.append(evt)
                except:
                    pass
    if os.path.exists(log_chatter_file):
        with open(log_chatter_file, "r") as f:
            for line in f:
                try:
                    evt = json.loads(line)
                    chatter_events.append(evt)
                except:
                    pass

# --------------------------------------------------------------------
#            VARIABEL UNTUK MENJAGA POSISI LOG YANG TELAH DIRENDER
# --------------------------------------------------------------------
last_rendered_input_index = 0
last_rendered_chatter_index = 0

# --------------------------------------------------------------------
#              FUNGSI MENYIMPAN DAN RENDER LOG
# --------------------------------------------------------------------
def add_input_event(msg_id, placeholders=None):
    if placeholders is None:
        placeholders = {}
    evt = {
        'timestamp': time.time(),
        'msg_id': msg_id,
        'placeholders': placeholders
    }
    input_events.append(evt)
    append_new_input_events(evt)

def add_chatter_event(msg_id, placeholders=None):
    if placeholders is None:
        placeholders = {}
    evt = {
        'timestamp': time.time(),
        'msg_id': msg_id,
        'placeholders': placeholders
    }
    chatter_events.append(evt)
    append_new_chatter_events(evt)

def translate_event(evt):
    msg_id = evt['msg_id']
    placeholders = evt['placeholders']
    timestamp = evt['timestamp']
    template = LANG_MSG.get(msg_id, {}).get(current_language, "[UNDEFINED MSG_ID]")
    rendered_text = template.format(**placeholders)
    ts_struct = time.localtime(timestamp)
    time_str = time.strftime("%H:%M:%S", ts_struct)
    return f"[{time_str}] {rendered_text}"

def append_new_input_events(new_evt):
    global last_rendered_input_index
    if use_gui:
        input_text.configure(state='normal')
        line = translate_event(new_evt) + "\n"
        input_text.insert(tk.END, line)
        input_text.configure(state='disabled')
        input_text.yview_moveto(1.0)
    with open(log_input_file, "a") as f:
        f.write(json.dumps(new_evt) + "\n")
    last_rendered_input_index = len(input_events)

def append_new_chatter_events(new_evt):
    global last_rendered_chatter_index
    if use_gui:
        chatter_text.configure(state='normal')
        line = translate_event(new_evt) + "\n"
        chatter_text.insert(tk.END, line)
        chatter_text.configure(state='disabled')
        chatter_text.yview_moveto(1.0)
    with open(log_chatter_file, "a") as f:
        f.write(json.dumps(new_evt) + "\n")
    last_rendered_chatter_index = len(chatter_events)

def render_all_logs():
    if use_gui:
        input_text.configure(state='normal')
        input_text.delete("1.0", tk.END)
        for evt in input_events:
            input_text.insert(tk.END, translate_event(evt) + "\n")
        input_text.configure(state='disabled')
        input_text.yview_moveto(1.0)
    
        chatter_text.configure(state='normal')
        chatter_text.delete("1.0", tk.END)
        for evt in chatter_events:
            chatter_text.insert(tk.END, translate_event(evt) + "\n")
        chatter_text.configure(state='disabled')
        chatter_text.yview_moveto(1.0)
    apply_ui_language()

# --------------------------------------------------------------------
#               MENGUBAH BAHASA GUI SECARA DINAMIS
# --------------------------------------------------------------------
def apply_ui_language():
    lang_ui = LANG_UI[current_language]
    if use_gui:
        # Pastikan title bar muncul dengan mengatur title jendela
        root.title(lang_ui['window_title'])
        debounce_label.configure(text=lang_ui['label_threshold'])
        apply_button.configure(text=lang_ui['btn_apply'])
        lang_button_id.configure(text=lang_ui['btn_id'])
        lang_button_en.configure(text=lang_ui['btn_en'])
        notebook.tab(tab1, text=lang_ui['tab_input_log'])
        notebook.tab(tab2, text=lang_ui['tab_chatter_log'])

def switch_language_to(lang):
    global current_language
    current_language = lang
    save_config()
    render_all_logs()

# --------------------------------------------------------------------
#                 MENGUBAH KONFIGURASI (LOAD & SAVE)
# --------------------------------------------------------------------
def load_config():
    global debounce_time, current_language
    if os.path.exists(config_file):
        with open(config_file, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("threshold="):
                    try:
                        val = float(line.split("=")[1])
                        debounce_time = val / 1000.0
                    except:
                        pass
                elif line.startswith("language="):
                    current_language = line.split("=")[1].strip()

def save_config():
    with open(config_file, "w") as f:
        f.write(f"threshold={int(debounce_time*1000)}\n")
        f.write(f"language={current_language}\n")

# --------------------------------------------------------------------
#                 MENGAMBIL LOG DARI FILE (LINE-DL JSON)
# --------------------------------------------------------------------
def load_logs():
    global input_events, chatter_events, last_rendered_input_index, last_rendered_chatter_index
    if os.path.exists(log_input_file):
        with open(log_input_file, "r") as f:
            for line in f:
                try:
                    evt = json.loads(line)
                    input_events.append(evt)
                except:
                    pass
    if os.path.exists(log_chatter_file):
        with open(log_chatter_file, "r") as f:
            for line in f:
                try:
                    evt = json.loads(line)
                    chatter_events.append(evt)
                except:
                    pass
    last_rendered_input_index = len(input_events)
    last_rendered_chatter_index = len(chatter_events)

# --------------------------------------------------------------------
#                 MENCARI PERANGKAT KEYBOARD FISIK
# --------------------------------------------------------------------
def find_keyboard_device():
    devices = [InputDevice(path) for path in list_devices()]
    for dev in devices:
        caps = dev.capabilities()
        if ecodes.EV_KEY in caps and ecodes.KEY_A in caps[ecodes.EV_KEY]:
            return dev
    return None

# --------------------------------------------------------------------
#              FUNGSI UTAMA MONITOR KEYBOARD
# --------------------------------------------------------------------
def monitor_keyboard():
    global debounce_time, last_global_down_time_for_logging

    dev = find_keyboard_device()
    if dev is None:
        add_input_event('no_device')
        return

    # Jangan mengambil alih perangkat secara eksklusif, agar aplikasi lain (misalnya QEMU/KVM)
    # tetap dapat menerima event dari perangkat input.
    # try:
    #     dev.grab()
    # except:
    #     pass

    add_input_event('device_found', {
        'device_name': dev.name,
        'device_path': dev.path
    })

    while True:
        event = dev.read_one()
        if event is None:
            time.sleep(0.01)
            continue
        if event.type != ecodes.EV_KEY:
            continue
        key_event = categorize(event)
        # Tangani kasus di mana keycode berupa list
        key = key_event.keycode if isinstance(key_event.keycode, str) else key_event.keycode[0]
        now = time.time()
        if key_event.keystate == 1:  # Key DOWN
            last_time_key = last_valid_down_time_per_key.get(key, None)
            if last_time_key is not None:
                delay_key = now - last_time_key
                if delay_key < debounce_time:
                    add_chatter_event('chatter_detected', {
                        'key': key,
                        'delay': int(delay_key * 1000),
                        'threshold': int(debounce_time * 1000)
                    })
                    continue
                else:
                    last_valid_down_time_per_key[key] = now
            else:
                last_valid_down_time_per_key[key] = now

            try:
                uinput_device.emit(getattr(uinput, key), 1)
            except Exception as e:
                add_input_event('inject_down_error', {
                    'key': key,
                    'error': str(e)
                })
            valid_keys[key] = now

            if last_global_down_time_for_logging is None:
                last_global_down_time_for_logging = now
                add_input_event('inject_down_first_global', {'key': key})
            else:
                delay_global = now - last_global_down_time_for_logging
                last_global_down_time_for_logging = now
                add_input_event('inject_down_subsequent', {
                    'key': key,
                    'delay': int(delay_global * 1000)
                })
        elif key_event.keystate == 0:  # Key UP
            if key in valid_keys:
                try:
                    uinput_device.emit(getattr(uinput, key), 0)
                except Exception as e:
                    add_input_event('inject_up_error', {
                        'key': key,
                        'error': str(e)
                    })
                del valid_keys[key]
                add_input_event('inject_up', {'key': key})
        else:
            add_input_event('inject_hold', {'key': key})

# --------------------------------------------------------------------
#                UPDATE THRESHOLD VIA GUI
# --------------------------------------------------------------------
def update_debounce_threshold():
    global debounce_time
    try:
        val_ms = float(debounce_entry.get())
        debounce_time = val_ms / 1000.0
        add_input_event('threshold_update', {'threshold': int(val_ms)})
        save_config()
    except ValueError:
        pass

# --------------------------------------------------------------------
#                   MODE BACKGROUND TANPA GUI
# --------------------------------------------------------------------
def run_background():
    load_config()
    load_logs()
    monitor_keyboard()

# --------------------------------------------------------------------
#                   MEMBANGUN GUI DENGAN TKINTER
# --------------------------------------------------------------------
use_gui = ("--nogui" not in sys.argv)

if use_gui:
    load_config()
    load_logs()

    root = tk.Tk()
    root.geometry("800x500")
    # Pastikan dekorasi jendela (title bar) ditampilkan.
    root.title(LANG_UI[current_language]['window_title'])

    # Handler untuk mencegah aplikasi berhenti saat jendela GUI ditutup.
    # Alih-alih menghentikan aplikasi, jendela akan disembunyikan (withdraw).
    def on_closing():
        root.withdraw()  # Sembunyikan jendela, tetapi proses tetap berjalan.
        print("GUI disembunyikan, aplikasi tetap berjalan di background.")

    root.protocol("WM_DELETE_WINDOW", on_closing)

    # Terapkan tema ttk "clam"
    style = ttk.Style(root)
    style.theme_use("clam")

    top_frame = ttk.Frame(root, padding="10")
    top_frame.pack(fill=tk.X)

    debounce_label = ttk.Label(top_frame, text=LANG_UI[current_language]['label_threshold'])
    debounce_label.pack(side=tk.LEFT, padx=(0,5))

    debounce_entry = ttk.Entry(top_frame, width=10)
    debounce_entry.insert(0, str(int(debounce_time * 1000)))
    debounce_entry.pack(side=tk.LEFT)

    apply_button = ttk.Button(top_frame, text=LANG_UI[current_language]['btn_apply'], command=update_debounce_threshold)
    apply_button.pack(side=tk.LEFT, padx=(5,10))

    # Tombol ubah bahasa di sebelah kanan (lebar persegi)
    lang_button_en = ttk.Button(top_frame, text=LANG_UI[current_language]['btn_en'], width=3, command=lambda: switch_language_to('en'))
    lang_button_en.pack(side=tk.RIGHT, padx=(5,5))
    lang_button_id = ttk.Button(top_frame, text=LANG_UI[current_language]['btn_id'], width=3, command=lambda: switch_language_to('id'))
    lang_button_id.pack(side=tk.RIGHT, padx=(5,5))

    notebook = ttk.Notebook(root)
    notebook.pack(fill=tk.BOTH, expand=True)

    tab1 = ttk.Frame(notebook)
    notebook.add(tab1, text=LANG_UI[current_language]['tab_input_log'])

    tab2 = ttk.Frame(notebook)
    notebook.add(tab2, text=LANG_UI[current_language]['tab_chatter_log'])

    input_text = ScrolledText(tab1, height=20, state='disabled')
    input_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    chatter_text = ScrolledText(tab2, height=20, state='disabled')
    chatter_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    bottom_frame = ttk.Frame(root, padding="5")
    bottom_frame.pack(side=tk.BOTTOM, fill=tk.X)
    copyright_label = ttk.Label(bottom_frame, text="© 2025 Mohammad Firman Syah v1.0.0", anchor="e")
    copyright_label.pack(side=tk.RIGHT)

    monitor_thread = threading.Thread(target=monitor_keyboard, daemon=True)
    monitor_thread.start()

    def initial_render():
        render_all_logs()
    initial_render()

    root.mainloop()
else:
    run_background()
