#!/usr/bin/env python3
"""
Script ini memastikan library yang diperlukan (evdev, python-uinput, pygame, ttkthemes) terinstal.
Aplikasi dapat dijalankan dengan GUI (default) atau dalam mode background (--nogui).
Konfigurasi (global bounce threshold, batas khusus per tombol, shortcut pause/continue, mode deteksi,
advanced settings, dsb.) serta log akan disimpan secara persisten dalam direktori yang sama
dengan file python ini. Aplikasi hanya dijalankan satu kali; jika dijalankan lagi,
instance sebelumnya akan dihentikan dan aplikasi akan direstart.
"""

import sys
import os
import json
import subprocess
import threading
import time
import atexit
import signal
import queue

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
    """
    Fungsi untuk melakukan pemasangan (install) package yang dibutuhkan
    melalui pip, jika package tersebut belum terpasang.
    """
    pip_name = pip_name or package
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                               "--break-system-packages", pip_name])
    except Exception as e:
        print(f"Terjadi kesalahan saat menginstal {pip_name}: {e}")
        sys.exit(1)

# Pastikan library evdev terpasang
try:
    import evdev
except ImportError:
    install_package("evdev")

# Pastikan library python-uinput terpasang
try:
    import uinput
except ImportError:
    install_package("python-uinput", "python-uinput")

# Pastikan pygame terpasang
try:
    import pygame
except ImportError:
    install_package("pygame")
    import pygame

# Pastikan paket ttkthemes terpasang
try:
    from ttkthemes import ThemedTk
except ImportError:
    install_package("ttkthemes")
    from ttkthemes import ThemedTk

# Jika dijalankan dengan sudo, ambil UID user asli dan tetapkan XDG_RUNTIME_DIR-nya
if os.getuid() == 0 and "SUDO_UID" in os.environ:
    sudo_uid = os.environ["SUDO_UID"]
    os.environ["XDG_RUNTIME_DIR"] = f"/run/user/{sudo_uid}"
os.environ['SDL_AUDIODRIVER'] = 'alsa'

sound_enabled = True
try:
    pygame.mixer.init()
except pygame.error as e:
    print(f"Audio device error: {e}. Efek suara dinonaktifkan.")
    sound_enabled = False

import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from evdev import InputDevice, categorize, ecodes, list_devices

# --------------------------------------------------------------------
# DEFINISI BASE DIRECTORY
# --------------------------------------------------------------------
script_dir = os.path.dirname(os.path.realpath(__file__))

config_file = os.path.join(script_dir, "config.txt")
log_input_file = os.path.join(script_dir, "log_input.txt")
log_bounce_file = os.path.join(script_dir, "log_bounce.txt")
pid_file = os.path.join(script_dir, "debounce_keyboard.pid")

def ensure_file_exists(filepath):
    if not os.path.exists(filepath):
        with open(filepath, "w") as f:
            f.write("")

ensure_file_exists(config_file)
ensure_file_exists(log_input_file)
ensure_file_exists(log_bounce_file)

# --------------------------------------------------------------------
# SINGLE INSTANCE
# --------------------------------------------------------------------
def remove_pid_file():
    if os.path.exists(pid_file):
        os.remove(pid_file)

atexit.register(remove_pid_file)

if os.path.exists(pid_file):
    try:
        with open(pid_file, "r") as f:
            old_pid = int(f.read().strip())
        os.kill(old_pid, 0)
        os.kill(old_pid, signal.SIGTERM)
        time.sleep(1)
        print("Instance sudah berjalan. Instance sebelumnya telah dihentikan dan aplikasi akan direstart.")
    except Exception:
        pass

with open(pid_file, "w") as f:
    f.write(str(os.getpid()))

# --------------------------------------------------------------------
# KONFIGURASI & VAR GLOB
# --------------------------------------------------------------------
bounce_time = 0.1   # (default 100 ms)
current_language = 'id'  # 'id' atau 'en'
custom_thresholds = {}
shortcut_pause = ""
shortcut_continue = ""
paused = False

manual_pause = False
manual_continue = False

# "down" = After Press, "up" = After Release
bounce_mode = "down"

# QEMU
force_disable_qemu = False
_last_qemu_check = 0
_qemu_active = False

# Grab
global_keyboard_grabbed = False
_prev_keyboard_grabbed = None
forced_pause = False

# Logging (disimpan di memori, lalu ditampilkan di GUI)
input_events = []
bounce_events = []

repeat_rate = 30
repeat_delay = 0.3

last_press_time_per_key = {}
last_release_time_per_key = {}
keys_were_down_blocked = {}

last_valid_down_time_per_key = {}
valid_keys = {}
first_event_down_per_key = set()

# --------------------------------------------------------------------
# STATISTIK
# --------------------------------------------------------------------
stats_data = {}
# stats_data[key] = { "press": int, "bounce": int, "time_added": float }

stats_tree = None
custom_tree = None

def record_stats_press(key_name):
    """
    Tambah 'press' di stats, sekaligus set time_added jika key_name baru.
    (Fungsi ini nantinya hanya dipanggil di main thread.)
    """
    if key_name not in stats_data:
        stats_data[key_name] = {
            "press": 0,
            "bounce": 0,
            "time_added": time.time()
        }
    stats_data[key_name]["press"] += 1
    update_stats_row(key_name)

def record_stats_bounce(key_name):
    """
    Tambah 'bounce' di stats. (Hanya di main thread.)
    """
    if key_name not in stats_data:
        stats_data[key_name] = {
            "press": 0,
            "bounce": 0,
            "time_added": time.time()
        }
    stats_data[key_name]["bounce"] += 1
    update_stats_row(key_name)

