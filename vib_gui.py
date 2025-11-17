#!/usr/bin/env python3
# vib_gui.py — BLE accel visualizer with logging + playback + separate FFTs
# Packet: [0xAA55][seq u16][base_us u32][n u8][n * (ax,ay,az int16 LE)]

import argparse, asyncio, threading, sys, signal, math, struct, time, csv
import numpy as np
from queue import SimpleQueue, Empty
from dataclasses import dataclass
from pathlib import Path

from bleak import BleakClient, BleakScanner, BleakError

# -------------------- Packet layout --------------------
SYNC_WORD   = 0xAA55
HEADER_FMT  = "<H H I B"           # sync, seq, base_us, n
HEADER_SIZE = struct.calcsize(HEADER_FMT)
SAMPLE_SIZE = 6                    # 3 * int16

# -------------------- Parse stream ---------------------
class StreamParser:
    def __init__(self): self.buf = bytearray()
    def feed(self, data: bytes):
        self.buf.extend(data); out=[]
        while True:
            i = self._find_sync(self.buf)
            if i < 0:
                if len(self.buf) > 1: self.buf = self.buf[-1:]
                break
            if i > 0: del self.buf[:i]
            if len(self.buf) < HEADER_SIZE: break
            sync, seq, base_us, n = struct.unpack_from(HEADER_FMT, self.buf, 0)
            if sync != SYNC_WORD: del self.buf[:2]; continue
            payload_len = n * SAMPLE_SIZE
            frame_len = HEADER_SIZE + payload_len
            if len(self.buf) < frame_len: break
            payload = self.buf[HEADER_SIZE:frame_len]
            del self.buf[:frame_len]
            samples = (np.frombuffer(payload, dtype="<i2").reshape(-1,3)
                       if payload_len else np.empty((0,3), dtype=np.int16))
            out.append((seq, base_us, samples))
        return out
    @staticmethod
    def _find_sync(b: bytearray) -> int:
        for i in range(len(b)-1):
            if b[i]==0x55 and b[i+1]==0xAA: return i
        return -1

# -------------------- BLE helpers ----------------------
async def _get_services_compat(client: BleakClient):
    svcs = getattr(client, "services", None)
    if svcs is not None: return svcs
    get_services = getattr(client, "get_services", None)
    if callable(get_services): return await get_services()
    await asyncio.sleep(0.5)
    svcs = getattr(client, "services", None)
    if svcs is not None: return svcs
    raise RuntimeError("Bleak services API not available")

def _char_has_notify(c) -> bool:
    props = getattr(c, "properties", None)
    if props is None: return True
    if isinstance(props, (list, set, tuple)): return "notify" in {p.lower() for p in props}
    try: return "notify" in str(props).lower()
    except Exception: return False

def _char_has_write(c) -> bool:
    props = getattr(c, "properties", None)
    if props is None: return False
    if isinstance(props, (list, set, tuple)):
        ps = {p.lower() for p in props}
        return ("write" in ps) or ("write-without-response" in ps) or ("write wo resp" in ps)
    try:
        s = str(props).lower()
        return ("write" in s) or ("write-without-response" in s) or ("write wo resp" in s)
    except Exception:
        return False

@dataclass
class BleConfig:
    addr: str | None
    name: str | None
    char_uuid: str | None
    cmd_uuid: str | None = None
    tries: int = 3
    reconnect_delay: float = 1.5

