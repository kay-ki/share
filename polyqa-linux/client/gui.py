import base64
import tempfile
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThread, QThreadPool, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFormLayout, QFrame,
                               QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget)

import settings
import transport

STYLE = """
* { font-family: "Aptos", "Segoe UI", "Helvetica Neue", sans-serif; }
QWidget { background: #fafbf8; color: #10242e; font-size: 14px; }
QLabel { background: transparent; }
QLabel#title { font-size: 21px; font-weight: 600; }
QLabel#how, QLabel#muted { color: #7d939d; font-size: 13px; }
QLabel#hint { color: #7d939d; font-size: 12px; }
QLabel#paneLabel { font-size: 13px; font-weight: 600; }
QLabel#gaugeName { color: #4a6270; font-size: 13px; }
QLabel#gaugeValue { font-size: 16px; }
QLabel#progress { color: #4a6270; font-size: 13px; }
QPushButton { background: #fafbf8; border: 1px solid #c7d1cc; padding: 6px 12px; color: #10242e; }
QPushButton:hover { border-color: #7d939d; }
QPushButton:disabled { color: #a9b7bd; border-color: #dde4df; }
QPushButton#go { background: #1f6273; border: 1px solid #1f6273; color: #ffffff;
                 font-weight: 600; padding: 9px 20px; }
QPushButton#go:hover { background: #17505f; }
QPushButton#go:disabled { background: #b9c6ca; border-color: #b9c6ca; color: #eef2f0; }
QPushButton#quiet { background: transparent; border: none; color: #1f6273;
                    text-decoration: underline; padding: 6px 2px; }
QPushButton#quiet:disabled { color: #a9b7bd; }
QWidget#rail { background: #f3f5f1; }
QWidget#row:hover { background: #e9ede8; }
QWidget#rowActive { background: #e1e9ea; }
QLabel#folderName { font-size: 14px; font-weight: 600; }
QLabel#folderDim { font-size: 14px; color: #4a6270; }
QLabel#stepName { font-size: 13.5px; color: #4a6270; }
QLabel#stepDone { font-size: 13.5px; color: #93a5ac; }
QLabel#stepActive { font-size: 13.5px; color: #10242e; font-weight: 600; }
QLabel#flag { color: #ab1f68; }
QLineEdit { border: 1px solid #c7d1cc; padding: 5px; background: #ffffff; }
QGraphicsView { border: none; background: #eef1ed; }
QFrame#sep { background: #dde4df; }
"""


class Row(QWidget):
    clicked = Signal()

    def __init__(self, left, right, left_style, right_style, indent):
        super().__init__()
        self.setObjectName("row")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12 + indent, 5, 12, 5)
        lay.setSpacing(8)
        a, b = QLabel(left), QLabel(right)
        a.setObjectName(left_style)
        b.setObjectName(right_style)
        lay.addWidget(a)
        lay.addStretch(1)
        lay.addWidget(b)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event):
        self.clicked.emit()


class Pane(QGraphicsView):
    zoomed = Signal(float)

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.item = QGraphicsPixmapItem()
        self.scene().addItem(self.item)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setMinimumHeight(300)

    def show_image(self, path):
        pix = QPixmap(str(path)) if path else QPixmap()
        self.item.setPixmap(pix)
        self.setSceneRect(self.item.boundingRect())
        if not pix.isNull():
            self.fitInView(self.item, Qt.KeepAspectRatio)

    def wheelEvent(self, event):
        self.zoomed.emit(1.15 if event.angleDelta().y() > 0 else 1 / 1.15)


class CallSignals(QObject):
    done = Signal(int, str, object, str)


class CallJob(QRunnable):
    def __init__(self, token, kind, cfg, command, signals, **extra):
        super().__init__()
        self.token, self.kind, self.cfg = token, kind, cfg
        self.command, self.signals, self.extra = command, signals, extra

    @Slot()
    def run(self):
        try:
            self.signals.done.emit(self.token, self.kind,
                                   transport.call(self.cfg, self.command, **self.extra), "")
        except Exception as exc:
            self.signals.done.emit(self.token, self.kind, None, str(exc))


POLL_MS = 3000


