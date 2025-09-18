# -*- coding: utf-8 -*-
#------------------------------------------------------------------------------
# built-in packages
import sys
import threading
import subprocess
import websocket
import queue
import multiprocessing
import uuid
import json
import base64
import struct
#------------------------------------------------------------------------------
# third-party packages
from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QLineEdit, QPushButton,
    QHBoxLayout, QVBoxLayout, QComboBox
)
from PyQt5.QtGui import QImage, QPixmap, QIcon
from PyQt5.QtCore import QTimer
import requests
from construct import Struct, Int32ub, Int8ub, Bytes, this
#------------------------------------------------------------------------------
# self-defined packages
#------------------------------------------------------------------------------

import sys
import threading
import subprocess
import websocket
import queue
import multiprocessing
import uuid
import json
from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QLineEdit, QPushButton,
    QHBoxLayout, QVBoxLayout, QComboBox
)
from PyQt5.QtGui import QImage, QPixmap
from PyQt5.QtCore import QTimer
import requests
import base64
from construct import Struct, Int32ub, Int8ub, Bytes

# --------------------------------------------------------------------------
# Reference ISO BMFF definition: tfhd structure
TFHDBox = Struct(
    "size" / Int32ub,
    "type" / Bytes(4),
    "version" / Int8ub,
    "flags" / Bytes(3),
    "track_ID" / Int32ub,
)

# --------------------------------------------------------------------------
# WebSocket stream handler, runs in a separate thread
class StreamClient(threading.Thread):
    def __init__(self, url, headers, on_data_callback):
        super().__init__(daemon=True)
        self.url = url
        self.headers = headers
        self.on_data_callback = on_data_callback
        self.ws = None

    # --------------------------------------------------------------------------
    def run(self):
        self.ws = websocket.WebSocketApp(
            self.url,
            header=self.headers,
            on_message=self.on_message,
            on_open=self.on_open,
            on_error=self.on_error,
            on_close=self.on_close
        )
        self.ws.run_forever()

    # --------------------------------------------------------------------------
    def on_open(self, ws):
        print("WebSocket connected")

    # --------------------------------------------------------------------------
    def on_message(self, ws, message):
        if isinstance(message, bytes):
            self.on_data_callback(message)

    # --------------------------------------------------------------------------
    def on_error(self, ws, error):
        print("WebSocket error:", error)

    # --------------------------------------------------------------------------
    def on_close(self, ws, close_status_code, close_msg):
        print("WebSocket closed")

    # --------------------------------------------------------------------------
    def send_command(self, command):
        if self.ws:
            self.ws.send(command)

    # --------------------------------------------------------------------------
    def close(self):
        if self.ws:
            self.ws.close()

# --------------------------------------------------------------------------
# FFmpeg decoding process: reads fragments from fragment_queue and outputs raw frames
def ffmpeg_decode_process(fragment_queue: multiprocessing.Queue, frame_queue: multiprocessing.Queue):
    ffmpeg_cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts+nobuffer",
        "-flags", "low_delay",
        "-analyzeduration", "0",
        "-probesize", "32",
        "-vsync", "0",
        "-i", "pipe:0",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-s", "640x360",
        "-"
    ]

    proc = subprocess.Popen(
        ffmpeg_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
        executable='./import/ffmpeg/ffmpeg.exe'
    )

    frame_size = 640 * 360 * 3

    def write_in_chunks(pipe, data, chunk_size=8192):
        for i in range(0, len(data), chunk_size):
            try:
                pipe.write(data[i:i+chunk_size])
                pipe.flush()
            except (BrokenPipeError, OSError) as e:
                print(f"Error writing to ffmpeg stdin: {e}")
                return False
        return True

    def is_valid_fragment(fragment: bytes) -> bool:
        if not isinstance(fragment, bytes) or len(fragment) < 12:
            return False
        if b'moof' not in fragment or b'mdat' not in fragment:
            return b'moov' in fragment
        return True

    def feed_fragments():
        while True:
            fragment = fragment_queue.get()
            if fragment is None or proc.poll() is not None:
                break
            if is_valid_fragment(fragment):
                if not write_in_chunks(proc.stdin, fragment):
                    break
        try:
            proc.stdin.close()
        except Exception:
            pass

    threading.Thread(target=feed_fragments, daemon=True).start()

    while True:
        try:
            raw = b""
            while len(raw) < frame_size:
                chunk = proc.stdout.read(frame_size - len(raw))
                if not chunk:
                    break
                raw += chunk
            if len(raw) == frame_size:
                try:
                    frame_queue.put_nowait(raw)
                except queue.Full:
                    _ = frame_queue.get_nowait()
                    frame_queue.put_nowait(raw)
        except Exception as e:
            print("FFmpeg read error (ignored):", e)
            continue

    proc.kill()
    proc.wait()