# --------------------------------------------------------------------
# LOAD & SAVE CONFIG
# --------------------------------------------------------------------
def load_config():
    global bounce_time, current_language, custom_thresholds
    global shortcut_pause, shortcut_continue, force_disable_qemu
    global bounce_mode, repeat_rate, repeat_delay

    if os.path.exists(config_file):
        with open(config_file, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("threshold="):
                    try:
                        val = float(line.split("=")[1])
                        bounce_time = val / 1000.0
                    except:
                        pass
                elif line.startswith("language="):
                    current_language = line.split("=")[1].strip()
                elif line.startswith("custom_thresholds="):
                    try:
                        custom_thresholds = json.loads(line.split("=",1)[1])
                    except:
                        custom_thresholds = {}
                elif line.startswith("shortcut_pause="):
                    try:
                        shortcut_pause = normalize_key(json.loads(line.split("=",1)[1]))
                    except:
                        shortcut_pause = ""
                elif line.startswith("shortcut_continue="):
                    try:
                        shortcut_continue = normalize_key(json.loads(line.split("=",1)[1]))
                    except:
                        shortcut_continue = ""
                elif line.startswith("force_disable_qemu="):
                    try:
                        val = line.split("=",1)[1].strip().lower()
                        force_disable_qemu = (val == "true")
                    except:
                        force_disable_qemu = False
                elif line.startswith("debounce_mode="):
                    try:
                        bounce_mode = line.split("=",1)[1].strip().lower()
                    except:
                        bounce_mode = "down"
                elif line.startswith("hold_rate=") or line.startswith("repeat_rate="):
                    try:
                        val = float(line.split("=")[1])
                        repeat_rate = val
                    except:
                        repeat_rate = 30
                elif line.startswith("hold_delay=") or line.startswith("repeat_delay="):
                    try:
                        val = float(line.split("=")[1])
                        repeat_delay = val / 1000.0
                    except:
                        repeat_delay = 0.3

def save_config():
    with open(config_file, "w") as f:
        f.write(f"threshold={int(bounce_time*1000)}\n")
        f.write(f"language={current_language}\n")
        f.write(f"custom_thresholds={json.dumps(custom_thresholds)}\n")
        f.write(f"shortcut_pause={json.dumps(shortcut_pause)}\n")
        f.write(f"shortcut_continue={json.dumps(shortcut_continue)}\n")
        f.write(f"force_disable_qemu={str(force_disable_qemu)}\n")
        f.write(f"debounce_mode={bounce_mode}\n")
        f.write(f"repeat_rate={repeat_rate}\n")
        f.write(f"repeat_delay={int(repeat_delay*1000)}\n")

# --------------------------------------------------------------------
# LOAD LOG
# --------------------------------------------------------------------
def load_logs():
    global input_events, bounce_events
    if os.path.exists(log_input_file):
        with open(log_input_file, "r") as f:
            for line in f:
                try:
                    evt = json.loads(line)
                    input_events.append(evt)
                except:
                    pass
    if os.path.exists(log_bounce_file):
        with open(log_bounce_file, "r") as f:
            for line in f:
                try:
                    evt = json.loads(line)
                    bounce_events.append(evt)
                except:
                    pass

def rebuild_stats_from_logs():
    press_ids = {
        'inject_down_first_global',
        'inject_down_subsequent',
        'inject_down_custom'
    }
    bounce_ids = {'bounce_detected'}

    for evt in input_events:
        msg_id = evt.get('msg_id', '')
        placeholders = evt.get('placeholders', {})
        key_name = placeholders.get('key', 'Unknown')
        if msg_id in press_ids:
            record_stats_press(key_name)

    for evt in bounce_events:
        msg_id = evt.get('msg_id', '')
        placeholders = evt.get('placeholders', {})
        key_name = placeholders.get('key', 'Unknown')
        if msg_id in bounce_ids:
            record_stats_bounce(key_name)

def load_logs_all():
    load_logs()
    rebuild_stats_from_logs()

# --------------------------------------------------------------------
# UI TEKS
# --------------------------------------------------------------------
LANG_UI = {
    'id': {
        'window_title': "Keyboard Debounce (Per-Tombol)",
        'tab_stats': "Statistik",
        'tab_input_log': "Log Masukan",
        'tab_bounce': "Log Bounce",
        'tab_custom_threshold': "Batas Khusus",
        'tab_pause_bounce': "Jeda Debounce",
        'tab_advanced': "Pengaturan Lanjutan",
        'tab_about': "Tentang",
        'btn_apply': "Terapkan",
        'btn_id': "ID",
        'btn_en': "EN",
        'label_threshold': "Batas Global Bounce (ms):",
        'btn_detect': "Deteksi",
        'btn_save': "Simpan",
        'btn_delete': "Hapus",
        'label_pause_shortcut': "Tombol Pause:",
        'label_continue_shortcut': "Tombol Lanjut:",
        'btn_detect_pause': "Deteksi",
        'btn_detect_continue': "Deteksi",
        'btn_manual_pause': "Pause Manual",
        'btn_manual_continue': "Lanjut Manual",
        'chk_force_disable_qemu': "Paksa jeda debounce saat QEMU/KVM aktif (hindari konflik evdev).",
        'label_detection_mode': "Mode Deteksi:",
        'option_key_down': "Setelah Tekan",
        'option_key_up': "Setelah Lepas",
        'label_hold_rate': "Repeat Rate (keys/detik):",
        'label_hold_delay': "Delay sebelum repeat (ms):",
        'stats_col_key': "Tombol",
        'stats_col_press': "Jumlah Tekan",
        'stats_col_bounce': "Jumlah Bounce",
        'stats_col_percent': "% Bounce",
        # Sorting
        'sorting_label': "Urutkan:",
        'sorting_time_added_asc': "Waktu Ditambahkan (ASC)",
        'sorting_time_added_desc': "Waktu Ditambahkan (DESC)",
        'sorting_key_asc': "KEY (ASC)",
        'sorting_key_desc': "KEY (DESC)",
        'sorting_press_asc': "Jumlah Tekan (ASC)",
        'sorting_press_desc': "Jumlah Tekan (DESC)",
        'sorting_bounce_asc': "Jumlah Bounce (ASC)",
        'sorting_bounce_desc': "Jumlah Bounce (DESC)",
        'sorting_percent_asc': "% Bounce (ASC)",
        'sorting_percent_desc': "% Bounce (DESC)",
        # About Tab
        'about_title': "Keyboard Debounce v1.1.0 (Rilis Stabil)",
        'about_license': "Lisensi MIT",
        'about_copyright': "Hak Cipta (c) 2025 Firman"
    },
    'en': {
        'window_title': "Keyboard Debounce (Per-Key)",
        'tab_stats': "Statistics",
        'tab_input_log': "Input Log",
        'tab_bounce': "Bounce Log",
        'tab_custom_threshold': "Custom Threshold",
        'tab_pause_bounce': "Pause Debounce",
        'tab_advanced': "Advanced Settings",
        'tab_about': "About",
        'btn_apply': "Apply",
        'btn_id': "ID",
        'btn_en': "EN",
        'label_threshold': "Global Bounce Threshold (ms):",
        'btn_detect': "Detect",
        'btn_save': "Save",
        'btn_delete': "Delete",
        'label_pause_shortcut': "Pause Shortcut:",
        'label_continue_shortcut': "Continue Shortcut:",
        'btn_detect_pause': "Detect",
        'btn_detect_continue': "Detect",
        'btn_manual_pause': "Manual Pause",
        'btn_manual_continue': "Manual Continue",
        'chk_force_disable_qemu': "Force pause while QEMU/KVM is active (avoid evdev conflicts).",
        'label_detection_mode': "Detection Mode:",
        'option_key_down': "After Press",
        'option_key_up': "After Release",
        'label_hold_rate': "Repeat Rate (keys/sec):",
        'label_hold_delay': "Delay before repeat (ms):",
        'stats_col_key': "Key",
        'stats_col_press': "Press Count",
        'stats_col_bounce': "Bounce Count",
        'stats_col_percent': "% Bounce",
        # Sorting
        'sorting_label': "Sort By:",
        'sorting_time_added_asc': "Time Added (ASC)",
        'sorting_time_added_desc': "Time Added (DESC)",
        'sorting_key_asc': "Key (ASC)",
        'sorting_key_desc': "Key (DESC)",
        'sorting_press_asc': "Press Count (ASC)",
        'sorting_press_desc': "Press Count (DESC)",
        'sorting_bounce_asc': "Bounce Count (ASC)",
        'sorting_bounce_desc': "Bounce Count (DESC)",
        'sorting_percent_asc': "% Bounce (ASC)",
        'sorting_percent_desc': "% Bounce (DESC)",
        # About Tab
        'about_title': "Keyboard Debounce v1.1.0 (Stable Release)",
        'about_license': "MIT License",
        'about_copyright': "Copyright (c) 2025 Firman"
    }
}

# Pesan log / event
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
        'id': "Memproses tombol {key} DOWN",
        'en': "Processing {key} DOWN"
    },
    'inject_down_subsequent': {
        'id': "Memproses tombol {key} DOWN (delay: {delay} from previous press)",
        'en': "Processing {key} DOWN (delay: {delay} from previous press)"
    },
    'inject_up': {
        'id': "Memproses tombol {key} UP",
        'en': "Processing {key} UP"
    },
    'inject_down_custom': {
        'id': "Memproses tombol {key} DOWN (delay: {delay} from previous release)",
        'en': "Processing {key} DOWN (delay: {delay} from previous release)"
    },
    'inject_up_custom': {
        'id': "Memproses tombol {key} UP",
        'en': "Processing {key} UP"
    },
    'inject_down_error': {
        'id': "Terjadi kesalahan saat inject DOWN untuk {key}: {error}",
        'en': "Error injecting DOWN for {key}: {error}"
    },
    'inject_up_error': {
        'id': "Terjadi kesalahan saat inject UP untuk {key}: {error}",
        'en': "Error injecting UP for {key}: {error}"
    },
    'bounce_detected': {
        'id': "Bounce event difilter pada {key}: jeda {delay} (< {threshold} ms)",
        'en': "Bounce event filtered on {key}: delay {delay} (< {threshold} ms)"
    },
    'inject_down_pause': {
        'id': "Tombol {key} DOWN saat pause/akan pause",
        'en': "Pause key {key} DOWN"
    },
    'inject_up_pause': {
        'id': "Tombol {key} UP saat pause",
        'en': "Pause key {key} UP"
    },
    'pause_activated': {
        'id': "Bounce PAUSE diaktifkan dengan tombol {key}",
        'en': "Bounce PAUSE activated with key {key}"
    },
    'continue_activated': {
        'id': "Bounce dilanjutkan dengan tombol {key}",
        'en': "Bounce continued with key {key}"
    },
    'manual_pause': {
        'id': "Pause manual diterima: {key}",
        'en': "Manual pause activated: {key}"
    },
    'manual_continue': {
        'id': "Lanjut manual diterima: {key}",
        'en': "Manual continue activated: {key}"
    },
    'grab_enabled': {
        'id': "Keyboard grab telah diaktifkan kembali",
        'en': "Keyboard grab re-enabled"
    },
    'force_disable_qemu': {
        'id': "Keyboard grab dihentikan paksa karena QEMU/KVM aktif",
        'en': "Keyboard grab forcibly disabled due to QEMU/KVM active"
    },
    'grab_failed_qemu_active': {
        'id': "Gagal melakukan grab keyboard karena QEMU/KVM masih aktif",
        'en': "Failed to grab keyboard because QEMU/KVM is still active"
    },
    'detection_mode_changed': {
        'id': "Mode deteksi bounce diubah menjadi {mode}",
        'en': "Bounce detection mode changed to {mode}"
    },
    'custom_threshold_added': {
        'id': "Custom threshold untuk {key} ditambahkan dengan nilai {threshold} ms",
        'en': "Custom threshold for {key} added with value {threshold} ms"
    },
    'custom_threshold_deleted': {
        'id': "Custom threshold untuk {key} dihapus",
        'en': "Custom threshold for {key} deleted"
    },
    'threshold_update': {
        'id': "Batas bounce diubah menjadi {threshold} ms",
        'en': "Global Bounce Threshold updated to {threshold} ms"
    },
    'hold_settings_update': {
        'id': "Pengaturan repeat diubah menjadi: Rate={rate} keys/detik, Delay={delay} ms",
        'en': "Repeat settings updated to: Rate={rate} keys/sec, Delay={delay} ms"
    }
}