class Worker(QObject):
    step_done = Signal(str, bool)
    progress = Signal(str)
    failed = Signal(str, str)
    finished = Signal()

    def __init__(self, cfg, folder, steps, labels, fresh, attach):
        super().__init__()
        self.cfg, self.folder, self.steps, self.labels = cfg, folder, steps, labels
        self.fresh, self.attach = fresh, attach

    @Slot()
    def run(self):
        seen = 0
        try:
            if not self.attach:
                transport.call(self.cfg, "start", folder=self.folder, step=",".join(self.steps),
                               fresh=self.fresh)
            while True:
                job = transport.call(self.cfg, "status", folder=self.folder)["job"]
                for step in job["done"][seen:]:
                    self.step_done.emit(step, step == job["flag"])
                seen = len(job["done"])
                if job["running"] and job["step"] and job["total"]:
                    self.progress.emit(f"{self.labels[job['step']]}: {job['stem']} "
                                       f"({job['index']} von {job['total']})")
                if job["error"]:
                    text = "\n\n".join(t for t in (job["error"], job.get("trace")) if t)
                    settings.log(f"{self.folder} {job['step']}: {text}")
                    self.failed.emit(job["step"] or "", text)
                    return
                if job["broken"]:
                    settings.log(f"{self.folder} {job['step']}: Rechnung auf dem Server abgebrochen "
                                 f"(Ausgabe in polyqa_work/{self.folder}/job.log)")
                    self.failed.emit(job["step"] or "", "angehalten oder abgebrochen – „Ordner rechnen“ "
                                                         "macht beim nächsten offenen Raster weiter")
                    return
                if not job["running"]:
                    break
                QThread.msleep(POLL_MS)
        except Exception as exc:
            self.failed.emit("", str(exc))
            return
        self.finished.emit()


def add_field(form, label, text, hint, width):
    edit = QLineEdit(text)
    edit.setMinimumWidth(width)
    note = QLabel(hint)
    note.setObjectName("hint")
    note.setWordWrap(True)
    note.setFixedWidth(width)
    note.setFixedHeight(note.heightForWidth(width))
    box = QVBoxLayout()
    box.setContentsMargins(0, 0, 0, 6)
    box.setSpacing(2)
    box.addWidget(edit)
    box.addWidget(note)
    holder = QWidget()
    holder.setLayout(box)
    form.addRow(label, holder)
    return edit


class Connection(QDialog):
    FIELDS = [("ssh_host", "Server",
               "benutzer@host oder ein Name aus ~/.ssh/config. Leer: polyqa läuft selbst auf dem "
               "Server und rechnet ohne ssh."),
              ("ssh_port", "Port", "Leer für 22."),
              ("ssh_key", "Schlüsseldatei",
               "Leer: ssh nimmt ~/.ssh/id_ed25519, id_rsa oder id_ecdsa. Heißt der Schlüssel anders, "
               "hier den vollen Pfad eintragen. Der Schlüssel darf keine Passphrase haben."),
              ("ssh_program", "ssh-Programm",
               "Leer: das ssh von Windows, fehlt es, das mitgelieferte im Ordner openssh. "
               "Sonst vollen Pfad zu ssh.exe eintragen."),
              ("server_python", "Python auf dem Server",
               "python3, wenn die Pakete installiert sind. Ohne Installation: "
               "/opt/polyqa/server/python3-polyqa."),
              ("server_script", "polyqa_server.py",
               "Pfad auf dem Server, z. B. /opt/polyqa/polyqa_server.py."),
              ("data_root", "Datenordner",
               "Auf dem Server. Jeder Unterordner mit .tif-Dateien wird links ein Eintrag."),
              ("out_root", "Ausgabeordner",
               "Auf dem Server. Leer für polyqa_out im Datenordner.")]

    def __init__(self, cfg, parent):
        super().__init__(parent)
        self.setWindowTitle("Verbindung")
        self.cfg = cfg
        form = QFormLayout(self)
        self.edits = {}
        for key, label, hint in self.FIELDS:
            self.edits[key] = add_field(form, label, str(cfg[key] or ""), hint, 420)
        self.note = QLabel("")
        self.note.setObjectName("muted")
        self.note.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow(self.note)
        test = QPushButton("Verbindung testen")
        test.clicked.connect(self.test)
        form.addRow(test)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return {**self.cfg, **{key: self.edits[key].text().strip() for key, _, _ in self.FIELDS}}

    def test(self):
        self.note.setText("verbinde …")
        QApplication.processEvents()
        try:
            found = transport.call(self.values(), "discover")["folders"]
            self.note.setText(f"Verbindung steht, {len(found)} Ordner gefunden")
        except Exception as exc:
            self.note.setText(f"{first_line(str(exc))}\nganzer Fehler in {settings.log_path()}")


