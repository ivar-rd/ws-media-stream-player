import sys
import threading
import subprocess
import websocket
import queue
import multiprocessing
import uuid
import json
from PyQt5.QtWidgets import QApplication, QWidget, QPushButton, QLineEdit, QLabel, QVBoxLayout, QHBoxLayout
from PyQt5.QtGui import QImage, QPixmap, QIcon
from PyQt5.QtCore import QTimer
import struct
from construct import Struct, Int32ub, Int8ub, Bytes, this

# 參考 ISO BMFF 定義：tfhd 結構
TFHDBox = Struct(
    "size" / Int32ub,
    "type" / Bytes(4),
    "version" / Int8ub,
    "flags" / Bytes(3),
    "track_ID" / Int32ub,
)

#--------------------------------------------------------------------------
# WebSocket 串流處理器，獨立 thread
class StreamClient(threading.Thread):
    def __init__(self, url, on_data_callback):
        super().__init__(daemon=True)
        self.url = url
        self.on_data_callback = on_data_callback
        self.ws = None

    def run(self):
        import websocket as ws
        self.ws = ws.WebSocketApp(
            self.url,
            on_message=self.on_message,
            on_open=self.on_open,
            on_error=self.on_error,
            on_close=self.on_close
        )
        self.ws.run_forever()

    def on_open(self, ws):
        print("WebSocket connected")

    def on_message(self, ws, message):
        if isinstance(message, bytes):
            self.on_data_callback(message)

    def on_error(self, ws, error):
        print("WebSocket error:", error)

    def on_close(self, ws, close_status_code, close_msg):
        print("WebSocket closed")

    def send_command(self, command):
        if self.ws:
            self.ws.send(command)

    def close(self):
        if self.ws:
            self.ws.close()

#--------------------------------------------------------------------------
# 解碼子進程，負責啟動 ffmpeg 並解碼
def ffmpeg_decode_process(fragment_queue: multiprocessing.Queue, frame_queue: multiprocessing.Queue):
    ffmpeg_cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts",
        "-use_wallclock_as_timestamps", "1",
        #"-fflags", "nobuffer",
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

    proc = subprocess.Popen(ffmpeg_cmd,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            #stderr=subprocess.DEVNULL, 
                            bufsize=0)

    frame_size = 640 * 360 * 3

    def write_in_chunks(pipe, data, chunk_size=8192):
        for i in range(0, len(data), chunk_size):
            try:
                pipe.write(data[i:i+chunk_size])
                pipe.flush()
            except BrokenPipeError:
                print("Broken pipe in ffmpeg stdin")
                return False
            except OSError as e:
                print(f"OSError writing to ffmpeg stdin: {e} fragment len={len(data)}")
                return False
        return True

    def is_valid_fragment(fragment: bytes) -> bool:
        if not isinstance(fragment, bytes):
            return False
        if len(fragment) < 12:  # 太短，不可能是合法 MP4 box
            print(f"⚠ Invalid fragment. size invalid")
            return False
        if b'moof' not in fragment or b'mdat' not in fragment:
            if b'moov' in fragment:
                return True
            print(f"⚠ Invalid fragment. moof mdat not exist.")
            return False
        return True

    def feed_fragments():
        i = 0
        while True:
            fragment = fragment_queue.get()
            if fragment is None:
                break
            if proc.poll() is not None:
                print("FFmpeg process exited, stop feeding")
                break
            if not isinstance(fragment, bytes):
                print(f"⚠ Invalid fragment type: {type(fragment)}")
                continue
            if len(fragment) == 0:
                print("⚠ Empty fragment skipped")
                continue
            if is_valid_fragment(fragment) == False:
                print(f"⚠ Invalid fragment.")
                continue
            bHasIframe = has_i_frame_length_prefixed(fragment)
            print(f'I-frame: {bHasIframe}')
 
            success = write_in_chunks(proc.stdin, fragment)
            if not success:
                break
        try:
            proc.stdin.close()
        except Exception:
            pass

    def read_box(data, offset):
        if offset + 8 > len(data):
            return None, None, None
        size, box_type = struct.unpack(">I4s", data[offset:offset+8])
        return size, box_type.decode("utf-8"), offset + 8

    feeder_thread = threading.Thread(target=feed_fragments, daemon=True)
    feeder_thread.start()

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