# --------------------------------------------------------------------
# LAINNYA: SOUND, KEY NORMALIZE, ETC
# --------------------------------------------------------------------
def play_sound(filename):
    if not sound_enabled:
        return
    sound_path = os.path.join(script_dir, filename)
    if not os.path.exists(sound_path):
        possible_sounds_dir = os.path.join(script_dir, "sounds")
        alt_path = os.path.join(possible_sounds_dir, filename)
        if os.path.exists(alt_path):
            sound_path = alt_path
    try:
        snd = pygame.mixer.Sound(sound_path)
        snd.play()
    except Exception as e:
        print(f"Error playing sound {filename}: {e}")

def normalize_key(key):
    mapping = {
        "KEY_CONTROL_L": "KEY_LEFTCTRL",
        "KEY_CONTROL_R": "KEY_RIGHTCTRL",
        "KEY_ALT_L": "KEY_LEFTALT",
        "KEY_ALT_R": "KEY_RIGHTALT",
        "KEY_SHIFT_L": "KEY_LEFTSHIFT",
        "KEY_SHIFT_R": "KEY_RIGHTSHIFT",
        "KEY_SCROLL_LOCK": "KEY_SCROLLLOCK"
    }
    return mapping.get(key, key)

def is_modifier(key_name):
    modifiers = {
        "KEY_LEFTSHIFT", "KEY_RIGHTSHIFT",
        "KEY_LEFTCTRL", "KEY_RIGHTCTRL",
        "KEY_LEFTALT", "KEY_RIGHTALT",
        "KEY_LEFTMETA", "KEY_RIGHTMETA",
        "KEY_CAPSLOCK", "KEY_NUMLOCK", "KEY_SCROLLLOCK"
    }
    return key_name in modifiers

# --------------------------------------------------------------------
# CREATE UINPUT
# --------------------------------------------------------------------
def create_uinput_device():
    available_keys = [getattr(uinput, attr) for attr in dir(uinput) if attr.startswith("KEY_")]
    return uinput.Device(available_keys, name="Virtual Keyboard")

uinput_device = create_uinput_device()

# --------------------------------------------------------------------
# LOGGING & RENDER
# --------------------------------------------------------------------
def translate_event(evt):
    """
    Memformat event menjadi string tampilan log.
    """
    lang = current_language
    msg_id = evt['msg_id']
    placeholders = evt.get('placeholders', {})

    # --- Normalisasi agar tidak double "ms", dan agar tidak int(None) ---
    # delay
    if 'delay' in placeholders:
        old_val = placeholders['delay']
        if old_val is not None:
            if isinstance(old_val, str) and old_val.endswith(" ms"):
                old_val = old_val[:-3]
            try:
                numeric_val = int(old_val)
                placeholders['delay'] = f"{numeric_val} ms"
            except:
                placeholders['delay'] = str(old_val)
        else:
            placeholders['delay'] = ""

    # threshold
    if 'threshold' in placeholders:
        old_thr = placeholders['threshold']
        if old_thr is not None:
            if isinstance(old_thr, str) and old_thr.endswith(" ms"):
                old_thr = old_thr[:-3]
            try:
                numeric_thr = int(old_thr)
                placeholders['threshold'] = str(numeric_thr)
            except:
                placeholders['threshold'] = str(old_thr)
        else:
            placeholders['threshold'] = ""

    template = LANG_MSG.get(msg_id, {}).get(lang, "[UNDEFINED MSG_ID]")
    rendered_text = template.format(**placeholders)

    # Hapus substring jika delay kosong
    if 'delay' in placeholders:
        if placeholders['delay'].strip() == "":
            rendered_text = rendered_text.replace("(delay:  from previous release)", "")
            rendered_text = rendered_text.replace("(delay:  from previous press)", "")
            rendered_text = " ".join(rendered_text.split())

    ts_struct = time.localtime(evt['timestamp'])
    time_str = time.strftime("%H:%M:%S", ts_struct)
    return f"[{time_str}] {rendered_text}"

def add_input_event(evt_dict):
    """
    Tambahkan event ke input_events (list) + tampilkan di GUI jika ada + tulis ke file log_input.
    """
    input_events.append(evt_dict)
    if use_gui:
        if input_text:
            input_text.configure(state='normal')
            line = translate_event(evt_dict) + "\n"
            input_text.insert(tk.END, line)
            input_text.configure(state='disabled')
            input_text.yview_moveto(1.0)

    # Tetap tulis ke file, baik GUI maupun non-GUI
    with open(log_input_file, "a") as f:
        f.write(json.dumps(evt_dict) + "\n")

def add_bounce_event(evt_dict):
    """
    Tambahkan event ke bounce_events (list) + tampilkan di GUI jika ada + tulis ke file log_bounce.
    """
    bounce_events.append(evt_dict)
    if use_gui:
        if bounce_text:
            bounce_text.configure(state='normal')
            line = translate_event(evt_dict) + "\n"
            bounce_text.insert(tk.END, line)
            bounce_text.configure(state='disabled')
            bounce_text.yview_moveto(1.0)

    # Tetap tulis ke file, baik GUI maupun non-GUI
    with open(log_bounce_file, "a") as f:
        f.write(json.dumps(evt_dict) + "\n")

# --------------------------------------------------------------------
# Fungsi-fungsi "enqueue" (dipanggil dari thread pemantau)
# --------------------------------------------------------------------
event_queue = queue.Queue()

def queue_input_event(msg_id, placeholders=None):
    if placeholders is None:
        placeholders = {}
    evt = {
        'timestamp': time.time(),
        'msg_id': msg_id,
        'placeholders': placeholders
    }
    event_queue.put(("input_event", evt))

def queue_bounce_event(msg_id, placeholders=None):
    if placeholders is None:
        placeholders = {}
    evt = {
        'timestamp': time.time(),
        'msg_id': msg_id,
        'placeholders': placeholders
    }
    event_queue.put(("bounce_event", evt))

def queue_stats_press(key_name):
    event_queue.put(("stats_press", key_name))

def queue_stats_bounce(key_name):
    event_queue.put(("stats_bounce", key_name))

# --------------------------------------------------------------------
# Proses event_queue di main thread
# --------------------------------------------------------------------
_stats_update_scheduled = False

def schedule_stats_render():
    """
    Agar tidak memanggil render_stats_table() setiap event (bisa berat),
    kita jadwalkan rendering secara terjadwal.
    """
    global _stats_update_scheduled
    if not _stats_update_scheduled:
        _stats_update_scheduled = True
        if use_gui:
            root.after(200, do_render_stats_table)

def do_render_stats_table():
    global _stats_update_scheduled
    _stats_update_scheduled = False
    render_stats_table()

def process_event_queue():
    """
    Ambil semua item di event_queue, proses (update log, stats, dll.).
    Pada mode GUI, fungsi ini akan dipanggil berulang via root.after().
    Pada mode --nogui, kita panggil manual dalam loop.
    """
    while True:
        try:
            item_type, data = event_queue.get_nowait()
        except queue.Empty:
            break

        if item_type == "input_event":
            add_input_event(data)
            schedule_stats_render()
        elif item_type == "bounce_event":
            add_bounce_event(data)
            schedule_stats_render()
        elif item_type == "stats_press":
            record_stats_press(data)
            schedule_stats_render()
        elif item_type == "stats_bounce":
            record_stats_bounce(data)
            schedule_stats_render()

    if use_gui:
        # Dijadwalkan loop lagi dalam GUI
        root.after(50, process_event_queue)

# --------------------------------------------------------------------
# render_all_logs & update UI (setelah load)
# --------------------------------------------------------------------
def render_all_logs():
    if use_gui:
        if input_text:
            input_text.configure(state='normal')
            input_text.delete("1.0", tk.END)
            for evt in input_events:
                input_text.insert(tk.END, translate_event(evt) + "\n")
            input_text.configure(state='disabled')
            input_text.yview_moveto(1.0)

        if bounce_text:
            bounce_text.configure(state='normal')
            bounce_text.delete("1.0", tk.END)
            for evt in bounce_events:
                bounce_text.insert(tk.END, translate_event(evt) + "\n")
            bounce_text.configure(state='disabled')
            bounce_text.yview_moveto(1.0)
    update_ui_language()

def update_stats_row(key):
    if use_gui:
        pass  # cukup panggil schedule_stats_render() nanti

# --------------------------------------------------------------------
# REPEAT THREAD
# --------------------------------------------------------------------
repeat_threads = {}