class Parameters(QDialog):
    FIELDS = [("target_res", "Zielauflösung in Meter",
               "Pixelgröße nach dem Downscale. Bisher 1 m. Gröber rechnet schneller, macht die "
               "Umrisse aber stufiger. Feinere Raster werden nie hochgerechnet."),
              ("min_hole_area", "Löcher schließen unter … m²",
               "Kleinere Löcher werden zugemacht, 4000 m² sind etwa 63 × 63 m. Prüfen bei "
               "„Löcher schließen“: Bleiben pixelige Mini-Löcher, Wert erhöhen; verschwinden echte "
               "Lücken, Wert senken."),
              ("max_deviation", "Größte Abweichung in Meter",
               "Wie weit die geglättete Linie höchstens vom Pixelumriss abweichen darf, bisher 3 m. "
               "Der Douglas-Wert wird je Ring automatisch in diesem Rahmen gesucht."),
              ("chaikin_iterations", "Chaikin-Durchläufe",
               "Wie rund die Ecken werden. Bisher 2, mehr als 3 bringt laut alten Messungen nichts. "
               "Passt ein Ring nicht ins Budget, wird er mit weniger Durchläufen geglättet."),
              ("connectivity", "Connectedness",
               "4: Pixel hängen nur über Kanten zusammen (Standard). 8: auch über Ecken, das ergibt "
               "weniger, größere Teile."),
              ("nodata_fallback", "NoData-Ersatz",
               "Gilt nur für Raster ohne eingetragenen NoData-Wert und ohne Alphakanal. "
               "Leer: alle Pixel zählen als gültig."),
              ("crs_fallback", "CRS-Ersatz",
               "Gilt nur für Raster ohne Koordinatensystem, z. B. PNG mit nur einer World-Datei. "
               "Etwa EPSG:25832. Leer: solche Raster brechen beim Downscale mit Hinweis ab."),
              ("ram_limit_gb", "RAM-Grenze in GB",
               "Mehr Arbeitsspeicher bekommt die Rechnung auf dem Server nicht, sie bricht dann mit "
               "Meldung ab statt anderen Prozessen Speicher wegzunehmen. Leer: keine Grenze."),
              ("blacklist", "Ausschließen",
               "Reguläre Ausdrücke auf den Pfad im Datenordner, mit ; getrennt. Passende Dateien "
               "werden nicht gerechnet."),
              ("folder_pattern", "Muster für den Ordnernamen",
               "Regulärer Ausdruck. Jede Gruppe (?P<name>…) wird ein Attribut im GPKG."),
              ("preview_px", "Vorschau in Pixel",
               "Kantenlänge der Vorschaubilder. Größer ist schärfer, aber langsamer über die Leitung.")]

    def __init__(self, cfg, parent):
        super().__init__(parent)
        self.setWindowTitle("Parameter")
        self.cfg = cfg
        form = QFormLayout(self)
        self.edits = {}
        for key, label, hint in self.FIELDS:
            self.edits[key] = add_field(form, label, self.text(cfg[key]), hint, 420)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        reset = buttons.addButton("Standard", QDialogButtonBox.ResetRole)
        reset.clicked.connect(self.reset)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    @staticmethod
    def text(value):
        if isinstance(value, list):
            return "; ".join(value)
        return "" if value is None else str(value)

    def reset(self):
        for key, _, _ in self.FIELDS:
            self.edits[key].setText(self.text(settings.DEFAULTS[key]))

    def values(self):
        out = dict(self.cfg)
        for key, _, _ in self.FIELDS:
            text = self.edits[key].text().strip()
            if key in settings.NUMERIC:
                out[key] = float(text.replace(",", "."))
            elif key in settings.INTEGER:
                out[key] = int(text)
            elif key in ("nodata_fallback", "ram_limit_gb"):
                out[key] = float(text.replace(",", ".")) if text else None
            elif key == "blacklist":
                out[key] = [part.strip() for part in text.split(";") if part.strip()]
            else:
                out[key] = text
        return out