# --------------------------------------------------------------------------
# MP4 parsing utilities
def find_mdat(data: bytes):
    offset = 0
    length = len(data)
    while offset + 8 <= length:
        size = int.from_bytes(data[offset:offset+4], 'big')
        box_type = data[offset+4:offset+8]
        if size == 1:
            size = int.from_bytes(data[offset+8:offset+16], 'big')
            header_size = 16
        else:
            header_size = 8
        if box_type == b'mdat':
            return offset + header_size, offset + size
        offset += size
    return None, None

# --------------------------------------------------------------------------
def find_nal_units(data: bytes):
    i, length = 0, len(data)
    while i < length - 4:
        if data[i:i+3] in [b'\x00\x00\x01', b'\x00\x00\x00\x01']:
            start = i + (3 if data[i:i+3] == b'\x00\x00\x01' else 4)
            j = start
            while j < length - 4:
                if data[j:j+3] == b'\x00\x00\x01' or data[j:j+4] == b'\x00\x00\x00\x01':
                    break
                j += 1
            yield data[start:j]
            i = j
        else:
            i += 1

# --------------------------------------------------------------------------
def has_i_frame(moof_mdat_data: bytes) -> bool:
    mdat_start, mdat_end = find_mdat(moof_mdat_data)
    if mdat_start is None:
        print("No mdat box found")
        return False
    mdat_data = moof_mdat_data[mdat_start:mdat_end]
    for nal in find_nal_units(mdat_data):
        if nal and (nal[0] & 0x1F) == 5:
            return True
    return False