class BleWorker:
    """
    Runs in its own thread; emits PACKETS into q:
    q.put((base_us:int, samples:np.ndarray[int16,(n,3)]))
    """
    def __init__(self, cfg: BleConfig, out_q: SimpleQueue):
        self.cfg = cfg
        self.q = out_q
        self.stop_flag = threading.Event()
        self.thread = threading.Thread(target=self._thread_main, daemon=True)
        self._client: BleakClient | None = None
        self._cmd_uuid: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self): self.thread.start()

    def stop(self):
        # request graceful stop + try to send BYE immediately on the worker loop
        self.stop_flag.set()
        if self._loop is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(self._send_bye_now(), self._loop)
                try: fut.result(timeout=0.5)
                except Exception: pass
            except Exception:
                pass

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        loop.run_until_complete(self._amain())

    async def _pick_device(self):
        devs = await BleakScanner.discover(timeout=2.0)
        if self.cfg.addr:
            for d in devs:
                if d.address and d.address.lower()==self.cfg.addr.lower(): return d
            raise BleakError(f"Address {self.cfg.addr} not found")
        if self.cfg.name:
            for d in devs:
                if d.name and self.cfg.name.lower() in d.name.lower(): return d
            raise BleakError(f"No device matching name '{self.cfg.name}'")
        raise BleakError("Use --addr or --name")

    async def _send_bye_now(self):
        client = self._client
        if not client or not client.is_connected: 
            return
        if self._cmd_uuid:
            try:
                await client.write_gatt_char(self._cmd_uuid, b"stop")
            except Exception:
                pass
            try:
                await client.write_gatt_char(self._cmd_uuid, b"bye")
            except Exception:
                pass
            for _ in range(10):
                if not client.is_connected: break
                await asyncio.sleep(0.05)

    async def _amain(self):
        parser = StreamParser()
        last_seq = None
        while not self.stop_flag.is_set():
            try:
                dev = await self._pick_device()
            except Exception as e:
                print("[ble] discovery:", e); await asyncio.sleep(self.cfg.reconnect_delay); continue

            for attempt in range(1, self.cfg.tries+1):
                if self.stop_flag.is_set(): return
                try:
                    addr = getattr(dev, "address", dev)
                    print(f"[ble] connecting {addr} (try {attempt}/{self.cfg.tries})")
                    try: client = BleakClient(dev, timeout=45.0)
                    except TypeError: client = BleakClient(dev)
                    await client.connect()
                    if not client.is_connected: raise BleakError("Connected=False")
                    self._client = client
                    try:
                        await client.exchange_mtu(247); print("[ble] requested MTU=247")
                    except Exception: pass

                    svcs = await _get_services_compat(client)

                    # Find CMD characteristic (explicit or heuristic)
                    cmd_uuid = (self.cfg.cmd_uuid.lower() if self.cfg.cmd_uuid else None)
                    cmd_found = None
                    for s in svcs:
                        for c in getattr(s, "characteristics", []):
                            cu = getattr(c, "uuid", "") or ""
                            if cmd_uuid and cu.lower() == cmd_uuid:
                                cmd_found = cu
                            elif (not cmd_uuid) and _char_has_write(c):
                                try:
                                    mlen = int(getattr(c, "max_length", 0)) or int(getattr(c, "max_len", 0)) or 0
                                except Exception:
                                    mlen = 0
                                if mlen == 0 or mlen <= 32:
                                    cmd_found = cu
                    if cmd_found:
                        self._cmd_uuid = cmd_found
                        print(f"[ble] command characteristic: {cmd_found}")
                    else:
                        self._cmd_uuid = None

                    parser = StreamParser()   # reset parser for this connection
                    last_seq = None           # reset sequence filter
                    first_packet = asyncio.Event()
                    
                    subs=[]
                    async def on_notify(_uuid, data: bytes):
                        
                        nonlocal last_seq
                        if not first_packet.is_set():
                            first_packet.set() 
                        for seq, base_us, samples in parser.feed(bytes(data)):
                            ok=True
                            if last_seq is not None:
                                fwd=(seq-last_seq)&0xFFFF
                                if fwd==0 or fwd>=0x8000: ok=False
                            if not ok: continue
                            last_seq=seq
                            try: self.q.put_nowait(("live", base_us, samples))
                            except Exception: pass

                    if self.cfg.char_uuid:
                        await client.start_notify(self.cfg.char_uuid, on_notify)
                        subs.append(self.cfg.char_uuid.lower())
                        print(f"[ble] subscribed {self.cfg.char_uuid}")
                    else:
                        n=0
                        for s in svcs:
                            for c in s.characteristics:
                                if _char_has_notify(c):
                                    try:
                                        await client.start_notify(c.uuid, on_notify)
                                        subs.append(c.uuid.lower()); n+=1
                                    except Exception as e:
                                        print(f"  ! subscribe {c.uuid}: {e}")
                        if n==0: raise BleakError("No notify characteristics found")
                        print(f"[ble] subscribed to {n} notify char(s)")

                    # If firmware expects an explicit start, trigger streaming now
                    if self._cmd_uuid:
                        try:
                            await client.write_gatt_char(self._cmd_uuid, b"start")
                        except Exception as e:
                            print("[ble] start command failed:", e)
                    try:
                        await asyncio.wait_for(first_packet.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        print("[ble] no notifications yet; retrying start…")
                        if self._cmd_uuid:
                            try:
                                await client.write_gatt_char(self._cmd_uuid, b"start")
                            except Exception as e:
                                print("[ble] start command failed (retry):", e)
                        try:
                            await asyncio.wait_for(first_packet.wait(), timeout=2.0)
                        except asyncio.TimeoutError:
                            raise BleakError("Notify active but no data; giving up this attempt")

                    while not self.stop_flag.is_set() and client.is_connected:
                        await asyncio.sleep(0.2)

                    # Graceful BYE if we are stopping and still connected
                    if self.stop_flag.is_set() and client.is_connected:
                        try:
                            await self._send_bye_now()
                        except Exception:
                            pass
                    
                    await asyncio.sleep(0.1)

                    try:
                        for u in subs:
                            try: await client.stop_notify(u)
                            except Exception: pass
                        await client.disconnect()
                    except Exception:
                        pass
                    finally:
                        self._client = None

                    if self.stop_flag.is_set(): return
                except Exception as e:
                    print("[ble] connect error:", e); await asyncio.sleep(1.0)

            if self.stop_flag.is_set(): return
            print(f"[ble] reconnecting in {self.cfg.reconnect_delay:.1f}s…")
            await asyncio.sleep(self.cfg.reconnect_delay)

# -------------------- GUI ------------------------------
from pyqtgraph.Qt import QtWidgets, QtCore
import pyqtgraph as pg

class Ring:
    """Fixed-size ring for t + (ax,ay,az) in float64 g."""
    def __init__(self, capacity:int):
        self.N=capacity
        self.t  = np.full(self.N, np.nan, float)
        self.ax = np.full(self.N, np.nan, float)
        self.ay = np.full(self.N, np.nan, float)
        self.az = np.full(self.N, np.nan, float)
        self.i=0; self.full=False
    def append_block(self, t: np.ndarray, g: np.ndarray):
        n=len(t); 
        if n==0: return
        n1=min(n, self.N-self.i); s1=slice(self.i, self.i+n1)
        self.t[s1]=t[:n1]; self.ax[s1]=g[:n1,0]; self.ay[s1]=g[:n1,1]; self.az[s1]=g[:n1,2]
        self.i=(self.i+n1)%self.N
        n2=n-n1
        if n2>0:
            s2=slice(self.i, self.i+n2)
            self.t[s2]=t[n1:]; self.ax[s2]=g[n1:,0]; self.ay[s2]=g[n1:,1]; self.az[s2]=g[n1:,2]
            self.i=(self.i+n2)%self.N
        if n>=self.N or self.i==0: self.full=True
    def view(self):
        if not self.full:
            sl=slice(0,self.i)
            return self.t[sl], self.ax[sl], self.ay[sl], self.az[sl]
        t = np.r_[self.t[self.i:],  self.t[:self.i]]
        x = np.r_[self.ax[self.i:], self.ax[:self.i]]
        y = np.r_[self.ay[self.i:], self.ay[:self.i]]
        z = np.r_[self.az[self.i:], self.az[:self.i]]
        return t,x,y,z

class SampleRateEMA:
    """Second-by-second EMA of observed sample rate (for display/FFT fallback)."""
    def __init__(self, init_hz:float, alpha:float=0.9):
        self.alpha=alpha; self.hz=float(init_hz)
        self._last_sec=int(time.monotonic()); self._count=0
    def tick(self, n=1):
        t=int(time.monotonic())
        if t!=self._last_sec:
            inst=self._count/max(1,t-self._last_sec)
            self.hz=self.alpha*self.hz+(1-self.alpha)*inst
            self._last_sec=t; self._count=0
        self._count+=n; return self.hz

class PlaybackIngest(threading.Thread):
    """Reads CSV logs (t,ax,ay,az), re-emits as PACKETS (monotonic chunks)."""
    def __init__(self, filepath: Path, out_q: SimpleQueue, chunk: int = 20):
        super().__init__(daemon=True)
        self.path=Path(filepath); self.q=out_q
        self.stop_flag=threading.Event()
        self.pause_flag=threading.Event()
        self.speed=1.0; self.chunk=chunk
    def set_speed(self, s: float): self.speed=max(0.01,float(s))
    def pause(self, paused: bool):
        if paused: self.pause_flag.set()
        else: self.pause_flag.clear()
    def run(self):
        try:
            rows=[]
            with self.path.open('r', newline='') as f:
                rdr=csv.reader(r for r in f if not r.startswith('#'))
                for r in rdr:
                    if len(r)>=4:
                        rows.append((float(r[0]), int(r[1]), int(r[2]), int(r[3])))
            if len(rows)<2: return
            t0=rows[0][0]; wall0=time.monotonic()
            i=0
            while i < len(rows) and not self.stop_flag.is_set():
                while self.pause_flag.is_set() and not self.stop_flag.is_set():
                    time.sleep(0.05)
                j=min(len(rows), i+self.chunk)
                seg = rows[i:j]
                base_us=int((seg[0][0]-t0)*1e6)
                arr=np.empty((len(seg),3),dtype=np.int16)
                for k,(t_rel, ax, ay, az) in enumerate(seg):
                    arr[k,0]=ax; arr[k,1]=ay; arr[k,2]=az
                try: self.q.put_nowait(("playback", base_us, arr))
                except Exception: pass
                i=j
                target=(rows[i-1][0]-t0)/self.speed
                while not self.stop_flag.is_set():
                    elapsed=time.monotonic()-wall0
                    dt=target-elapsed
                    if dt<=0: break
                    time.sleep(min(0.02, dt))
        except Exception:
            return
    def stop(self): self.stop_flag.set()

class VibGui(QtWidgets.QMainWindow):
    def __init__(self, q: SimpleQueue, odr_hz: float, range_g: int, history_s: float, buffer_s: float,
                 use_hpf: bool, remove_mean: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("BLE Vibration — Live / Log / Playback")
        self.resize(1400, 980)
        self.q = q

        # --- dynamic GUI sampling rate (Hz), starts from nominal ODR ---
        self.fs_gui = odr_hz if (odr_hz and odr_hz > 0) else 400.0
        self.period_us = 1e6 / self.fs_gui

        self.sens = {2:0.061e-3, 4:0.122e-3, 8:0.244e-3, 16:0.488e-3}.get(int(range_g), 0.244e-3)
        self.t0_dev_us = None

        # store scrollback length (seconds retained in RAM)
        self.buffer_s = float(buffer_s)

        # use fs_gui to size the ring buffer
        plot_fs = self.fs_gui
        cap = int(max(2048, math.ceil(buffer_s * plot_fs)))
        self.ring = Ring(cap)
        self.history_s = history_s
        self.use_hpf = use_hpf
        self.remove_mean = remove_mean

        # EMA of *measured* sampling rate
        self.sps = SampleRateEMA(init_hz=self.fs_gui)
        self.playback: PlaybackIngest | None = None
        self.fft_secs = 2.0  # seconds of data for FFT (fixed window)
        self.logging = False
        self.log_fp=None; self.log_writer=None; self.log_t0=None
        self.mode = "live"  # or "playback"

        # ---- Top bar ----
        top = QtWidgets.QHBoxLayout()
        self.btn_live = QtWidgets.QPushButton("Back to Live")
        self.btn_open = QtWidgets.QPushButton("Open Log…")
        self.btn_play = QtWidgets.QPushButton("▶ Play"); self.btn_play.setCheckable(True); self.btn_play.setEnabled(False)
        self.cmb_speed = QtWidgets.QComboBox()
        self.cmb_speed.addItems(["0.25×", "0.5×", "1×", "2×", "4×", "16×", "Max"])
        self.cmb_speed.setCurrentText("1×")
        self.btn_log = QtWidgets.QPushButton("● Start Logging"); self.btn_log.setCheckable(True)
        self.chk_hpf = QtWidgets.QCheckBox("High-pass"); self.chk_hpf.setChecked(self.use_hpf)
        self.chk_mean = QtWidgets.QCheckBox("De-mean"); self.chk_mean.setChecked(self.remove_mean)
        self.lbl_status = QtWidgets.QLabel("Status: idle")
        for w in (self.btn_live, self.btn_open, self.btn_play, QtWidgets.QLabel("Speed:"), self.cmb_speed,
                  self.btn_log, self.chk_hpf, self.chk_mean):
            top.addWidget(w)
        top.addStretch(1); top.addWidget(self.lbl_status)

        # ---- Plots ----
        pg.setConfigOptions(antialias=False, background='k', foreground='w')
        win = QtWidgets.QWidget(); self.setCentralWidget(win)
        root = QtWidgets.QVBoxLayout(win); root.addLayout(top)

        self.time_plot = pg.PlotWidget(title="Acceleration (g)")
        self.time_plot.addLegend(); self.time_plot.setClipToView(True)
        self.time_plot.setDownsampling(mode='peak'); self.time_plot.showGrid(x=True,y=True,alpha=0.3)
        self.cur_tx = self.time_plot.plot(pen=pg.mkPen((240,80,80), width=1), name='ax')
        self.cur_ty = self.time_plot.plot(pen=pg.mkPen((80,200,80), width=1), name='ay')
        self.cur_tz = self.time_plot.plot(pen=pg.mkPen((80,140,240), width=1), name='az')
        root.addWidget(self.time_plot, 2)

        # --- Time scroll bar (for navigating history / playback) ---
        scroll_layout = QtWidgets.QHBoxLayout()
        scroll_layout.addWidget(QtWidgets.QLabel("Scroll:"))
        self.scroll_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.scroll_slider.setRange(0, 1000)
        self.scroll_slider.setValue(1000)
        self.scroll_slider.setEnabled(False)  # enabled in playback
        scroll_layout.addWidget(self.scroll_slider, 1)
        root.addLayout(scroll_layout)

        self.scroll_follow_tail = True  # when True we follow latest data

        self.scroll_slider.valueChanged.connect(self._on_scroll_changed)

        grid = QtWidgets.QGridLayout(); root.addLayout(grid, 3)
        self.fft_x = pg.PlotWidget(title="FFT X (normalized)"); self.fft_x.showGrid(x=True,y=True,alpha=0.3)
        self.fft_y = pg.PlotWidget(title="FFT Y (normalized)"); self.fft_y.showGrid(x=True,y=True,alpha=0.3)
        self.fft_z = pg.PlotWidget(title="FFT Z (normalized)"); self.fft_z.showGrid(x=True,y=True,alpha=0.3)
        self.fft_m = pg.PlotWidget(title="FFT |a| (normalized)"); self.fft_m.showGrid(x=True,y=True,alpha=0.3)
        self.cur_fx = self.fft_x.plot(pen=pg.mkPen((240,80,80), width=1))
        self.cur_fy = self.fft_y.plot(pen=pg.mkPen((80,200,80), width=1))
        self.cur_fz = self.fft_z.plot(pen=pg.mkPen((80,140,240), width=1))
        self.cur_fm = self.fft_m.plot(pen=pg.mkPen(200,200,200))
        grid.addWidget(self.fft_x, 0, 0); grid.addWidget(self.fft_y, 0, 1)
        grid.addWidget(self.fft_z, 1, 0); grid.addWidget(self.fft_m, 1, 1)

        # ---- Signals ----
        self.btn_open.clicked.connect(self._on_open_log)
        self.btn_play.toggled.connect(self._on_toggle_play)
        self.cmb_speed.currentTextChanged.connect(self._on_speed)
        self.btn_live.clicked.connect(self._back_to_live)
        self.btn_log.toggled.connect(self._on_toggle_log)
        self.chk_hpf.toggled.connect(lambda v: setattr(self, "use_hpf", bool(v)))
        self.chk_mean.toggled.connect(lambda v: setattr(self, "remove_mean", bool(v)))

        # ---- Timers ----
        self.feed_timer = QtCore.QTimer(self); self.feed_timer.timeout.connect(self._drain_queue); self.feed_timer.start(10)
        self.plot_timer = QtCore.QTimer(self); self.plot_timer.timeout.connect(self._refresh_plots); self.plot_timer.start(30)

    # ---------- Logging ----------
    def _on_toggle_log(self, checked: bool):
        if checked:
            default = Path("logs"); default.mkdir(parents=True, exist_ok=True)
            suggested = default / time.strftime("vib_%Y%m%d_%H%M%S.csv")
            path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save log as…", str(suggested), "CSV (*.csv)")
            if not path: self.btn_log.setChecked(False); return
            try:
                self.log_fp = open(path, 'w', newline='', buffering=1)  # line-buffered
                self.log_writer = csv.writer(self.log_fp)
                self.log_fp.write("# t,ax,ay,az (t in seconds since start)\n")
                self.log_t0=None; self.logging=True; self.btn_log.setText("■ Stop Logging")
                self.lbl_status.setText(f"Status: logging → {Path(path).name}")
            except Exception as e:
                QtWidgets.QMessageBox.critical(self,"Error",f"Cannot open file:\n{e}")
                self.btn_log.setChecked(False)
        else:
            self._stop_log()

    def _on_scroll_changed(self, value: int):
        # ignore programmatic changes while disabled
        if not self.scroll_slider.isEnabled():
            return

        # if user moves away from max, stop following the tail
        if value < self.scroll_slider.maximum():
            self.scroll_follow_tail = False
        else:
            self.scroll_follow_tail = True

        self._update_time_view_from_slider()

    def _update_time_view_from_slider(self):
        t, *_ = self.ring.view()
        if not t.size:
            return

        t_start = t[0]
        t_end   = t[-1]
        if not np.isfinite(t_start) or not np.isfinite(t_end):
            return

        span = max(0.0, float(t_end - t_start))
        win  = float(self.history_s)
        if span <= 0:
            return

        # If history shorter than window, or we're following tail, or slider disabled:
        # just show tail like before.
        if span <= win or self.scroll_follow_tail or not self.scroll_slider.isEnabled():
            tmax = t_end
            tmin = max(t_start, tmax - win)
        else:
            frac = self.scroll_slider.value() / self.scroll_slider.maximum()
            frac = min(max(frac, 0.0), 1.0)
            # left edge slides between earliest and (end - window)
            tmin = t_start + frac * max(0.0, span - win)
            tmax = tmin + win

        self.time_plot.setXRange(tmin, tmax, padding=0.0)

    
    def _purge_queue(self):
    # dump any leftover packets (live or playback) so mode switches are clean
        while True:
            try:
                _ = self.q.get_nowait()
            except Empty:
                break

    def _stop_log(self):
        if self.log_fp:
            try: self.log_fp.flush(); self.log_fp.close()
            except Exception: pass
        self.log_fp=None; self.log_writer=None; self.log_t0=None; self.logging=False
        self.btn_log.setText("● Start Logging")

    # ---------- Playback ----------
    def _on_open_log(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open log…", "logs", "CSV (*.csv)")
        if not path: return
        self._start_playback(Path(path))

    def _start_playback(self, filepath: Path):
        # stop any existing playback first
        self._stop_playback()

        # --- CLEAN SWITCH INTO PLAYBACK ---
        self._purge_queue()          # dump any leftover live packets
        self.t0_dev_us = None        # reset device epoch so playback starts at t=0
        # start with a fresh ring buffer sized for plotting
        fs = (1e6 / self.period_us) if self.period_us else max(1.0, self.sps.hz)
        cap = max(2048, int(self.buffer_s * fs))
        self.ring = Ring(cap)

        self.mode = "playback"

        # start the playback worker
        self.playback = PlaybackIngest(filepath, self.q)
        self.playback.set_speed(self._speed_value())
        self.playback.start()

        # UI
        self.btn_play.setEnabled(True)
        self.btn_play.setChecked(True)
        self.btn_play.setText("⏸ Pause")
        self.lbl_status.setText(f"Status: playback {filepath.name}")

        # enable scroll bar for playback navigation
        self.scroll_slider.setEnabled(True)
        self.scroll_follow_tail = True
        self.scroll_slider.setValue(self.scroll_slider.maximum())


    def _stop_playback(self):
        if self.playback:
            try:
                self.playback.stop()
            except Exception:
                pass
        self.playback = None
        self.btn_play.setEnabled(False)
        self.btn_play.setChecked(False)
        self.btn_play.setText("▶ Play")
        self.scroll_follow_tail = True

    def _on_toggle_play(self, checked: bool):
        if not self.playback:
            self.btn_play.setChecked(False)
            return
        self.playback.pause(not checked)
        self.btn_play.setText("⏸ Pause" if checked else "▶ Play")

        if checked:
            # when (re)starting playback, snap back to the tail
            self.scroll_follow_tail = True
            if self.scroll_slider.isEnabled():
                self.scroll_slider.blockSignals(True)
                self.scroll_slider.setValue(self.scroll_slider.maximum())
                self.scroll_slider.blockSignals(False)


    def _on_speed(self, _):
        sp=self._speed_value()
        if self.playback: self.playback.set_speed(sp)

    def _speed_value(self) -> float:
        txt = self.cmb_speed.currentText()
        if "Max" in txt:
            # effectively "no timing", ingest as fast as possible
            return 1e9
        try:
            return float(txt.replace("×", ""))
        except Exception:
            return 1.0


    def _back_to_live(self):
        # stop playback first
        self._stop_playback()

        # --- CLEAN SWITCH BACK TO LIVE ---
        self._purge_queue()          # drop any leftover playback packets
        self.t0_dev_us = None        # next live packet will re-seed time
        fs = (1e6 / self.period_us) if self.period_us else max(1.0, self.sps.hz)
        cap = max(2048, int(self.buffer_s * fs))
        self.ring = Ring(cap)
        self.mode = "live"

        # disable scrollbar in live mode (we always follow the tail)
        self.scroll_slider.setEnabled(False)
        self.scroll_follow_tail = True
        self.scroll_slider.setValue(self.scroll_slider.maximum())

        self.lbl_status.setText("Status: live (BLE)")

    # ---------- Data ingest & plotting ----------
    def _drain_queue(self):
        drained = 0
        while True:
            try:
                item = self.q.get_nowait()
            except Empty:
                break

            # Accept both old (untagged) and new (tagged) tuples
            if isinstance(item, tuple) and len(item) == 3 and isinstance(item[0], str):
                source, base_us, samples = item
            else:
                # Back-compat: if untagged, assume it's from the *current* mode
                source, (base_us, samples) = self.mode, item

            # Drop packets from the other source
            if (self.mode == "live" and source != "live") or (self.mode == "playback" and source != "playback"):
                continue

            if samples.size == 0:
                continue

            drained += len(samples)

            # seed device epoch if needed
            if self.t0_dev_us is None:
                self.t0_dev_us = base_us

            # build timestamps for this block
            if self.period_us:
                t0 = (base_us - self.t0_dev_us) / 1e6
                ts = t0 + (np.arange(samples.shape[0]) * (self.period_us / 1e6))
            else:
                ts = np.full(samples.shape[0], (base_us - self.t0_dev_us) / 1e6, dtype=float)

            # --- LOGGING: live mode only ---
            if self.mode == "live" and self.logging and self.log_writer is not None:
                # ts was just computed above for this block
                if self.log_t0 is None and ts.size:
                    # anchor log time to the first sample we log
                    self.log_t0 = ts[0]

                if self.log_t0 is not None:
                    for ti, si in zip(ts, samples):
                        t_rel = ti - self.log_t0
                        self.log_writer.writerow([
                            f"{t_rel:.6f}",
                            int(si[0]), int(si[1]), int(si[2]),
                        ])

                # make sure data hits disk regularly so logging appears to “start” immediately
                if self.log_fp:
                    try:
                        self.log_fp.flush()
                    except Exception:
                        pass

            # scale to g and append to ring buffer
            g = samples.astype(np.float64) * self.sens
            self.ring.append_block(ts, g)

        # UI updates
        if drained:
            if self.mode == "live":
                hz = self.sps.tick(drained)

                # adapt GUI fs to measured rate (guard against garbage / zero)
                if hz > 10.0:
                    self.fs_gui = hz
                    self.period_us = 1e6 / self.fs_gui

                self.lbl_status.setText(f"Status: live (BLE)  fs≈{hz:.0f} Hz")
            # playback: keep its own status label

            # if we're following the tail in playback, keep slider at the end
            if self.scroll_follow_tail and self.scroll_slider.isEnabled():
                self.scroll_slider.blockSignals(True)
                self.scroll_slider.setValue(self.scroll_slider.maximum())
                self.scroll_slider.blockSignals(False)

            # compute and apply X-range based on scroll state
            self._update_time_view_from_slider()

    def _refresh_plots(self):
        t, x, y, z = self.ring.view()
        if not t.size:
            for c in (self.cur_fx, self.cur_fy, self.cur_fz, self.cur_fm):
                c.setData([], [])
            return

        def _proc(sig):
            arr = sig
            if self.remove_mean and arr.size:
                arr = arr - np.mean(arr)
            if self.use_hpf and arr.size > 1:
                alpha = 0.995
                y_hp = np.zeros_like(arr)
                for i in range(1, arr.size):
                    y_hp[i] = alpha * (y_hp[i - 1] + arr[i] - arr[i - 1])
                arr = y_hp
            return arr

        # --- determine visible time window from current plot view / slider ---
        try:
            tmin, tmax = self.time_plot.viewRange()[0]  # (xMin, xMax)
        except Exception:
            tmin, tmax = float("nan"), float("nan")

        # if viewRange is invalid, fall back to last history_s seconds
        if (not np.isfinite(tmin)) or (not np.isfinite(tmax)) or (tmax <= tmin):
            t_end = t[-1]
            t_start = t[0]
            tmax = t_end
            tmin = max(t_start, tmax - self.history_s)

        # indices for current visible time window
        i0 = np.searchsorted(t, tmin, side="left")
        i1 = np.searchsorted(t, tmax, side="right")

        tv = t[i0:i1]
        xv = x[i0:i1]
        yv = y[i0:i1]
        zv = z[i0:i1]

        # ----------------- Time-domain plot -----------------
        if tv.size == 0:
            # nothing in view; clear time plots
            self.cur_tx.setData([], [], _callSync='off')
            self.cur_ty.setData([], [], _callSync='off')
            self.cur_tz.setData([], [], _callSync='off')
        else:
            self.cur_tx.setData(tv, _proc(xv), _callSync='off')
            self.cur_ty.setData(tv, _proc(yv), _callSync='off')
            self.cur_tz.setData(tv, _proc(zv), _callSync='off')

        # ----------------- FFT (based on visible window) -----------------
        max_fft = 4096

        # Choose source for FFT: visible window if non-empty, else whole buffer
        if tv.size >= 2:
            src_t, src_x, src_y, src_z = tv, xv, yv, zv
        else:
            src_t, src_x, src_y, src_z = t, x, y, z

        # compute sampling rate (fs)
        if self.period_us:
            fs = 1e6 / self.period_us
        else:
            dt = np.median(np.diff(src_t[-min(len(src_t), 512):]))
            fs = 1.0 / dt if dt > 0 else max(1.0, self.sps.hz)

        # choose FFT size from desired seconds, nearest lower power of two, and clamp
        n_target = int(max(256, fs * self.fft_secs))
        n = 1 << (n_target.bit_length() - 1)
        n = min(n, max_fft)

        if len(src_x) < n or n < 256:
            # visible window too short for a decent FFT
            for c in (self.cur_fx, self.cur_fy, self.cur_fz, self.cur_fm):
                c.setData([], [])
            return

        tx_sig = src_x[-n:]
        ty_sig = src_y[-n:]
        tz_sig = src_z[-n:]

        win = np.hanning(n)

        def fft_norm(sig):
            sigv = sig.copy()
            if self.remove_mean:
                sigv -= np.mean(sigv)
            if self.use_hpf and sigv.size > 1:
                alpha = 0.995
                y_hp = np.zeros_like(sigv)
                for i in range(1, sigv.size):
                    y_hp[i] = alpha * (y_hp[i - 1] + sigv[i] - sigv[i - 1])
                sigv = y_hp
            spec = np.fft.rfft(sigv * win)
            mag = np.abs(spec)
            m = np.max(mag)
            return (mag / m) if m > 0 else mag

        fx = fft_norm(tx_sig)
        fy = fft_norm(ty_sig)
        fz = fft_norm(tz_sig)
        fmag = np.sqrt(fx**2 + fy**2 + fz**2)
        fm = np.max(fmag)
        fmag = (fmag / fm) if fm > 0 else fmag

        freqs = np.fft.rfftfreq(n, d=1.0 / fs)

        # --- display only a "safe" band up to 200 Hz (or Nyquist, whichever is lower) ---
        max_display = min(200.0, fs / 2.0)
        mask = freqs <= max_display

        freqs_disp = freqs[mask]
        fx_disp    = fx[mask]
        fy_disp    = fy[mask]
        fz_disp    = fz[mask]
        fmag_disp  = fmag[mask]

        self.cur_fx.setData(freqs_disp, fx_disp, _callSync='off')
        self.cur_fy.setData(freqs_disp, fy_disp, _callSync='off')
        self.cur_fz.setData(freqs_disp, fz_disp, _callSync='off')
        self.cur_fm.setData(freqs_disp, fmag_disp, _callSync='off')

        # lock X-axis so it doesn't jump around with small fs changes
        for pw in (self.fft_x, self.fft_y, self.fft_z, self.fft_m):
            pw.setXRange(0, max_display, padding=0)


# -------------------- Main -----------------------------
def main():
    ap = argparse.ArgumentParser(description="BLE accel visualizer (live/log/playback) with separate FFTs")
    ap.add_argument("--addr")
    ap.add_argument("--name", default="XIAO-ACCEL")
    ap.add_argument("--char", help="Notify characteristic UUID (optional)")
    ap.add_argument("--cmd",  help="Command characteristic UUID (optional)")
    ap.add_argument("--odr", type=float, default=833, help="Accel ODR [Hz] for timing")
    ap.add_argument("--range-g", type=int, default=8, choices=[2,4,8,16], help="Accel range (counts->g)")
    ap.add_argument("--history", type=float, default=8.0, help="Visible time window seconds")
    ap.add_argument("--buffer",  type=float, default=600.0, help="Seconds retained in RAM (scrollback)")
    ap.add_argument("--hpf", action="store_true", help="Enable view high-pass filter")
    ap.add_argument("--demean", action="store_true", help="Remove mean in view")
    args = ap.parse_args()

    # Windows event loop quirk
    if sys.platform.startswith("win"):
        try:
            import asyncio as _a
            _a.set_event_loop_policy(_a.WindowsSelectorEventLoopPolicy())  # type: ignore
        except Exception:
            pass

    q = SimpleQueue()
    cfg = BleConfig(addr=args.addr,
                    name=args.name if not args.addr else None,
                    char_uuid=args.char,
                    cmd_uuid=args.cmd)
    worker = BleWorker(cfg, q); worker.start()

    from pyqtgraph.Qt import QtWidgets, QtCore
    import pyqtgraph as pg
    app = QtWidgets.QApplication(sys.argv)
    pg.setConfigOptions(antialias=False, background='k', foreground='w')
    gui = VibGui(q, odr_hz=args.odr, range_g=args.range_g,
                 history_s=args.history, buffer_s=args.buffer,
                 use_hpf=args.hpf, remove_mean=args.demean)
    gui.show()

    def _stop():
        # ask the worker to send BYE immediately, then quit shortly after
        worker.stop()
        if gui.playback: gui._stop_playback()
        gui._stop_log()
        QtWidgets.QApplication.processEvents()
        QtCore.QTimer.singleShot(400, QtWidgets.QApplication.quit)

    signal.signal(signal.SIGINT, lambda *_: _stop())
    signal.signal(signal.SIGTERM, lambda *_: _stop())

    # Ensure window-close also triggers graceful BYE
    def _on_close_event(ev):
        _stop()
        ev.accept()
    gui.closeEvent = _on_close_event  # type: ignore

    ret = app.exec_()
    worker.stop()
    sys.exit(ret)

if __name__ == "__main__":
    main()