class Window(QWidget):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.steps = []
        self.labels = {}
        self.folders = []
        self.folder_stem = "_ordner"
        self.state = None
        self.folder_index = 0
        self.step_index = 0
        self.raster_index = 0
        self.token = 0
        self.thread = None
        self.error = ""
        self.logged = set()
        self.cache = Path(tempfile.gettempdir()) / "polyqa_cache"
        self.cache.mkdir(exist_ok=True)
        self.pool = QThreadPool.globalInstance()
        self.signals = CallSignals()
        self.signals.done.connect(self.call_done)
        self.setWindowTitle("polyqa")
        self.resize(1360, 880)
        self.build()
        self.connect_server()

    def build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        head = QHBoxLayout()
        head.setContentsMargins(16, 12, 16, 12)
        self.head_label = QLabel()
        self.head_label.setObjectName("muted")
        connection = QPushButton("Verbindung")
        connection.clicked.connect(self.edit_connection)
        params = QPushButton("Parameter")
        params.clicked.connect(self.edit_parameters)
        head.addWidget(self.head_label)
        head.addStretch(1)
        head.addWidget(connection)
        head.addWidget(params)
        outer.addLayout(head)
        outer.addWidget(self.line())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.rail = QWidget()
        self.rail.setObjectName("rail")
        self.rail_layout = QVBoxLayout(self.rail)
        self.rail_layout.setContentsMargins(0, 8, 0, 8)
        self.rail_layout.setSpacing(0)
        self.rail_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(self.rail)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(262)
        scroll.setFrameShape(QFrame.NoFrame)
        body.addWidget(scroll)
        body.addWidget(self.line(vertical=True))

        stage = QVBoxLayout()
        stage.setContentsMargins(0, 0, 0, 0)
        stage.setSpacing(0)
        title_row = QVBoxLayout()
        title_row.setContentsMargins(16, 14, 16, 10)
        title_row.setSpacing(2)
        self.title = QLabel()
        self.title.setObjectName("title")
        self.how = QLabel()
        self.how.setObjectName("how")
        title_row.addWidget(self.title)
        title_row.addWidget(self.how)
        stage.addLayout(title_row)

        pick_row = QHBoxLayout()
        pick_row.setContentsMargins(16, 0, 16, 12)
        self.raster_label = QLabel()
        self.raster_label.setObjectName("muted")
        self.prev_btn = QPushButton("‹")
        self.next_btn = QPushButton("›")
        self.prev_btn.setFixedWidth(34)
        self.next_btn.setFixedWidth(34)
        self.prev_btn.clicked.connect(lambda: self.step_raster(-1))
        self.next_btn.clicked.connect(lambda: self.step_raster(1))
        self.counter = QLabel()
        self.counter.setObjectName("muted")
        self.counter.setAlignment(Qt.AlignCenter)
        self.counter.setFixedWidth(64)
        pick_row.addWidget(self.raster_label)
        pick_row.addStretch(1)
        pick_row.addWidget(self.prev_btn)
        pick_row.addWidget(self.counter)
        pick_row.addWidget(self.next_btn)
        stage.addLayout(pick_row)

        twin = QHBoxLayout()
        twin.setContentsMargins(0, 0, 0, 0)
        twin.setSpacing(1)
        self.panes, self.pane_notes = {}, {}
        for side, caption in (("before", "vorher"), ("after", "nachher")):
            column = QVBoxLayout()
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(0)
            header = QHBoxLayout()
            header.setContentsMargins(14, 8, 14, 8)
            label = QLabel(caption)
            label.setObjectName("paneLabel")
            note = QLabel()
            note.setObjectName("muted")
            header.addWidget(label)
            header.addStretch(1)
            header.addWidget(note)
            pane = Pane()
            pane.zoomed.connect(self.zoom_both)
            column.addLayout(header)
            column.addWidget(pane, 1)
            holder = QWidget()
            holder.setLayout(column)
            twin.addWidget(holder, 1)
            self.panes[side], self.pane_notes[side] = pane, note
        for bar in ("horizontalScrollBar", "verticalScrollBar"):
            getattr(self.panes["before"], bar)().valueChanged.connect(self.sync_scroll)
        stage.addLayout(twin, 1)
        stage.addWidget(self.line())

        self.gauges = QHBoxLayout()
        self.gauges.setContentsMargins(0, 0, 0, 0)
        self.gauges.setSpacing(1)
        holder = QWidget()
        holder.setLayout(self.gauges)
        stage.addWidget(holder)
        stage.addWidget(self.line())

        actions = QHBoxLayout()
        actions.setContentsMargins(16, 12, 16, 14)
        self.go = QPushButton("Ordner rechnen")
        self.go.setObjectName("go")
        self.go.clicked.connect(self.go_clicked)
        self.single = QPushButton("nur diesen Schritt")
        self.single.setObjectName("quiet")
        self.single.clicked.connect(self.run_single)
        self.progress = QLabel()
        self.progress.setObjectName("progress")
        self.progress.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.copy_btn = QPushButton("Fehler kopieren")
        self.copy_btn.setObjectName("quiet")
        self.copy_btn.setToolTip(f"Alle Fehler stehen auch in {settings.log_path()}")
        self.copy_btn.clicked.connect(self.copy_error)
        self.copy_btn.hide()
        actions.addWidget(self.go)
        actions.addWidget(self.single)
        actions.addStretch(1)
        actions.addWidget(self.progress)
        actions.addWidget(self.copy_btn)
        stage.addLayout(actions)

        holder = QWidget()
        holder.setLayout(stage)
        body.addWidget(holder, 1)
        outer.addLayout(body, 1)

    def set_status(self, text, full=""):
        self.progress.setText(text)
        self.error = full
        self.update_copy()

    def show_error(self, full):
        self.set_status(first_line(full), full)

    def error_detail(self):
        if self.error:
            return self.error
        payload = self.payload() if self.folders and self.steps else None
        if payload and payload.get("trace"):
            stem = self.folder_stem if self.step == "merge" else self.stem
            return f"{self.folder['name']} / {stem} / {self.labels[self.step]}\n\n{payload['trace']}"
        return ""

    def update_copy(self):
        self.copy_btn.setVisible(bool(self.error_detail()))

    def copy_error(self):
        QApplication.clipboard().setText(self.error_detail())

    def log_step_errors(self):
        for stem, by_step in self.state["metrics"].items():
            for step, payload in by_step.items():
                key = (self.folder["name"], stem, step, payload.get("trace"))
                if payload.get("trace") and key not in self.logged:
                    self.logged.add(key)
                    settings.log(f"{self.folder['name']} / {stem} / {step}\n{payload['trace']}")

    def line(self, vertical=False):
        frame = QFrame()
        frame.setObjectName("sep")
        frame.setFixedWidth(1) if vertical else frame.setFixedHeight(1)
        return frame

    @property
    def folder(self):
        return self.folders[self.folder_index] if self.folders else None

    @property
    def step(self):
        return self.steps[self.step_index][0] if self.steps else ""

    @property
    def stem(self):
        return self.folder["stems"][self.raster_index]

    def edit_connection(self):
        dialog = Connection(self.cfg, self)
        if dialog.exec() == QDialog.Accepted:
            self.cfg = dialog.values()
            settings.save(self.cfg)
            self.connect_server()

    def edit_parameters(self):
        dialog = Parameters(self.cfg, self)
        if dialog.exec() == QDialog.Accepted:
            self.cfg = dialog.values()
            settings.save(self.cfg)
            self.refresh()

    def ask(self, kind, command, **extra):
        self.token += 1
        self.pool.start(CallJob(self.token, kind, self.cfg, command, self.signals, **extra))
        return self.token

    def connect_server(self):
        out = self.cfg["out_root"] or "polyqa_out im Datenordner"
        host = self.cfg["ssh_host"] or "lokal"
        self.head_label.setText(f"{host}:{self.cfg['data_root']}   →   {out}"
                                if settings.configured(self.cfg) else "keine Verbindung eingerichtet")
        self.folders, self.state = [], None
        self.refresh()
        if settings.configured(self.cfg):
            self.set_status("verbinde …")
            self.ask("discover", "discover")

    def request_state(self):
        self.ask("state", "state", folder=self.folder["name"])

    def ask_state(self):
        self.state = None
        self.refresh()
        self.request_state()

    @Slot(int, str, object, str)
    def call_done(self, token, kind, payload, error):
        if token != self.token:
            return
        if error:
            self.show_error(error)
            return
        if kind == "discover":
            self.set_status("")
            self.steps = payload["steps"]
            self.labels = {k: label for k, label, _ in payload["steps"]}
            self.folders = payload["folders"]
            self.folder_stem = payload["folder_stem"]
            self.folder_index = self.raster_index = 0
            if self.folders:
                self.ask_state()
            else:
                self.refresh()
        elif kind == "state":
            self.state = payload
            self.log_step_errors()
            job = payload.get("job")
            if job and job["running"] and self.thread is None:
                self.start(job["steps"], fresh=False, attach=True)
                return
            if self.thread is None:
                self.step_index = self.first_open_step()
            self.refresh()
            self.load_previews()
        elif kind == "stop":
            self.set_status("wird angehalten …")
        elif kind.startswith("preview:"):
            side = kind.split(":", 1)[1]
            png = payload.get("png")
            if not png:
                self.panes[side].show_image(None)
                return
            target = self.cache / f"{self.folder['name']}_{self.stem}_{self.step}_{side}.png"
            target.write_bytes(base64.b64decode(png))
            self.panes[side].show_image(target)

    def step_states(self):
        return self.state["steps"] if self.state else {}

    def open_steps(self):
        return [k for k, _, _ in self.steps if self.step_states().get(k) == "offen"]

    def first_open_step(self):
        for i, (key, _, _) in enumerate(self.steps):
            if self.step_states().get(key) != "fertig":
                return i
        return len(self.steps) - 1

    def payload(self):
        if not self.state:
            return None
        stem = self.folder_stem if self.step == "merge" else self.stem
        return self.state["metrics"].get(stem, {}).get(self.step)

    def pick_folder(self, index):
        self.folder_index = index
        self.raster_index = 0
        self.set_status("")
        self.ask_state()

    def pick_step(self, index):
        self.step_index = index
        self.refresh()
        self.load_previews()

    def step_raster(self, delta):
        self.raster_index = (self.raster_index + delta) % len(self.folder["stems"])
        self.refresh()
        self.load_previews()

    def zoom_both(self, factor):
        for pane in self.panes.values():
            pane.scale(factor, factor)

    def sync_scroll(self):
        a, b = self.panes["before"], self.panes["after"]
        b.horizontalScrollBar().setValue(a.horizontalScrollBar().value())
        b.verticalScrollBar().setValue(a.verticalScrollBar().value())

    def build_rail(self):
        while self.rail_layout.count() > 1:
            item = self.rail_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for index, folder in enumerate(self.folders):
            active = index == self.folder_index
            right = ""
            if active and self.state:
                values = list(self.step_states().values())
                done = values.count("fertig")
                right = ("Prüfung" if "flag" in values else
                         "fertig" if done == len(values) else
                         "offen" if done == 0 else f"{done} von {len(values)}")
            row = Row(folder["name"], right, "folderName",
                      "flag" if right == "Prüfung" else "folderDim", 0)
            if active:
                row.setObjectName("rowActive")
            row.clicked.connect(lambda i=index: self.pick_folder(i))
            self.rail_layout.insertWidget(self.rail_layout.count() - 1, row)
            if not active:
                continue
            for s, (key, label, _) in enumerate(self.steps):
                state = self.step_states().get(key, "")
                style = "stepActive" if s == self.step_index else (
                    "stepDone" if state == "fertig" else "stepName")
                row = Row(label, "Prüfung" if state == "flag" else "", style, "flag", 14)
                if s == self.step_index:
                    row.setObjectName("rowActive")
                row.clicked.connect(lambda i=s: self.pick_step(i))
                self.rail_layout.insertWidget(self.rail_layout.count() - 1, row)

    def build_gauges(self, payload):
        while self.gauges.count():
            item = self.gauges.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for name, value, verdict in (payload["gauges"] if payload else [["", "", ""]] * 3):
            box = QVBoxLayout()
            box.setContentsMargins(16, 10, 16, 12)
            box.setSpacing(1)
            title = QLabel(name)
            title.setObjectName("gaugeName")
            number = QLabel(value)
            number.setObjectName("gaugeValue")
            number.setTextInteractionFlags(Qt.TextSelectableByMouse)
            number.setStyleSheet("color: #ab1f68;" if verdict == "flag" else "color: #10242e;")
            box.addWidget(title)
            box.addWidget(number)
            holder = QWidget()
            holder.setLayout(box)
            self.gauges.addWidget(holder, 1)

    def refresh(self):
        self.build_rail()
        running = self.thread is not None
        if not self.folders or not self.steps:
            self.title.setText("Keine Verbindung" if not settings.configured(self.cfg) else "Keine Ordner")
            self.how.setText("Über „Verbindung“ Serverskript und Datenordner eintragen.")
            self.build_gauges(None)
            self.go.setEnabled(False)
            self.single.setEnabled(False)
            self.update_copy()
            return
        key, label, how = self.steps[self.step_index]
        self.title.setText(label)
        self.how.setText(how)
        depth = self.stem.count("__") + 1
        self.raster_label.setText("/".join(Path(self.folder["rasters"][self.raster_index]).parts[-depth:]))
        self.counter.setText(f"{self.raster_index + 1} / {len(self.folder['rasters'])}")
        payload = self.payload()
        self.build_gauges(payload)
        for side in ("before", "after"):
            self.pane_notes[side].setText(payload[side] if payload else "")
        state = self.step_states().get(key)
        if running:
            self.go.setText("anhalten")
        elif state == "flag" and self.open_steps():
            self.go.setText("trotzdem weiterrechnen")
        elif self.open_steps():
            self.go.setText("Ordner rechnen")
        else:
            self.go.setText("Ordner neu rechnen")
        self.go.setEnabled(running or self.state is not None)
        self.single.setEnabled(not running and self.state is not None)
        self.update_copy()

    def load_previews(self):
        self.token += 1
        token = self.token
        for side in ("before", "after"):
            cached = self.cache / f"{self.folder['name']}_{self.stem}_{self.step}_{side}.png"
            if cached.exists():
                self.panes[side].show_image(cached)
                continue
            self.panes[side].show_image(None)
            self.pool.start(CallJob(token, f"preview:{side}", self.cfg, "preview",
                                    self.signals, folder=self.folder["name"], stem=self.stem,
                                    step=self.step, side=side))

    def drop_cache(self, from_index):
        keys = [k for k, _, _ in self.steps[from_index:]]
        for path in self.cache.glob(f"{self.folder['name']}_*"):
            if any(f"_{key}_" in path.name for key in keys):
                path.unlink()

    def go_clicked(self):
        if self.thread:
            self.ask("stop", "stop", folder=self.worker.folder)
        else:
            self.run_folder()

    def run_folder(self):
        if self.open_steps():
            self.start(self.open_steps(), fresh=False)
        else:
            self.start([k for k, _, _ in self.steps], fresh=True)

    def run_single(self):
        self.start([self.step], fresh=True)

    def start(self, steps, fresh, attach=False):
        order = [k for k, _, _ in self.steps]
        if fresh:
            self.drop_cache(min(order.index(s) for s in steps))
        self.thread = QThread()
        self.worker = Worker(self.cfg, self.folder["name"], steps, self.labels, fresh, attach)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.set_status)
        self.worker.step_done.connect(self.on_step_done)
        self.worker.failed.connect(self.on_failed)
        self.worker.finished.connect(self.on_finished)
        self.thread.start()
        self.refresh()

    @Slot(str, bool)
    def on_step_done(self, step, flagged):
        self.step_index = [k for k, _, _ in self.steps].index(step)
        if flagged:
            self.set_status("Prüfung schlägt an – weiter erst nach Klärung")
        self.request_state()

    @Slot(str, str)
    def on_failed(self, step, message):
        order = [k for k, _, _ in self.steps]
        if step in order:
            self.step_index = order.index(step)
            message = f"{self.labels[step]}: {message}"
        self.show_error(message)
        self.stop(keep_message=True)

    @Slot()
    def on_finished(self):
        self.stop(keep_message=self.progress.text().startswith("Prüfung"))

    def stop(self, keep_message=False):
        if self.thread:
            self.thread.quit()
            self.thread.wait()
            self.thread = None
        if not keep_message:
            self.set_status("")
        self.refresh()
        if self.folders:
            self.request_state()


def first_line(text):
    return next((line for line in text.splitlines() if line.strip()), text)


def launch():
    cfg = settings.load()
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(STYLE)
    window = Window(cfg)
    window.show()
    return app, window