#--------------------------------------------------------------------------
def find_mdat(data: bytes):
    offset = 0
    length = len(data)
    while offset + 8 <= length:
        size = int.from_bytes(data[offset:offset+4], 'big')
        box_type = data[offset+4:offset+8]
        if size == 1:  # large size
            size = int.from_bytes(data[offset+8:offset+16], 'big')
            header_size = 16
        else:
            header_size = 8
        if box_type == b'mdat':
            return offset + header_size, offset + size
        offset += size
    return None, None

#--------------------------------------------------------------------------
def find_nal_units(data: bytes):
    # 簡單尋找 H.264 NAL start code (0x000001 or 0x00000001)
    i = 0
    length = len(data)
    while i < length - 4:
        if data[i:i+3] == b'\x00\x00\x01':
            start = i + 3
            # 找下一個 start code
            j = start
            while j < length - 4:
                if data[j:j+3] == b'\x00\x00\x01':
                    break
                j += 1
            yield data[start:j]
            i = j
        elif data[i:i+4] == b'\x00\x00\x00\x01':
            start = i + 4
            j = start
            while j < length - 4:
                if data[j:j+4] == b'\x00\x00\x00\x01' or data[j:j+3] == b'\x00\x00\x01':
                    break
                j += 1
            yield data[start:j]
            i = j
        else:
            i += 1

#--------------------------------------------------------------------------
def has_i_frame(moof_mdat_data: bytes) -> bool:
    mdat_start, mdat_end = find_mdat(moof_mdat_data)
    if mdat_start is None:
        print("No mdat box found")
        return False
    mdat_data = moof_mdat_data[mdat_start:mdat_end]
    for nal in find_nal_units(mdat_data):
        if len(nal) == 0:
            continue
        nal_type = nal[0] & 0x1F  # H.264 NAL type low 5 bits
        # print(f"NAL type: {nal_type}")
        if nal_type == 5:  # IDR frame (I-frame)
            return True
    return False

#--------------------------------------------------------------------------
def has_i_frame_length_prefixed(moof_mdat_data: bytes, nal_length_size=4) -> bool:
    mdat_start, mdat_end = find_mdat(moof_mdat_data)
    if mdat_start is None:
        print("No mdat box found")
        return False
    mdat_data = moof_mdat_data[mdat_start:mdat_end]

    offset = 0
    length = len(mdat_data)
    while offset + nal_length_size <= length:
        nal_size = int.from_bytes(mdat_data[offset:offset+nal_length_size], 'big')
        offset += nal_length_size
        if offset + nal_size > length:
            break
        nal = mdat_data[offset:offset+nal_size]
        offset += nal_size
        if len(nal) == 0:
            continue
        nal_type = nal[0] & 0x1F  # H.264 NAL type
        if nal_type == 5:  # IDR frame
            return True
    return False