def repeat_thread_func(key, stop_event):
    time.sleep(repeat_delay)
    interval = 1.0 / repeat_rate
    while not stop_event.is_set():
        try:
            uinput_device.emit(getattr(uinput, key), 1)
            uinput_device.emit(getattr(uinput, key), 0)
        except Exception as e:
            queue_input_event('inject_down_error', {'key': key, 'error': str(e)})
        time.sleep(interval)

def start_repeat(key):
    if is_modifier(key):
        return
    if key not in repeat_threads:
        stop_event = threading.Event()
        t = threading.Thread(target=repeat_thread_func, args=(key, stop_event), daemon=True)
        repeat_threads[key] = (t, stop_event)
        t.start()

def stop_repeat(key):
    if key in repeat_threads:
        t, stop_event = repeat_threads[key]
        stop_event.set()
        del repeat_threads[key]

# --------------------------------------------------------------------
# CUSTOM THRESHOLD
# --------------------------------------------------------------------
def save_custom_thresholds():
    save_config()

def update_custom_threshold(key, threshold_ms):
    try:
        threshold = float(threshold_ms) / 1000.0
        custom_thresholds[key] = threshold
        save_custom_thresholds()
        render_custom_thresholds()
        queue_input_event('custom_threshold_added', {'key': key, 'threshold': int(threshold_ms)})
    except Exception as e:
        print(f"Error updating custom threshold: {e}")

def render_custom_thresholds():
    if use_gui and custom_tree:
        headers = ("Tombol", "Batas (ms)") if current_language=='id' else ("Key", "Threshold (ms)")
        custom_tree["columns"] = headers
        for col in headers:
            custom_tree.heading(col, text=col)
        total_width = custom_tree.winfo_width()
        if total_width <= 0:
            total_width = 240
        col_width = int(total_width * 0.99) // len(headers)
        for col in headers:
            custom_tree.column(col, width=col_width, minwidth=col_width, anchor="center", stretch=True)
        for item in custom_tree.get_children():
            custom_tree.delete(item)
        for kk, thr in custom_thresholds.items():
            custom_tree.insert("", tk.END, iid=kk, values=(kk, int(thr*1000)))

def delete_custom_threshold():
    if not use_gui or not custom_tree:
        return
    selected = custom_tree.selection()
    if selected:
        key = selected[0]
        if key in custom_thresholds:
            del custom_thresholds[key]
            save_custom_thresholds()
            render_custom_thresholds()
            queue_input_event('custom_threshold_deleted', {'key': key})

def update_custom_threshold_gui():
    key = custom_key_entry.get().strip()
    thr = custom_threshold_entry.get().strip()
    if key and thr:
        update_custom_threshold(key, float(thr))
        custom_key_entry.config(state='readonly')
        custom_key_entry.delete(0, tk.END)
        custom_threshold_entry.delete(0, tk.END)

def detect_custom_key():
    custom_key_entry.config(state='normal')
    custom_key_entry.delete(0, tk.END)
    placeholder = "TEKAN TOMBOL" if current_language=='id' else "PRESS KEY"
    custom_key_entry.insert(0, placeholder)
    custom_key_entry.focus_force()
    custom_key_entry.bind("<FocusOut>", lambda event: custom_key_entry.delete(0, tk.END))

    def on_key(event):
        detected = "KEY_" + event.keysym.upper()
        normalized = normalize_key(detected)
        custom_key_entry.delete(0, tk.END)
        custom_key_entry.insert(0, normalized)
        custom_key_entry.config(state='readonly')
        custom_key_entry.unbind("<Key>")
        custom_key_entry.unbind("<FocusOut>")

    custom_key_entry.bind("<Key>", on_key)

# --------------------------------------------------------------------
# PAUSE BOUNCE
# --------------------------------------------------------------------
def update_pause_shortcuts(pause_key, continue_key):
    global shortcut_pause, shortcut_continue
    shortcut_pause = normalize_key(pause_key)
    shortcut_continue = normalize_key(continue_key)
    save_config()
    update_ui_language()

def detect_pause_key(entry):
    entry.config(state='normal')
    entry.delete(0, tk.END)
    placeholder = "TEKAN TOMBOL" if current_language=='id' else "PRESS KEY"
    entry.insert(0, placeholder)
    entry.focus_force()
    entry.bind("<FocusOut>", lambda event: entry.delete(0, tk.END))

    def on_key(event):
        detected = "KEY_" + event.keysym.upper()
        normalized = normalize_key(detected)
        entry.delete(0, tk.END)
        entry.insert(0, normalized)
        entry.config(state='readonly')
        entry.unbind("<Key>")
        entry.unbind("<FocusOut>")

    entry.bind("<Key>", on_key)

def manual_pause_action():
    global paused, global_keyboard_grabbed, forced_pause
    if not paused:
        paused = True
        forced_pause = False
        global_keyboard_grabbed = False
        play_sound("pause.wav")
        queue_input_event('manual_pause', {'key': shortcut_pause if shortcut_pause else "KEY_PAUSE"})

def manual_continue_action():
    global paused, global_keyboard_grabbed
    if paused:
        paused = False
        if not is_qemu_kvm_active():
            global_keyboard_grabbed = True
            play_sound("continue.wav")
        else:
            global_keyboard_grabbed = False
            play_sound("pause.wav")
        queue_input_event('manual_continue', {'key': shortcut_continue if shortcut_continue else "KEY_SCROLLLOCK"})

# --------------------------------------------------------------------
# ADVANCED SETTINGS
# --------------------------------------------------------------------
def update_force_disable_qemu():
    global force_disable_qemu
    force_disable_qemu = force_disable_qemu_var.get()
    save_config()

def apply_repeat_settings():
    global repeat_rate, repeat_delay
    try:
        new_rate = float(entry_hold_rate.get())
        new_delay_ms = float(entry_hold_delay.get())
        repeat_rate = new_rate if new_rate > 0 else 30
        repeat_delay = (new_delay_ms if new_delay_ms > 0 else 300) / 1000.0
        save_config()
        queue_input_event('hold_settings_update', {
            'rate': repeat_rate,
            'delay': int(repeat_delay*1000)
        })
    except:
        pass

# --------------------------------------------------------------------
# STATISTICS SORTING
# --------------------------------------------------------------------
stats_sort_mode = "time_added_asc"

def calc_stats_percent(press, bounce):
    if press == 0:
        return 0.0
    return (bounce / press) * 100.0

def parse_sort_mode(mode_str):
    parts = mode_str.split("_")
    if parts[0] == "time":
        col = "time"
    elif parts[0] == "key":
        col = "key"
    elif parts[0] == "press":
        col = "press"
    elif parts[0] == "bounce":
        col = "bounce"
    elif parts[0] == "percent":
        col = "percent"
    else:
        col = "time"
    rev = (parts[-1] == "desc")
    return col, rev

def on_stats_sort_changed(event=None):
    global stats_sort_mode
    stats_sort_mode = stats_sort_combobox.get()
    render_stats_table()

def render_stats_table(preserve_scroll=False):
    if not use_gui or not stats_tree:
        return

    if preserve_scroll:
        yview = stats_tree.yview()
    else:
        yview = (0.0,)

    for item in stats_tree.get_children():
        stats_tree.delete(item)

    data_rows = []
    for k, v in stats_data.items():
        press = v["press"]
        bnc = v["bounce"]
        pct = calc_stats_percent(press, bnc)
        tadd = v.get("time_added", 0.0)
        data_rows.append((k, press, bnc, pct, tadd))

    col_key, reverse = parse_sort_mode(convert_sort_text_to_key(stats_sort_mode))
    if col_key == "time":
        data_rows.sort(key=lambda x: x[4], reverse=reverse)
    elif col_key == "key":
        data_rows.sort(key=lambda x: x[0], reverse=reverse)
    elif col_key == "press":
        data_rows.sort(key=lambda x: x[1], reverse=reverse)
    elif col_key == "bounce":
        data_rows.sort(key=lambda x: x[2], reverse=reverse)
    elif col_key == "percent":
        data_rows.sort(key=lambda x: x[3], reverse=reverse)

    for row in data_rows:
        key_str = row[0]
        press_val = row[1]
        bounce_val = row[2]
        pct_val = f"{row[3]:.1f}%"
        stats_tree.insert("", tk.END, iid=key_str, values=(key_str, press_val, bounce_val, pct_val))

    stats_tree.yview_moveto(yview[0])

def convert_sort_text_to_key(text):
    mapping_text_to_key = {
        # ID
        "Waktu Ditambahkan (ASC)": "time_added_asc",
        "Waktu Ditambahkan (DESC)": "time_added_desc",
        "KEY (ASC)": "key_asc",
        "KEY (DESC)": "key_desc",
        "Jumlah Tekan (ASC)": "press_asc",
        "Jumlah Tekan (DESC)": "press_desc",
        "Jumlah Bounce (ASC)": "bounce_asc",
        "Jumlah Bounce (DESC)": "bounce_desc",
        "% Bounce (ASC)": "percent_asc",
        "% Bounce (DESC)": "percent_desc",
        # EN
        "Time Added (ASC)": "time_added_asc",
        "Time Added (DESC)": "time_added_desc",
        "Key (ASC)": "key_asc",
        "Key (DESC)": "key_desc",
        "Press Count (ASC)": "press_asc",
        "Press Count (DESC)": "press_desc",
        "Bounce Count (ASC)": "bounce_asc",
        "Bounce Count (DESC)": "bounce_desc",
        "% Bounce (ASC)": "percent_asc",
        "% Bounce (DESC)": "percent_desc",
    }
    return mapping_text_to_key.get(text, "time_added_asc")