# --------------------------------------------------------------------------
# PyQt5 Video Player GUI
class VideoPlayer(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("WebSocket MP4 Player")
        self.resize(760, 400)
        self.id = 1
        self.strHandleId = str(uuid.uuid4())
        self.is_connected = False

        # ---------------- Video Display ----------------
        self.image_label = QLabel("Waiting for stream...")
        self.image_label.setFixedSize(640, 360)

        # ---------------- Product / IP Inputs ----------------
        self.product_combo = QComboBox()
        self.product_combo.addItems(["IVAR", "IVARM"])
        self.product_combo.currentTextChanged.connect(self.on_product_changed)

        self.ip_input = QLineEdit()
        self.ip_input.setPlaceholderText("e.g., 192.168.2.114")

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Username")

        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Password")
        self.password_input.setEchoMode(QLineEdit.Password)

        self.connect_btn = QPushButton("CONNECT WS")
        self.connect_btn.clicked.connect(self.toggle_connection)

        product_ip_layout = QHBoxLayout()
        product_ip_layout.addWidget(QLabel("Product:"))
        product_ip_layout.addWidget(self.product_combo)
        product_ip_layout.addWidget(QLabel("IP:"))
        product_ip_layout.addWidget(self.ip_input)
        product_ip_layout.addWidget(QLabel("Username:"))
        product_ip_layout.addWidget(self.username_input)
        product_ip_layout.addWidget(QLabel("Password:"))
        product_ip_layout.addWidget(self.password_input)
        product_ip_layout.addWidget(self.connect_btn)

        # ---------------- Channel / Time Inputs ----------------
        self.channel_input = QLineEdit()
        self.channel_input.setPlaceholderText("Channel ID / Number")

        self.start_time_input = QLineEdit()
        self.start_time_input.setPlaceholderText("Start Time e.g., 2025-07-25T08:10:00.000Z")

        self.end_time_input = QLineEdit()
        self.end_time_input.setPlaceholderText("End Time e.g., 2025-07-25T08:20:00.000Z")

        time_layout = QVBoxLayout()
        time_layout.addWidget(QLabel("Channel ID / Number:"))
        time_layout.addWidget(self.channel_input)
        time_layout.addWidget(QLabel("Start Time:"))
        time_layout.addWidget(self.start_time_input)
        time_layout.addWidget(QLabel("End Time:"))
        time_layout.addWidget(self.end_time_input)

        # ---------------- Control Buttons ----------------
        self.live_btn = QPushButton("LIVE")
        self.playback_btn = QPushButton("PLAYBACK")
        self.pause_btn = QPushButton("PAUSE")
        self.stop_btn = QPushButton("STOP")
        self.speed_btn = QPushButton("1X")

        for btn in [self.live_btn, self.playback_btn, self.pause_btn, self.stop_btn, self.speed_btn]:
            btn.setEnabled(False)

        control_layout = QHBoxLayout()
        for btn in [self.live_btn, self.playback_btn, self.pause_btn, self.stop_btn, self.speed_btn]:
            control_layout.addWidget(btn)

        # ---------------- Main Layout ----------------
        main_layout = QVBoxLayout()
        main_layout.addWidget(self.image_label)
        main_layout.addLayout(product_ip_layout)
        main_layout.addLayout(time_layout)
        main_layout.addLayout(control_layout)
        self.setLayout(main_layout)

        # ---------------- Queues & Decoder ----------------
        self.fragment_queue = multiprocessing.Queue(maxsize=20)
        self.frame_queue = multiprocessing.Queue(maxsize=5)
        self.decoder_process = None
        self.client = None

        # ---------------- Initialize default product ----------------
        self.product_combo.setCurrentIndex(0)
        self.on_product_changed(self.product_combo.currentText())

        # ---------------- Frame Update Timer ----------------
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_frame)
        self.timer.start(33)

    # ---------------- Product Selection ----------------
    def on_product_changed(self, product):
        self.channel_input.clear()
        if product == "IVAR":
            self.channel_input.setPlaceholderText("Channel Number e.g., 1")
        else:
            self.channel_input.setPlaceholderText("Channel ID e.g., {UUID}")
        self.update_button_bindings(product)

    # ---------------- Dynamic Button Bindings ----------------
    def update_button_bindings(self, product):
        # Disconnect previous bindings
        for btn in [self.live_btn, self.playback_btn, self.pause_btn, self.stop_btn, self.speed_btn]:
            try:
                btn.clicked.disconnect()
            except TypeError:
                pass

        if product == "IVAR":
            self.live_btn.clicked.connect(lambda: [self.send_live_command_ivar(), self.set_buttons_live()])
            self.playback_btn.clicked.connect(lambda: [self.send_playback_command_ivar(), self.set_buttons_playback()])
            self.pause_btn.clicked.connect(lambda: [self.send_pause_command_ivar(), self.set_buttons_pause()])
            self.stop_btn.clicked.connect(lambda: [self.send_stop_command_ivar(), self.set_buttons_enabled(True)])
            self.speed_btn.clicked.connect(lambda: [self.send_speed_command_ivar(), self.set_buttons_playback()])
        else:
            self.live_btn.clicked.connect(lambda: [self.send_live_command_ivarm(), self.set_buttons_live()])
            self.playback_btn.clicked.connect(lambda: [self.send_playback_command_ivarm(), self.set_buttons_playback()])
            self.pause_btn.clicked.connect(lambda: [self.send_pause_command_ivarm(), self.set_buttons_pause()])
            self.stop_btn.clicked.connect(lambda: [self.send_stop_command_ivarm(), self.set_buttons_enabled(True)])
            self.speed_btn.clicked.connect(lambda: [self.send_speed_command_ivarm(), self.set_buttons_playback()])

    # ---------------- Connect / Disconnect ----------------
    def toggle_connection(self):
        if not self.is_connected:
            ip = self.ip_input.text().strip()
            if not ip:
                print("Please enter a valid IP address.")
                return
            product = self.product_combo.currentText()
            self.connect_ws_by_product(product, ip)
            self.set_buttons_enabled(True) 
        else:
            self.disconnect_ws()

    # --------------------------------------------------------------------------
    def connect_ws_by_product(self, product, ip):
        isProductIVAR = product == "IVAR"
        proxy_login_url = f"http://{ip}:{'8001' if isProductIVAR else '9501'}/session/login"
        ws_url = f"ws://{ip}:{'8001' if isProductIVAR else '9501'}/{'ws2' if isProductIVAR else 'cloud_mgr/media_stream'}"

        self.connect_ws(ws_url, proxy_login_url, isProductIVAR)

    # ---------------- Connect WebSocket ----------------
    def connect_ws(self, ws_url, proxy_login_url, isProductIVAR):
        """Establish WebSocket connection and start decoder process"""
        self.disconnect_ws()  # cleanup previous state

        self.fragment_queue = multiprocessing.Queue(maxsize=20)
        self.frame_queue = multiprocessing.Queue(maxsize=5)

        # Start decoder process
        self.decoder_process = multiprocessing.Process(
            target=ffmpeg_decode_process,
            args=(self.fragment_queue, self.frame_queue)
        )
        self.decoder_process.daemon = True
        self.decoder_process.start()

        # Prepare headers with Basic Auth
        self.product_combo.setEnabled(False)
        self.ip_input.setEnabled(False)
        self.username_input.setEnabled(False)
        self.password_input.setEnabled(False)
        username = self.username_input.text()
        password = self.password_input.text()
        auth_b64 = base64.b64encode(f"{username}:{password}".encode()).decode()
        response = requests.get(proxy_login_url, headers={"Authorization": f"Basic {auth_b64}"})

        if response.status_code != 200:
            print("Failed login or connection.")
            self.decoder_process.terminate()
            return

        token = response.headers.get("X-SessionToken")
        self.headers = {"X-SessionToken": token}

        self.client = StreamClient(ws_url, self.headers,
                                   self.on_stream_data_ivarm if not isProductIVAR else self.on_stream_data_ivar)
        self.client.start()
        self.is_connected = True
        self.connect_btn.setText("DISCONNECT")
        print(f"WebSocket connected to: {ws_url}")

    # ---------------- Disconnect WebSocket ----------------
    def disconnect_ws(self):
        """Safely close WebSocket and decoder process"""
        self.product_combo.setEnabled(True)
        self.ip_input.setEnabled(True)
        self.username_input.setEnabled(True)
        self.password_input.setEnabled(True)

        # Stop WebSocket
        if self.client:
            try:
                self.client.close()
            except:
                pass
            self.client = None

        # Send sentinel to decoder
        if self.fragment_queue:
            try:
                self.fragment_queue.put_nowait(None)
            except:
                pass

        # Wait for decoder to terminate
        if self.decoder_process and self.decoder_process.is_alive():
            self.decoder_process.join(timeout=1)
            if self.decoder_process.is_alive():
                self.decoder_process.terminate()
        self.decoder_process = None

        # Clear queues
        for q in [self.fragment_queue, self.frame_queue]:
            if q:
                while not q.empty():
                    try:
                        q.get_nowait()
                    except:
                        break

        self.is_connected = False
        self.connect_btn.setText("CONNECT WS")
        print("Disconnected and cleaned up.")


    #--------------------------------------------------------------------------
    def __get_ip_and_channel(self):
        return self.ip_input.text().strip(), self.channel_input.text().strip()

    #--------------------------------------------------------------------------
    def __get_start_time(self):
        return self.start_time_input.text().strip()

    #--------------------------------------------------------------------------
    def __get_end_time(self):
        return self.end_time_input.text().strip()

    # ---------------- Button State Helpers ----------------
    def set_buttons_live(self):
        for btn in [self.live_btn, self.playback_btn, self.pause_btn, self.speed_btn]:
            btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.channel_input.setReadOnly(True)

    # --------------------------------------------------------------------------
    def set_buttons_playback(self):
        for btn in [self.live_btn, self.playback_btn]:
            btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self.speed_btn.setEnabled(True)
        self.channel_input.setReadOnly(True)
        self.start_time_input.setReadOnly(True)
        self.end_time_input.setReadOnly(True)

    # --------------------------------------------------------------------------
    def set_buttons_pause(self):
        for btn in [self.live_btn, self.playback_btn, self.pause_btn]:
            btn.setEnabled(False)
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.speed_btn.setEnabled(True)
        self.channel_input.setReadOnly(True)
        self.start_time_input.setReadOnly(True)
        self.end_time_input.setReadOnly(True)

    # --------------------------------------------------------------------------
    def set_buttons_enabled(self, enabled: bool):
        for btn in [self.live_btn, self.playback_btn, self.pause_btn, self.stop_btn, self.speed_btn]:
            btn.setEnabled(enabled)
        self.channel_input.setReadOnly(False)
        self.start_time_input.setReadOnly(False)
        self.end_time_input.setReadOnly(False)

    # ---------------- IVARM Commands ----------------
    def send_live_command_ivarm(self):
        _, channel_id = self.__get_ip_and_channel()
        cmd = {
            "command": "LIVE_MEDIA_STREAM",
            "stream": 0,
            "handleId": self.strHandleId,
            "requestId": str(uuid.uuid4()),
            "channelId": channel_id
        }
        self.send_command(json.dumps(cmd))

    # --------------------------------------------------------------------------
    def send_playback_command_ivarm(self):
        _, channel_id = self.__get_ip_and_channel()
        cmd = {
            "command": "PLAYBACK_MEDIA_STREAM",
            "stream": 0,
            "handleId": self.strHandleId,
            "requestId": str(uuid.uuid4()),
            "channelId": channel_id,
            "startTime": self.__get_start_time()
        }
        if self.__get_end_time():
            cmd["endTime"] = self.__get_end_time()
        self.send_command(json.dumps(cmd))

    # --------------------------------------------------------------------------
    def send_speed_command_ivarm(self):
        cmd = {
            "command": "SET_PLAYBACK_SPEED",
            "handleId": self.strHandleId,
            "requestId": str(uuid.uuid4()),
            "value": 1
        }
        self.send_command(json.dumps(cmd))

    # --------------------------------------------------------------------------
    def send_stop_command_ivarm(self):
        cmd = {
            "command": "STOP_MEDIA_STREAM",
            "handleId": self.strHandleId,
            "requestId": str(uuid.uuid4()),
        }
        self.send_command(json.dumps(cmd))
        self.clear_video_and_restart_decoder()

    # --------------------------------------------------------------------------
    def send_pause_command_ivarm(self):
        cmd = {
            "command": "PAUSE_MEDIA_STREAM",
            "handleId": self.strHandleId,
            "requestId": str(uuid.uuid4()),
        }
        self.send_command(json.dumps(cmd))

    # ---------------- IVAR Commands ----------------
    def send_live_command_ivar(self):
        _, channel = self.__get_ip_and_channel()
        cmd = f'/live/ch{channel}?id={self.id}'
        self.send_command(cmd)

    # --------------------------------------------------------------------------
    def send_playback_command_ivar(self):
        _, channel = self.__get_ip_and_channel()
        cmd = f'/playback/ch{channel}?id={self.id}&begin={self.__get_start_time()}'
        if self.__get_end_time():
            cmd += f'&{self.__get_end_time()}'
        self.send_command(cmd)

    # --------------------------------------------------------------------------
    def send_speed_command_ivar(self):
        _, channel = self.__get_ip_and_channel()
        cmd = f'/speed?id={self.id}&val=1'
        self.send_command(cmd)

    # --------------------------------------------------------------------------
    def send_stop_command_ivar(self):
        _, channel = self.__get_ip_and_channel()
        cmd = f'/stop?id={self.id}'
        self.send_command(cmd)
        self.clear_video_and_restart_decoder()

    # --------------------------------------------------------------------------
    def send_pause_command_ivar(self):
        _, channel = self.__get_ip_and_channel()
        cmd = f'/pause?id={self.id}'
        self.send_command(cmd)

    # ---------------- Helper ----------------
    def clear_video_and_restart_decoder(self):
        """Clear video display and restart decoder process"""
        self.image_label.clear()
        self.image_label.setText("Waiting for stream...")

        while not self.fragment_queue.empty():
            try: self.fragment_queue.get_nowait()
            except: break

        while not self.frame_queue.empty():
            try: self.frame_queue.get_nowait()
            except: break

        if self.decoder_process.is_alive():
            try:
                self.fragment_queue.put(None)
                self.decoder_process.terminate()
                self.decoder_process.join()
            except Exception as e:
                print(f"Error terminating decoder process: {e}")

        # Recreate queues and restart decoder
        self.fragment_queue = multiprocessing.Queue(maxsize=20)
        self.frame_queue = multiprocessing.Queue(maxsize=5)
        self.decoder_process = multiprocessing.Process(
            target=ffmpeg_decode_process,
            args=(self.fragment_queue, self.frame_queue)
        )
        self.decoder_process.daemon = True
        self.decoder_process.start()

    # ---------------- Queue Command ----------------
    def send_command(self, command):
        print("Send command:", command)
        if self.client:
            self.client.send_command(command)

        for q in [self.fragment_queue, self.frame_queue]:
            if q:
                while not q.empty():
                    try:
                        q.get_nowait()
                    except:
                        break

    # --------------------------------------------------------------------------
    # Frame Update
    def update_frame(self):
        """Update video frame from frame_queue"""
        if not self.frame_queue:
            return
        try:
            if not self.frame_queue.empty():
                frame = self.frame_queue.get_nowait()
                if frame:
                    image = QImage(frame, 640, 360, QImage.Format_RGB888)
                    self.image_label.setPixmap(QPixmap.fromImage(image))
        except Exception as e:
            print("Frame update error:", e)

    # --------------------------------------------------------------------------
    # Stream data handlers
    def on_stream_data_ivarm(self, data):
        if isinstance(data, bytes):
            try:
                header_len = int.from_bytes(data[8:10], byteorder='big')
                media_data = data[10 + header_len:]
                if media_data:
                    try:
                        self.fragment_queue.put_nowait(media_data)
                    except queue.Full:
                        _ = self.fragment_queue.get_nowait()
                        self.fragment_queue.put_nowait(media_data)
            except Exception as e:
                print("Stream data error:", e)

    # --------------------------------------------------------------------------
    def on_stream_data_ivar(self, data):
        if isinstance(data, bytes):
            try:
                header_len = int.from_bytes(data[4:6], byteorder='big')
                media_data = data[6 + header_len:]
                if media_data:
                    try:
                        self.fragment_queue.put_nowait(media_data)
                    except queue.Full:
                        _ = self.fragment_queue.get_nowait()
                        self.fragment_queue.put_nowait(media_data)
            except Exception as e:
                print("Stream data error:", e)

# --------------------------------------------------------------------------
if __name__ == "__main__":
    if sys.platform.startswith('win'):
        multiprocessing.freeze_support()
    app = QApplication(sys.argv)
    player = VideoPlayer()
    player.show()
    sys.exit(app.exec_())