#--------------------------------------------------------------------------
# PyQt5 GUI 播放器
class VideoPlayer(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("WebSocket MP4 Player")
        self.image_label = QLabel("Waiting for stream...")
        self.image_label.setFixedSize(640, 360)
        self.resize(760, 400)

        # IP Input + Connect Button on same row
        self.ip_label = QLabel("IVARM Cloud Manager IP:")
        self.ip_input = QLineEdit()
        #self.ip_input = QLineEdit("192.168.2.114")
        self.ip_input.setPlaceholderText("e.g., 192.168.2.114")

        self.connect_btn = QPushButton("CONNECT WS")
        self.connect_btn.clicked.connect(self.toggle_connection)

        ip_row_layout = QHBoxLayout()
        ip_row_layout.addWidget(self.ip_label)
        ip_row_layout.addWidget(self.ip_input)
        ip_row_layout.addWidget(self.connect_btn)

        # Channel ID Input
        self.channel_label = QLabel("Channel ID:")
        self.channel_input = QLineEdit()
        #self.channel_input = QLineEdit("{4428a112-47bd-5555-990f-890546518e88}")
        self.channel_input.setPlaceholderText("e.g., 3")

        # Start Time Input
        self.start_time_label = QLabel("Start Time:")
        self.start_time_input = QLineEdit()
        self.start_time_input.setPlaceholderText("e.g., 2025-07-25T08:10:00.000Z")

        # End Time Input
        self.end_time_label = QLabel("End Time:")
        self.end_time_input = QLineEdit()
        self.end_time_input.setPlaceholderText("e.g., 2025-07-25T08:20:00.000Z")

        # Control Buttons
        self.live_btn = QPushButton("LIVE")
        self.playback_btn = QPushButton("PLAYBACK")
        self.pause_btn = QPushButton("PAUSE")
        self.stop_btn = QPushButton("STOP")
        self.speed_btn = QPushButton("1X")
        #self.ff_btn = QPushButton("FF")
        #self.rew_btn = QPushButton("REW")

        # Layouts
        input_layout = QVBoxLayout()
        input_layout.addLayout(ip_row_layout)
        input_layout.addWidget(self.channel_label)
        input_layout.addWidget(self.channel_input)
        input_layout.addWidget(self.start_time_label)
        input_layout.addWidget(self.start_time_input)
        input_layout.addWidget(self.end_time_label)
        input_layout.addWidget(self.end_time_input)

        control_layout = QHBoxLayout()
        for btn in [self.live_btn, self.playback_btn, self.pause_btn,
                    self.stop_btn, self.speed_btn]:
            btn.setEnabled(False)
            control_layout.addWidget(btn)

        main_layout = QVBoxLayout()
        main_layout.addWidget(self.image_label)
        main_layout.addLayout(input_layout)
        main_layout.addLayout(control_layout)
        self.setLayout(main_layout)

        # Queues
        self.fragment_queue = multiprocessing.Queue(maxsize=20)
        self.frame_queue = multiprocessing.Queue(maxsize=5)

        # Decoder process
        self.decoder_process = multiprocessing.Process(
            target=ffmpeg_decode_process,
            args=(self.fragment_queue, self.frame_queue)
        )
        self.client = None
        self.is_connected = False

        # Button bindings
        self.live_btn.clicked.connect(lambda: [self.send_live_command(), self.set_buttons_live()])
        self.playback_btn.clicked.connect(lambda: [self.send_playback_command(), self.set_buttons_playback()])
        self.pause_btn.clicked.connect(lambda: [self.send_pause_command(), self.set_buttons_pause()])
        self.stop_btn.clicked.connect(lambda: [self.send_stop_command(), self.set_buttons_enabled(True)])  # 按 STOP 後恢復全部按鈕啟用狀態（你可自行調整）
        self.speed_btn.clicked.connect(lambda: [self.send_speed_command(), self.set_buttons_playback()])
        #self.ff_btn.clicked.connect(lambda: [self.send_forward_command(), self.set_buttons_playback()])
        #self.rew_btn.clicked.connect(lambda:[self.send_backward_command(), self.set_buttons_playback()])

        # Frame update timer
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_frame)
        self.timer.start(33)

    #--------------------------------------------------------------------------
    def toggle_connection(self):
        if not self.is_connected:
            self.connect_ws()
            self.connect_btn.setText("DISCONNECT WS")
            self.ip_input.setReadOnly(True)
            self.set_buttons_enabled(True)
            self.is_connected = True
        else:
            self.disconnect_ws()
            self.connect_btn.setText("CONNECT WS")
            self.ip_input.setReadOnly(False)
            self.is_connected = False
            self.set_buttons_enabled(False)

    #--------------------------------------------------------------------------
    def connect_ws(self):
        ip, _ = self.get_ip_and_channel()
        ws_url = f"ws://{ip}:9500/cloud_mgr/media_stream"
        self.client = StreamClient(ws_url, self.on_stream_data)
        self.client.start()
        print(f"WebSocket connected to: {ws_url}")

        self.strHandleId = str(uuid.uuid4())
        # 重建 queues
        self.fragment_queue = multiprocessing.Queue(maxsize=20)
        self.frame_queue = multiprocessing.Queue(maxsize=5)

        # 啟動新的子進程
        self.decoder_process = multiprocessing.Process(
            target=ffmpeg_decode_process,
            args=(self.fragment_queue, self.frame_queue)
        )
        self.decoder_process.daemon = True
        self.decoder_process.start()

    #--------------------------------------------------------------------------
    def disconnect_ws(self):
        if self.client:
            try:
                self.client.close()
            except Exception as e:
                print(f"Error closing WebSocket client: {e}")
            self.client = None

        while not self.fragment_queue.empty():
            try:
                self.fragment_queue.get_nowait()
            except:
                break

        while not self.frame_queue.empty():
            try:
                self.frame_queue.get_nowait()
            except:
                break

        if self.decoder_process.is_alive():
            try:
                self.fragment_queue.put(None)
                self.decoder_process.terminate()
                self.decoder_process.join()
            except Exception as e:
                print(f"Error terminating decoder process: {e}")

    #--------------------------------------------------------------------------
    def set_buttons_live(self):
        for btn in [self.live_btn, self.playback_btn, self.pause_btn, self.speed_btn]:
            btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.channel_input.setReadOnly(True)

    #--------------------------------------------------------------------------
    def set_buttons_playback(self):
        for btn in [self.live_btn, self.playback_btn]:
            btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self.speed_btn.setEnabled(True)
        self.channel_input.setReadOnly(True)
        self.start_time_input.setReadOnly(True)
        self.end_time_input.setReadOnly(True)

    #--------------------------------------------------------------------------
    def set_buttons_pause(self):
        for btn in [self.live_btn, self.playback_btn]:
            btn.setEnabled(False)
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.speed_btn.setEnabled(True)
        #self.ff_btn.setEnabled(True)
        #self.rew_btn.setEnabled(True)
        self.channel_input.setReadOnly(True)
        self.start_time_input.setReadOnly(True)
        self.end_time_input.setReadOnly(True)

    #--------------------------------------------------------------------------
    def set_buttons_enabled(self, enabled: bool):
        for btn in [
            self.live_btn, self.playback_btn, self.pause_btn,
            self.stop_btn, self.speed_btn
        ]:
            btn.setEnabled(enabled)
            self.channel_input.setReadOnly(False)
            self.start_time_input.setReadOnly(False)
            self.end_time_input.setReadOnly(False)

    #--------------------------------------------------------------------------
    def get_ip_and_channel(self):
        return self.ip_input.text().strip(), self.channel_input.text().strip()

    #--------------------------------------------------------------------------
    def send_command(self, command):
        print("Send command:", command)
        if self.client:
            self.client.send_command(command)

        while not self.fragment_queue.empty():
            try:
                self.fragment_queue.get_nowait()
            except:
                break

        while not self.frame_queue.empty():
            try:
                self.frame_queue.get_nowait()
            except:
                break

    #--------------------------------------------------------------------------
    def send_live_command(self):
        _, channel_id = self.get_ip_and_channel()
        cmd = {
            "command": "LIVE_MEDIA_STREAM",
            "stream": 0,
            "handleId": self.strHandleId,
            "requestId": str(uuid.uuid4()),
            "channelId": channel_id
        }
        self.send_command(json.dumps(cmd))

    #--------------------------------------------------------------------------
    def send_playback_command(self):
        _, channel_id = self.get_ip_and_channel()
        cmd = {
            "command": "PLAYBACK_MEDIA_STREAM",
            "stream": 0,
            "handleId": self.strHandleId,
            "requestId": str(uuid.uuid4()),
            "channelId": channel_id,
            "startTime": self.get_start_time()
        }
        if self.get_end_time():
            cmd["endTime"] = self.get_end_time()
        self.send_command(json.dumps(cmd))

    #--------------------------------------------------------------------------
    def send_speed_command(self):
        cmd = {
            "command": "SET_PLAYBACK_SPEED",
            "handleId": self.strHandleId,
            "requestId": str(uuid.uuid4()),
            "value": 1
        }
        self.send_command(json.dumps(cmd))

    #--------------------------------------------------------------------------
    def send_forward_command(self):
        cmd = {
            "command": "PLAYBACK_STEP_FORWARD",
            "handleId": self.strHandleId,
            "requestId": str(uuid.uuid4())
        }
        self.send_command(json.dumps(cmd))

    #--------------------------------------------------------------------------
    def send_backward_command(self):
        cmd = {
            "command": "PLAYBACK_STEP_BACKWARD",
            "handleId": self.strHandleId,
            "requestId": str(uuid.uuid4())
        }
        self.send_command(json.dumps(cmd))

    #--------------------------------------------------------------------------
    def send_stop_command(self):
        cmd = {
            "command": "STOP_MEDIA_STREAM",
            "handleId": self.strHandleId,
            "requestId": str(uuid.uuid4()),
        }
        self.send_command(json.dumps(cmd))
        # 將畫面清空
        self.image_label.clear()
        self.image_label.setText("Waiting for stream...")

        while not self.fragment_queue.empty():
            try:
                self.fragment_queue.get_nowait()
            except:
                break

        while not self.frame_queue.empty():
            try:
                self.frame_queue.get_nowait()
            except:
                break

        if self.decoder_process.is_alive():
            try:
                self.fragment_queue.put(None)
                self.decoder_process.terminate()
                self.decoder_process.join()
            except Exception as e:
                print(f"Error terminating decoder process: {e}")

        # 重建 queues
        self.fragment_queue = multiprocessing.Queue(maxsize=20)
        self.frame_queue = multiprocessing.Queue(maxsize=5)

        # 啟動新的子進程
        self.decoder_process = multiprocessing.Process(
            target=ffmpeg_decode_process,
            args=(self.fragment_queue, self.frame_queue)
        )
        self.decoder_process.daemon = True
        self.decoder_process.start()

    #--------------------------------------------------------------------------
    def send_pause_command(self):
        cmd = {
            "command": "PAUSE_MEDIA_STREAM",
            "handleId": self.strHandleId,
            "requestId": str(uuid.uuid4()),
        }
        self.send_command(json.dumps(cmd))
        return

    #--------------------------------------------------------------------------
    def get_start_time(self):
        return self.start_time_input.text().strip()

    #--------------------------------------------------------------------------
    def get_end_time(self):
        return self.end_time_input.text().strip()

    #--------------------------------------------------------------------------
    def is_valid_fragment(self, fragment: bytes) -> bool:
        if not isinstance(fragment, bytes):
            return False
        if len(fragment) < 12:
            return False
        if b'moof' not in fragment or b'mdat' not in fragment:
            if b'moov' in fragment:
                return True
            return False
        return True

    #--------------------------------------------------------------------------
    def on_stream_data(self, data):
        if isinstance(data, bytes):
            try:
                header_len = int.from_bytes(data[8:10], byteorder='big')
                media_data = data[10 + header_len:]
                if media_data and self.is_valid_fragment(media_data):
                    try:
                        self.fragment_queue.put_nowait(media_data)
                    except queue.Full:
                        _ = self.fragment_queue.get_nowait()
                        self.fragment_queue.put_nowait(media_data)
            except Exception as e:
                print("Stream data error:", e)

    #--------------------------------------------------------------------------
    def update_frame(self):
        if not self.frame_queue.empty():
            try:
                frame = self.frame_queue.get_nowait()
                image = QImage(frame, 640, 360, QImage.Format_RGB888)
                pixmap = QPixmap.fromImage(image)
                self.image_label.setPixmap(pixmap)
            except Exception as e:
                print("Frame update error:", e)

    #--------------------------------------------------------------------------
    def closeEvent(self, event):
        if self.client:
            try:
                self.client.close()
            except:
                pass

        if self.decoder_process.is_alive():
            self.fragment_queue.put(None)
            self.decoder_process.terminate()
            self.decoder_process.join()

        event.accept()

if __name__ == "__main__":
    if sys.platform.startswith('win'):
        # freeze_support for multi-process on windows platform
        multiprocessing.freeze_support()
    app = QApplication(sys.argv)
    player = VideoPlayer()
    player.show()
    sys.exit(app.exec_())