# --------------------------------------------------------------------
# TAB / UI BINDING
# --------------------------------------------------------------------
def adjust_treeview_columns_on_tab_change():
    if use_gui:
        # custom
        if custom_tree:
            total_width_custom = custom_tree.winfo_width()
            cols_custom = custom_tree["columns"]
            if cols_custom and total_width_custom > 0:
                col_width = int(total_width_custom * 0.99) // len(cols_custom)
                for col in cols_custom:
                    custom_tree.column(col, width=col_width, minwidth=col_width)

        # stats
        if stats_tree:
            total_width_stats = stats_tree.winfo_width()
            if total_width_stats > 0:
                stats_cols = stats_tree["columns"]
                col_width_stats = int(total_width_stats * 0.99) // len(stats_cols)
                for col in stats_cols:
                    stats_tree.column(col, width=col_width_stats, minwidth=col_width_stats)

def on_tab_changed(event):
    adjust_treeview_columns_on_tab_change()
    try:
        custom_key_entry.config(state='normal')
        if custom_key_entry.get() in ["PRESS KEY", "TEKAN TOMBOL"]:
            custom_key_entry.delete(0, tk.END)
        custom_key_entry.config(state='readonly')
        if pause_entry_global:
            pause_entry_global.config(state='normal')
            pause_entry_global.delete(0, tk.END)
            if shortcut_pause:
                pause_entry_global.insert(0, shortcut_pause)
            pause_entry_global.config(state='readonly')
        if continue_entry_global:
            continue_entry_global.config(state='normal')
            continue_entry_global.delete(0, tk.END)
            if shortcut_continue:
                continue_entry_global.insert(0, shortcut_continue)
            continue_entry_global.config(state='readonly')
    except Exception:
        pass

def update_ui_language():
    lang_ui = LANG_UI[current_language]
    if use_gui:
        root.title(lang_ui['window_title'])
        bounce_label.configure(text=lang_ui['label_threshold'])
        apply_button.configure(text=lang_ui['btn_apply'])
        lang_button_id.configure(text=lang_ui['btn_id'])
        lang_button_en.configure(text=lang_ui['btn_en'])
        notebook.tab(tab_stats, text=lang_ui['tab_stats'])
        notebook.tab(tab1, text=lang_ui['tab_input_log'])
        notebook.tab(tab2, text=lang_ui['tab_bounce'])
        notebook.tab(tab3, text=lang_ui.get('tab_custom_threshold', "Custom Threshold"))
        notebook.tab(tab5, text=lang_ui.get('tab_pause_bounce', "Pause Debounce"))
        notebook.tab(tab6, text=lang_ui.get('tab_advanced', "Advanced Settings"))
        notebook.tab(tab7, text=lang_ui.get('tab_about', "About"))

        custom_label_key.configure(text=("Tombol:" if current_language=='id' else "Key:"))
        custom_label_thr.configure(text=("Batas (ms):" if current_language=='id' else "Threshold (ms):"))
        detect_button.configure(text=lang_ui.get('btn_detect', "Detect"))
        custom_save_button.configure(text=lang_ui.get('btn_save', "Save"))
        delete_button.configure(text=lang_ui.get('btn_delete', "Delete"))
        update_ui_pause_tab()

        # Stats header
        stats_cols = (
            lang_ui['stats_col_key'],
            lang_ui['stats_col_press'],
            lang_ui['stats_col_bounce'],
            lang_ui['stats_col_percent']
        )
        stats_tree["columns"] = stats_cols
        for col in stats_cols:
            stats_tree.heading(col, text=col)

        # Sorting label
        sorting_label.configure(text=lang_ui.get('sorting_label', "Sort By:"))
        sort_items = [
            lang_ui.get('sorting_time_added_asc', "Time Added (ASC)"),
            lang_ui.get('sorting_time_added_desc', "Time Added (DESC)"),
            lang_ui.get('sorting_key_asc', "Key (ASC)"),
            lang_ui.get('sorting_key_desc', "Key (DESC)"),
            lang_ui.get('sorting_press_asc', "Press Count (ASC)"),
            lang_ui.get('sorting_press_desc', "Press Count (DESC)"),
            lang_ui.get('sorting_bounce_asc', "Bounce Count (ASC)"),
            lang_ui.get('sorting_bounce_desc', "Bounce Count (DESC)"),
            lang_ui.get('sorting_percent_asc', "% Bounce (ASC)"),
            lang_ui.get('sorting_percent_desc', "% Bounce (DESC)")
        ]
        stats_sort_combobox.config(values=sort_items)

        # About Tab
        about_title_label.config(text=lang_ui.get('about_title', "Keyboard Debounce v1.1.0"))
        about_license_label.config(text=lang_ui.get('about_license', "MIT License"))
        about_copyright_label.config(
            text=lang_ui.get('about_copyright', "Copyright (c) 2025")
        )
        about_title_label.configure(anchor="w", justify="left")
        about_license_label.configure(anchor="w", justify="left")
        about_copyright_label.configure(anchor="w", justify="left")

        license_label.configure(anchor="w", justify="left")

        render_stats_table()

def update_ui_pause_tab():
    lang_ui = LANG_UI[current_language]
    if pause_entry_global:
        pause_entry_global.config(state='normal')
        pause_entry_global.delete(0, tk.END)
        if shortcut_pause:
            pause_entry_global.insert(0, shortcut_pause)
        pause_entry_global.config(state='readonly')
    if continue_entry_global:
        continue_entry_global.config(state='normal')
        continue_entry_global.delete(0, tk.END)
        if shortcut_continue:
            continue_entry_global.insert(0, shortcut_continue)
        continue_entry_global.config(state='readonly')

    if btn_manual_pause_global:
        btn_manual_pause_global.configure(text=lang_ui.get('btn_manual_pause', "Manual Pause"))
    if btn_manual_continue_global:
        btn_manual_continue_global.configure(text=lang_ui.get('btn_manual_continue', "Manual Continue"))
    if btn_detect_pause_global:
        btn_detect_pause_global.configure(text=lang_ui.get('btn_detect_pause', "Detect"))
    if btn_detect_continue_global:
        btn_detect_continue_global.configure(text=lang_ui.get('btn_detect_continue', "Detect"))
    if btn_save_shortcut_global:
        btn_save_shortcut_global.configure(text=lang_ui.get('btn_save', "Save"))

def switch_language_to(lang):
    global current_language
    current_language = lang
    save_config()
    render_all_logs()
    if use_gui:
        pause_entry_global.config(state='normal')
        pause_entry_global.delete(0, tk.END)
        if shortcut_pause:
            pause_entry_global.insert(0, shortcut_pause)
        pause_entry_global.config(state='readonly')
        continue_entry_global.config(state='normal')
        continue_entry_global.delete(0, tk.END)
        if shortcut_continue:
            continue_entry_global.insert(0, shortcut_continue)
        continue_entry_global.config(state='readonly')
        os.execv(sys.executable, [sys.executable] + sys.argv)

def find_keyboard_device():
    devices = [InputDevice(path) for path in list_devices()]
    for dev in devices:
        caps = dev.capabilities()
        if ecodes.EV_KEY in caps and ecodes.KEY_A in caps[ecodes.EV_KEY]:
            return dev
    return None

def is_qemu_kvm_active():
    try:
        output = subprocess.check_output(["pgrep", "-f", "qemu-system"], universal_newlines=True)
        return bool(output.strip())
    except subprocess.CalledProcessError:
        return False

# --------------------------------------------------------------------
# MONITOR KEYBOARD (thread)
# --------------------------------------------------------------------
def monitor_keyboard():
    global paused, forced_pause, global_keyboard_grabbed
    global _last_qemu_check, _qemu_active

    dev = find_keyboard_device()
    if dev is None:
        queue_input_event('no_device')
        return

    grabbed_by_app = False
    try:
        if not (force_disable_qemu and is_qemu_kvm_active()):
            dev.grab()
            grabbed_by_app = True
        else:
            queue_input_event('force_disable_qemu', {'note': 'Initial: QEMU/KVM aktif, tidak melakukan grab'})
        queue_input_event('device_found', {'device_name': dev.name, 'device_path': dev.path})
    except Exception:
        grabbed_by_app = False

    global_keyboard_grabbed = grabbed_by_app
    _last_qemu_check = time.time()

    forced_pause = False
    if force_disable_qemu and is_qemu_kvm_active():
        paused = True
        forced_pause = True

    while True:
        now = time.time()
        if now - _last_qemu_check > 0.5:
            _qemu_active = is_qemu_kvm_active() if force_disable_qemu else False
            _last_qemu_check = now
            if _qemu_active:
                paused = True
                forced_pause = True
                if grabbed_by_app:
                    try:
                        dev.ungrab()
                        grabbed_by_app = False
                        global_keyboard_grabbed = False
                        queue_input_event('force_disable_qemu', {'note': 'QEMU aktif, keyboard ungrab otomatis'})
                    except Exception as e:
                        queue_input_event('inject_down_error', {'key': 'N/A', 'error': f"ungrab error: {str(e)}"})
            else:
                if force_disable_qemu and forced_pause:
                    forced_pause = False
                    paused = False
                    if not grabbed_by_app:
                        try:
                            dev.grab()
                            grabbed_by_app = True
                            global_keyboard_grabbed = True
                            play_sound("continue.wav")
                            queue_input_event('grab_enabled', {'note': 'Force mode: QEMU nonaktif, keyboard grabbed otomatis'})
                            queue_input_event('continue_activated', {'key': 'AUTO_FORCE'})
                        except Exception as e:
                            queue_input_event('grab_failed_qemu_active', {'error': str(e)})

        if _qemu_active:
            if grabbed_by_app:
                try:
                    dev.ungrab()
                    grabbed_by_app = False
                    global_keyboard_grabbed = False
                except Exception as e:
                    queue_input_event('inject_down_error', {'key': 'N/A', 'error': f"ungrab error: {str(e)}"})
        else:
            if not paused and not grabbed_by_app:
                try:
                    dev.grab()
                    grabbed_by_app = True
                    global_keyboard_grabbed = True
                except:
                    pass

        event = dev.read_one()
        if event is None:
            time.sleep(0.01)
            continue

        if event.type != ecodes.EV_KEY:
            continue

        key_event = categorize(event)
        raw_key = key_event.keycode if isinstance(key_event.keycode, str) else key_event.keycode[0]
        norm_key = normalize_key(raw_key)
        keystate = key_event.keystate
        now = time.time()

        # Shortcut pause/continue
        if keystate == 1:  # Key down
            if shortcut_pause and norm_key == shortcut_pause and not paused:
                if force_disable_qemu and is_qemu_kvm_active():
                    continue
                paused = True
                forced_pause = False
                queue_input_event('inject_down_pause', {'key': norm_key})
                try:
                    dev.ungrab()
                    grabbed_by_app = False
                    global_keyboard_grabbed = False
                except:
                    pass
                play_sound("pause.wav")
                queue_input_event('pause_activated', {'key': norm_key})
                continue

            if shortcut_continue and norm_key == shortcut_continue and paused:
                if force_disable_qemu and is_qemu_kvm_active():
                    play_sound("pause.wav")
                    queue_input_event('grab_failed_qemu_active', {'error': 'QEMU/KVM active'})
                    continue
                paused = False
                forced_pause = False
                try:
                    if not is_qemu_kvm_active():
                        dev.grab()
                        grabbed_by_app = True
                        global_keyboard_grabbed = True
                        play_sound("continue.wav")
                        queue_input_event('grab_enabled', {'note': 'Grab re-enabled'})
                        queue_input_event('continue_activated', {'key': norm_key})
                    else:
                        play_sound("pause.wav")
                        queue_input_event('grab_failed_qemu_active', {'error': 'QEMU/KVM active'})
                except Exception as e:
                    queue_input_event('grab_failed_qemu_active', {'error': str(e)})
                continue

        if paused:
            if not (force_disable_qemu and _qemu_active):
                if keystate == 1:
                    queue_input_event('inject_down_pause', {'key': norm_key, 'note': 'Sedang Pause'})
                elif keystate == 0:
                    queue_input_event('inject_up_pause', {'key': norm_key, 'note': 'Sedang Pause'})
            continue

        # MAIN BOUNCE CHECK
        if bounce_mode == "up":  # After Release
            if keystate == 1:
                threshold_used = custom_thresholds.get(norm_key, bounce_time)
                last_rel = last_release_time_per_key.get(norm_key, 0.0)
                elapsed = now - last_rel
                if elapsed < threshold_used:
                    keys_were_down_blocked[norm_key] = True
                    queue_stats_bounce(norm_key)
                    queue_bounce_event('bounce_detected', {
                        'key': norm_key,
                        'delay': int(elapsed*1000),
                        'threshold': int(threshold_used*1000)
                    })
                    stop_repeat(norm_key)
                else:
                    keys_were_down_blocked[norm_key] = False
                    last_press_time_per_key[norm_key] = now
                    delay_val = None
                    if norm_key in last_release_time_per_key and last_release_time_per_key[norm_key] != 0.0:
                        delay_val = int(elapsed*1000)
                    queue_input_event('inject_down_custom', {
                        'key': norm_key,
                        'delay': delay_val
                    })
                    queue_stats_press(norm_key)
                    try:
                        uinput_device.emit(getattr(uinput, norm_key), 1)
                    except Exception as e:
                        queue_input_event('inject_down_error', {'key': norm_key, 'error': str(e)})
                    start_repeat(norm_key)
            elif keystate == 2:
                pass
            else:  # up
                stop_repeat(norm_key)
                blocked = keys_were_down_blocked.get(norm_key, False)
                if blocked:
                    keys_were_down_blocked[norm_key] = False
                else:
                    queue_input_event('inject_up_custom', {'key': norm_key})
                    try:
                        uinput_device.emit(getattr(uinput, norm_key), 0)
                    except Exception as e:
                        queue_input_event('inject_up_error', {'key': norm_key, 'error': str(e)})
                    last_release_time_per_key[norm_key] = now
        else:  # bounce_mode == "down"
            if keystate == 1:
                threshold_used = custom_thresholds.get(norm_key, bounce_time)
                last_time = last_valid_down_time_per_key.get(norm_key, None)
                now_ts = now
                start_repeat(norm_key)
                if last_time is None or (now_ts - last_time >= threshold_used):
                    last_valid_down_time_per_key[norm_key] = now_ts
                    if norm_key not in first_event_down_per_key:
                        queue_input_event('inject_down_first_global', {'key': norm_key})
                        first_event_down_per_key.add(norm_key)
                    else:
                        delay_ms = int((now_ts - last_time)*1000) if last_time else 0
                        queue_input_event('inject_down_subsequent', {'key': norm_key, 'delay': delay_ms})
                    queue_stats_press(norm_key)
                    try:
                        uinput_device.emit(getattr(uinput, norm_key), 1)
                    except Exception as e:
                        queue_input_event('inject_down_error', {'key': norm_key, 'error': str(e)})
                    valid_keys[norm_key] = now_ts
                else:
                    queue_stats_bounce(norm_key)
                    queue_bounce_event('bounce_detected', {
                        'key': norm_key,
                        'delay': int((now_ts - last_time)*1000) if last_time else 0,
                        'threshold': int(threshold_used*1000)
                    })
            elif keystate == 2:
                pass
            else:
                stop_repeat(norm_key)
                if norm_key in valid_keys:
                    try:
                        uinput_device.emit(getattr(uinput, norm_key), 0)
                    except Exception as e:
                        queue_input_event('inject_up_error', {'key': norm_key, 'error': str(e)})
                    del valid_keys[norm_key]
                    queue_input_event('inject_up', {'key': norm_key})

def update_bounce_threshold():
    global bounce_time
    try:
        val_ms = float(bounce_entry.get())
        bounce_time = val_ms / 1000.0
        save_config()
        queue_input_event('threshold_update', {'threshold': int(val_ms)})
    except ValueError:
        pass

# --------------------------------------------------------------------
# RUN_BACKGROUND TANPA GUI
# --------------------------------------------------------------------
def run_background():
    """
    Jalankan program tanpa GUI, namun tetap memproses event_queue agar log ditulis.
    """
    load_config()
    load_logs_all()

    # Jalankan pemantau keyboard di thread terpisah
    monitor_thread = threading.Thread(target=monitor_keyboard, daemon=True)
    monitor_thread.start()

    # Loop utama: proses event_queue supaya log tersimpan
    while True:
        process_event_queue()
        time.sleep(0.05)

# --------------------------------------------------------------------
# GUI atau NO-GUI
# --------------------------------------------------------------------
use_gui = ("--nogui" not in sys.argv)
pause_entry_global = None
continue_entry_global = None
btn_manual_pause_global = None
btn_manual_continue_global = None
btn_detect_pause_global = None
btn_detect_continue_global = None
btn_save_shortcut_global = None
input_text = None
bounce_text = None

if use_gui:
    load_config()
    load_logs_all()

    root = ThemedTk(theme="arc", className="Keyboard Debounce")

    root.geometry("900x600")
    root.minsize(900,600)

    style = ttk.Style(root)
    style.layout("TNotebook.Tab",
        [
            ('Notebook.tab', {'sticky': 'nswe', 'children':
                [('Notebook.padding', {'side': 'top', 'sticky': 'nswe', 'children':
                    [('Notebook.label', {'side': 'top', 'sticky': ''})],
                })],
            })
        ]
    )

    lang_ui = LANG_UI[current_language]
    root.title(lang_ui['window_title'])

    top_frame = ttk.Frame(root, padding="10")
    top_frame.pack(fill=tk.X)

    bounce_label = ttk.Label(top_frame, text=lang_ui['label_threshold'])
    bounce_label.pack(side=tk.LEFT, padx=(0,5))

    def validate_number(action, value_if_allowed):
        if action != '1':
            return True
        try:
            float(value_if_allowed)
            return True
        except ValueError:
            return False

    vcmd = (root.register(validate_number), '%d', '%P')
    bounce_entry = ttk.Entry(top_frame, width=10, validate='key',
                             validatecommand=vcmd, exportselection=0)
    bounce_entry.insert(0, str(int(bounce_time * 1000)))
    bounce_entry.pack(side=tk.LEFT)
    bounce_entry.bind("<FocusOut>", lambda e: e.widget.selection_clear())

    apply_button = ttk.Button(top_frame, text=lang_ui['btn_apply'], command=update_bounce_threshold)
    apply_button.pack(side=tk.LEFT, padx=(5,10))

    lang_button_en = ttk.Button(top_frame, text=lang_ui['btn_en'], width=3, command=lambda: switch_language_to('en'))
    lang_button_en.pack(side=tk.RIGHT, padx=(5,5))
    lang_button_id = ttk.Button(top_frame, text=lang_ui['btn_id'], width=3, command=lambda: switch_language_to('id'))
    lang_button_id.pack(side=tk.RIGHT, padx=(5,5))

    status_frame = ttk.Frame(top_frame)
    status_frame.pack(side=tk.RIGHT, padx=(5,5))
    status_circle_label = ttk.Label(status_frame, text="●", font=("TkDefaultFont", 14))
    status_circle_label.pack(side=tk.LEFT, padx=(0,2))
    status_word_label = ttk.Label(status_frame, text="Enable", font=("TkDefaultFont", 14), foreground="black")
    status_word_label.pack(side=tk.LEFT)

    notebook = ttk.Notebook(root)
    notebook.pack(fill=tk.BOTH, expand=True)

    tab_stats = ttk.Frame(notebook)
    notebook.add(tab_stats, text=lang_ui.get('tab_stats', "Statistics"))

    stats_cols = (
        lang_ui['stats_col_key'],
        lang_ui['stats_col_press'],
        lang_ui['stats_col_bounce'],
        lang_ui['stats_col_percent']
    )
    stats_tree = ttk.Treeview(tab_stats, columns=stats_cols, show="headings", selectmode="none")
    stats_tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10,0))
    for col in stats_cols:
        stats_tree.heading(col, text=col)
        stats_tree.column(col, anchor="center", stretch=True)

    sort_frame = ttk.Frame(tab_stats)
    sort_frame.pack(pady=5)

    sorting_label = ttk.Label(sort_frame, text=lang_ui.get('sorting_label', "Sort By:"))
    sorting_label.pack(side=tk.LEFT, padx=(0,5))

    sort_items = [
        lang_ui.get('sorting_time_added_asc', "Time Added (ASC)"),
        lang_ui.get('sorting_time_added_desc', "Time Added (DESC)"),
        lang_ui.get('sorting_key_asc', "Key (ASC)"),
        lang_ui.get('sorting_key_desc', "Key (DESC)"),
        lang_ui.get('sorting_press_asc', "Press Count (ASC)"),
        lang_ui.get('sorting_press_desc', "Press Count (DESC)"),
        lang_ui.get('sorting_bounce_asc', "Bounce Count (ASC)"),
        lang_ui.get('sorting_bounce_desc', "Bounce Count (DESC)"),
        lang_ui.get('sorting_percent_asc', "% Bounce (ASC)"),
        lang_ui.get('sorting_percent_desc', "% Bounce (DESC)")
    ]

    stats_sort_combobox = ttk.Combobox(sort_frame,
                                       values=sort_items,
                                       state="readonly",
                                       width=25,
                                       exportselection=0)
    stats_sort_combobox.bind("<FocusIn>", lambda e: e.widget.selection_clear())
    stats_sort_combobox.set(sort_items[0])
    stats_sort_combobox.pack(side=tk.LEFT)

    def combobox_selection_changed(event):
        global stats_sort_mode
        stats_sort_mode = stats_sort_combobox.get()
        render_stats_table(preserve_scroll=False)

    stats_sort_combobox.bind("<<ComboboxSelected>>", combobox_selection_changed)

    tab1 = ttk.Frame(notebook)
    notebook.add(tab1, text=lang_ui['tab_input_log'])

    tab2 = ttk.Frame(notebook)
    notebook.add(tab2, text=lang_ui['tab_bounce'])

    tab3 = ttk.Frame(notebook)
    notebook.add(tab3, text=lang_ui.get('tab_custom_threshold', "Custom Threshold"))

    tab5 = ttk.Frame(notebook)
    notebook.add(tab5, text=lang_ui.get('tab_pause_bounce', "Pause Debounce"))

    tab6 = ttk.Frame(notebook)
    notebook.add(tab6, text=lang_ui.get('tab_advanced', "Advanced Settings"))

    tab7 = ttk.Frame(notebook)
    notebook.add(tab7, text=lang_ui.get('tab_about', "About"))

    input_text = ScrolledText(tab1, height=20, state='disabled')
    input_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    bounce_text = ScrolledText(tab2, height=20, state='disabled')
    bounce_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    custom_frame = ttk.Frame(tab3, padding="10")
    custom_frame.pack(fill=tk.X, pady=(0,10))

    custom_label_key = ttk.Label(custom_frame, text=("Tombol:" if current_language=='id' else "Key:"))
    custom_label_key.grid(row=0, column=0, sticky="w", padx=5, pady=5)
    custom_key_entry = ttk.Entry(custom_frame, width=15, state='readonly', exportselection=0)
    custom_key_entry.grid(row=0, column=1, sticky="w", padx=5, pady=5)
    custom_key_entry.bind("<FocusOut>", lambda e: e.widget.selection_clear())

    detect_button = ttk.Button(custom_frame, text=lang_ui.get('btn_detect', "Detect"), command=detect_custom_key)
    detect_button.grid(row=0, column=2, padx=5, pady=5)

    custom_label_thr = ttk.Label(custom_frame, text=("Batas (ms):" if current_language=='id' else "Threshold (ms):"))
    custom_label_thr.grid(row=1, column=0, sticky="w", padx=5, pady=5)

    custom_entry_vcmd = (root.register(validate_number), '%d', '%P')
    custom_threshold_entry = ttk.Entry(custom_frame, width=15, validate='key',
                                       validatecommand=custom_entry_vcmd, exportselection=0)
    custom_threshold_entry.grid(row=1, column=1, sticky="w", padx=5, pady=5)
    custom_threshold_entry.bind("<FocusOut>", lambda e: e.widget.selection_clear())

    custom_save_button = ttk.Button(custom_frame, text=lang_ui.get('btn_save', "Save"), command=update_custom_threshold_gui)
    custom_save_button.grid(row=1, column=2, padx=5, pady=5)

    tree_frame = ttk.Frame(tab3, padding="10")
    tree_frame.pack(fill=tk.BOTH, expand=True)
    headers = ("Tombol", "Batas (ms)") if current_language=='id' else ("Key", "Threshold (ms)")
    custom_tree = ttk.Treeview(tree_frame, columns=headers, show="headings", selectmode="browse")
    for col in headers:
        custom_tree.heading(col, text=col)
        custom_tree.column(col, anchor="center", stretch=True)
    custom_tree.grid(row=0, column=0, sticky="nsew")
    tree_frame.columnconfigure(0, weight=1)
    tree_frame.rowconfigure(0, weight=1)
    tree_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=custom_tree.yview)
    custom_tree.configure(yscroll=tree_scroll.set)
    tree_scroll.grid(row=0, column=1, sticky="ns")

    delete_button = ttk.Button(tab3, text=lang_ui.get('btn_delete', "Delete"), command=delete_custom_threshold)
    delete_button.pack(pady=5)

    pause_frame_global = ttk.Frame(tab5, padding="10")
    pause_frame_global.pack(fill=tk.X, pady=(0,10))

    label_pause = ttk.Label(pause_frame_global, text=lang_ui.get('label_pause_shortcut', "Pause Shortcut:"))
    label_pause.grid(row=0, column=0, sticky="w", padx=5, pady=5)
    pause_entry_global = ttk.Entry(pause_frame_global, width=15, state='readonly', exportselection=0)
    pause_entry_global.grid(row=0, column=1, sticky="w", padx=5, pady=5)
    pause_entry_global.bind("<FocusOut>", lambda e: e.widget.selection_clear())

    btn_detect_pause_global = ttk.Button(pause_frame_global, text=lang_ui.get('btn_detect_pause', "Detect"),
                                         command=lambda: detect_pause_key(pause_entry_global))
    btn_detect_pause_global.grid(row=0, column=2, padx=5, pady=5)
    btn_manual_pause_global = ttk.Button(pause_frame_global, text=lang_ui.get('btn_manual_pause', "Manual Pause"),
                                         command=manual_pause_action)
    btn_manual_pause_global.grid(row=0, column=3, padx=5, pady=5)

    label_continue = ttk.Label(pause_frame_global, text=lang_ui.get('label_continue_shortcut', "Continue Shortcut:"))
    label_continue.grid(row=1, column=0, sticky="w", padx=5, pady=5)
    continue_entry_global = ttk.Entry(pause_frame_global, width=15, state='readonly', exportselection=0)
    continue_entry_global.grid(row=1, column=1, sticky="w", padx=5, pady=5)
    continue_entry_global.bind("<FocusOut>", lambda e: e.widget.selection_clear())

    btn_detect_continue_global = ttk.Button(pause_frame_global, text=lang_ui.get('btn_detect_continue', "Detect"),
                                            command=lambda: detect_pause_key(continue_entry_global))
    btn_detect_continue_global.grid(row=1, column=2, padx=5, pady=5)
    btn_manual_continue_global = ttk.Button(pause_frame_global, text=lang_ui.get('btn_manual_continue', "Manual Continue"),
                                            command=manual_continue_action)
    btn_manual_continue_global.grid(row=1, column=3, padx=5, pady=5)
    btn_save_shortcut_global = ttk.Button(pause_frame_global, text=lang_ui.get('btn_save', "Save"),
                                          command=lambda: update_pause_shortcuts(pause_entry_global.get(),
                                                                                 continue_entry_global.get()))
    btn_save_shortcut_global.grid(row=2, column=0, columnspan=4, pady=10)

    adv_frame = ttk.Frame(tab6, padding="10")
    adv_frame.pack(fill=tk.X, pady=(0,10))

    force_disable_qemu_var = tk.BooleanVar(value=force_disable_qemu)
    chk = ttk.Checkbutton(adv_frame,
                          text=lang_ui.get('chk_force_disable_qemu'),
                          variable=force_disable_qemu_var,
                          command=update_force_disable_qemu)
    chk.pack(anchor="w", pady=(0,10))

    mode_frame = ttk.Frame(adv_frame)
    mode_frame.pack(fill=tk.X, pady=(5,5))

    detection_mode_label = ttk.Label(mode_frame, text=lang_ui.get('label_detection_mode', "Detection Mode:"))
    detection_mode_label.pack(side=tk.LEFT, padx=(0,5))

    detection_mode_values = [
        lang_ui.get('option_key_down', "After Press"),
        lang_ui.get('option_key_up', "After Release")
    ]
    detection_mode_var = tk.StringVar(
        value=(lang_ui.get('option_key_down', "After Press") if bounce_mode=="down"
               else lang_ui.get('option_key_up', "After Release"))
    )

    detection_mode_dropdown = ttk.Combobox(mode_frame,
                                           textvariable=detection_mode_var,
                                           values=detection_mode_values,
                                           state="readonly",
                                           exportselection=0)
    detection_mode_dropdown.bind("<FocusIn>", lambda e: e.widget.selection_clear())
    detection_mode_dropdown.pack(side=tk.LEFT)

    def on_detection_mode_change(event):
        global bounce_mode
        selected = detection_mode_var.get().lower()
        if "press" in selected or "tekan" in selected:
            new_mode = "down"
        else:
            new_mode = "up"
        if new_mode != bounce_mode:
            bounce_mode = new_mode
            save_config()
            queue_input_event('detection_mode_changed', {'mode': detection_mode_var.get()})

    detection_mode_dropdown.bind("<<ComboboxSelected>>", on_detection_mode_change)

    row2_frame = ttk.Frame(adv_frame)
    row2_frame.pack(fill=tk.X, pady=(5,5))

    label_hold_r = ttk.Label(row2_frame, text=lang_ui.get('label_hold_rate', "Repeat Rate (keys/sec):"))
    label_hold_r.pack(side=tk.LEFT, padx=(0,5))

    def validate_float(action, val_if_allowed):
        if action != '1':
            return True
        try:
            float(val_if_allowed)
            return True
        except:
            return False

    float_vcmd = (row2_frame.register(validate_float), '%d', '%P')
    entry_hold_rate = ttk.Entry(row2_frame, width=6, validate='key',
                                validatecommand=float_vcmd, exportselection=0)
    entry_hold_rate.pack(side=tk.LEFT, padx=(0,10))
    entry_hold_rate.insert(0, str(repeat_rate))
    entry_hold_rate.bind("<FocusOut>", lambda e: e.widget.selection_clear())

    label_hold_d = ttk.Label(row2_frame, text=lang_ui.get('label_hold_delay', "Delay before repeat (ms):"))
    label_hold_d.pack(side=tk.LEFT, padx=(0,5))
    entry_hold_delay = ttk.Entry(row2_frame, width=6, validate='key',
                                 validatecommand=float_vcmd, exportselection=0)
    entry_hold_delay.pack(side=tk.LEFT, padx=(0,10))
    entry_hold_delay.insert(0, str(int(repeat_delay*1000)))
    entry_hold_delay.bind("<FocusOut>", lambda e: e.widget.selection_clear())

    btn_apply_hold = ttk.Button(row2_frame, text=lang_ui.get('btn_apply', "Apply"), command=apply_repeat_settings)
    btn_apply_hold.pack(side=tk.LEFT)

    tab7_frame = ttk.Frame(tab7, padding="10")
    tab7_frame.pack(fill=tk.BOTH, expand=True)

    about_title_label = ttk.Label(tab7_frame, text=lang_ui.get('about_title',"Keyboard Debounce v1.1.0"),
                                  font=("TkDefaultFont", 12, "bold"), anchor="w", justify="left")
    about_title_label.pack(pady=(20,5), padx=10, anchor="w")

    about_license_label = ttk.Label(tab7_frame, text=lang_ui.get('about_license',"MIT License"),
                                    font=("TkDefaultFont", 10, "bold"), anchor="w", justify="left")
    about_license_label.pack(pady=(0,5), padx=10, anchor="w")

    copyright_text = lang_ui.get('about_copyright',"Copyright (c) 2025")
    about_copyright_label = ttk.Label(
        tab7_frame,
        text=copyright_text,
        font=("TkDefaultFont", 10, "bold"),
        anchor="w",
        justify="left"
    )
    about_copyright_label.pack(pady=(0,10), padx=10, anchor="w")

    license_str = """Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in 
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR 
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, 
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL 
THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER 
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, 
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN 
THE SOFTWARE.
"""
    license_label = ttk.Label(tab7_frame, text=license_str, anchor="w", justify="left", wraplength=800)
    license_label.pack(fill=tk.X, padx=10, pady=(0,10))

    notebook.bind("<<NotebookTabChanged>>", on_tab_changed)

    def update_status_indicator():
        global global_keyboard_grabbed
        if global_keyboard_grabbed:
            status_circle_label.config(foreground="green")
            status_word_label.config(text="Enable")
        else:
            status_circle_label.config(foreground="gray")
            status_word_label.config(text="Disable")
        root.after(500, update_status_indicator)

    update_status_indicator()

    monitor_thread = threading.Thread(target=monitor_keyboard, daemon=True)
    monitor_thread.start()

    def initial_render():
        render_all_logs()
        adjust_treeview_columns_on_tab_change()
        render_stats_table()

    initial_render()

    bottom_frame = ttk.Frame(root, padding="5")
    bottom_frame.pack(side=tk.BOTTOM, fill=tk.X)

    link_label = ttk.Label(
        bottom_frame,
        text="https://github.com/mohammadfirmansyah/keyboard-debounce",
        cursor="hand2"
    )
    link_label.pack(side=tk.LEFT, padx=(5,5))

    def open_link(event):
        import webbrowser
        webbrowser.open("https://github.com/mohammadfirmansyah/keyboard-debounce")
    link_label.bind("<Button-1>", open_link)

    copyright_label = ttk.Label(
        bottom_frame,
        text="© 2025 Mohammad Firman Syah",
        anchor="e"
    )
    copyright_label.pack(side=tk.RIGHT)

    update_ui_language()

    root.after(50, process_event_queue)

    def cek_titlebar():
        root.update_idletasks()
        if root.overrideredirect():
            print("Title bar tidak terdeteksi, aplikasi akan direstart.")
            os.execv(sys.executable, [sys.executable] + sys.argv)
        else:
            root.after(2000, cek_titlebar)

    root.after(2000, cek_titlebar)

    root.mainloop()
else:
    run_background()
