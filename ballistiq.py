#!/usr/bin/env python3
"""Ballistiq: marca a mano la posición de un objeto en cada frame de un vídeo
y exporta la tabla frame -> píxel a un Excel que queda junto al vídeo.

Los frames se numeran desde 0. Las coordenadas son píxeles del vídeo tal como se
ve (con la rotación y el ancho/alto indicados), con origen arriba a la izquierda.
"""

import argparse
import bisect
import math
import os
import signal
import sys
from collections import OrderedDict
from pathlib import Path

import av
import numpy as np
from openpyxl import Workbook
try:
    from PyQt6.QtCore import QT_VERSION, QPointF, QRectF, Qt, QTimer, pyqtSignal
    from PyQt6.QtGui import (QColor, QImage, QKeySequence, QLinearGradient, QPainter, QPalette,
                             QPen, QPixmap, QPolygonF, QShortcut, QTransform)
    from PyQt6.QtWidgets import (QAbstractSpinBox, QApplication, QCheckBox, QDoubleSpinBox,
                                 QFileDialog, QFormLayout, QFrame, QGraphicsScene, QGraphicsView,
                                 QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                                 QMessageBox, QProgressDialog, QPushButton, QSizePolicy, QSlider,
                                 QSpinBox, QStyle, QStyleOptionSlider, QVBoxLayout, QWidget)
except ImportError as qt6_error:
    # Reserva para sistemas sin Qt6 que traen PyQt5 en sus propios paquetes.
    try:
        from PyQt5.QtCore import QT_VERSION, QPointF, QRectF, Qt, QTimer, pyqtSignal
        from PyQt5.QtGui import (QColor, QImage, QKeySequence, QLinearGradient, QPainter, QPalette,
                                 QPen, QPixmap, QPolygonF, QTransform)
        from PyQt5.QtWidgets import (QAbstractSpinBox, QApplication, QCheckBox, QDoubleSpinBox,
                                     QFileDialog, QFormLayout, QFrame, QGraphicsScene, QGraphicsView,
                                     QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                                     QMessageBox, QProgressDialog, QPushButton, QShortcut,
                                     QSizePolicy, QSlider, QSpinBox, QStyle, QStyleOptionSlider,
                                     QVBoxLayout, QWidget)
    except ImportError:
        raise qt6_error

__version__ = "1.1.0"

PREDICTED_FRAMES = 5   # cuántos frames futuros se predicen
FIT_POINTS = 5         # con cuántos puntos recientes se calcula la predicción
CACHE_BYTES = 512 * 1024 * 1024
ZOOM_STEP = 1.25
ZOOM_MIN, ZOOM_MAX = 0.02, 64.0
MIN_EXPORT_POINTS = 2
PANEL_WIDTH, PANEL_MARGIN = 270, 14
FIRST, LAST = 0, 1     # posición de cada marca en MainWindow.ticks

POINT_COLOR = QColor("#ff3b30")
PREDICTED_COLOR = QColor("#00d1ff")
HALO_COLOR = QColor(0, 0, 0, 170)
ACTIVE_RADIUS, PAST_RADIUS = 14, 5
PREDICTED_MIN_GAP = 12  # separación mínima en pantalla entre círculos de predicción

VIDEO_FILTER = ("Vídeos (*.mp4 *.mov *.avi *.mkv *.m4v *.wmv *.mpg *.mpeg *.webm "
                "*.MP4 *.MOV *.AVI *.MKV *.M4V *.WMV *.MPG *.MPEG *.WEBM);;"
                "Todos los archivos (*)")
INVALID_NAME_CHARS = '<>:"/\\|?*'
FFMPEG_ERROR = getattr(av, "FFmpegError", av.error.FFmpegError)  # el nombre cambia según la versión

STYLE = """
#panel { background: #f5f5f7; border-left: 1px solid #d9d9de; }
#navbar { background: #f5f5f7; border-top: 1px solid #d9d9de; }
QLabel[role="title"] { color: #6e6e73; font-size: 11px; font-weight: 600; margin-top: 12px; }
QLabel[role="hint"] { color: #6e6e73; font-size: 11px; }
QLabel[role="warning"] { color: #b25000; font-size: 11px; }
QPushButton#export { background: #0a84ff; color: white; font-weight: 600;
                     border: none; border-radius: 5px; padding: 8px; }
QPushButton#export:hover { background: #0077ed; }
QPushButton#export:pressed { background: #0062c4; }
QPushButton[role="link"] { color: #0a84ff; border: none; background: transparent; }
"""


class VideoReader:
    """Lee cualquier frame por su número con exactitud.

    Al abrir recorre los paquetes (sin decodificar) para saber el timestamp real
    de cada frame, y luego salta por timestamp. Saltar por número de frame, como
    hace OpenCV, devuelve frames equivocados si los FPS varían mucho (cámara lenta).
    Si el archivo no trae timestamps, se decodifica en orden desde el principio:
    es más lento, pero igual de exacto.
    """

    def __init__(self, path, progress=None):
        self.path = str(path)
        self._cache = OrderedDict()
        self._cache_size = 8
        self._scan(progress)
        self._open_decoder()
        first = self.frame(0)
        if first is None:
            raise ValueError("no se pudo decodificar ningún frame")
        self.height, self.width = first.shape[:2]
        self._cache_size = max(8, min(512, CACHE_BYTES // first.nbytes))

    def _scan(self, progress):
        size = max(os.path.getsize(self.path), 1)
        pts, keys, complete = [], [], True
        with av.open(self.path) as container:
            stream = container.streams.video[0]
            self.fps = float(stream.average_rate or stream.guessed_rate or 0)
            for n, packet in enumerate(container.demux(stream)):
                if packet.size == 0:
                    continue
                if packet.pts is None:
                    complete = False
                    break
                if packet.is_keyframe:   # también si se descarta: hace falta para decodificar
                    keys.append(packet.pts)
                if not getattr(packet, "is_discard", False):
                    pts.append(packet.pts)
                if progress and n % 256 == 0 and packet.pos:
                    progress(min(packet.pos / size, 1.0))

        self.variable_fps = False
        if complete and pts and keys and len(set(pts)) == len(pts):
            self.pts = sorted(pts)
            self._keys = sorted(keys)
            self._index = {t: i for i, t in enumerate(self.pts)}
            self.count = len(self.pts)
            if self.count > 1:
                gaps = np.diff(self.pts)
                typical = np.median(gaps)
                self.variable_fps = bool(np.any(np.abs(gaps - typical) > max(2, 0.2 * typical)))
        else:
            self.pts = self._keys = self._index = None
            with av.open(self.path) as container:
                self.count = sum(1 for _ in container.decode(video=0))
        if not self.fps:
            self.fps = 30.0

    def _open_decoder(self):
        self._container = av.open(self.path)
        self._stream = self._container.streams.video[0]
        self._stream.thread_type = "AUTO"
        self._frames = self._container.decode(self._stream)
        self._next = 0

    def _position_for(self, k):
        """Deja el decodificador en un punto desde el que se pueda llegar al frame k."""
        key = None
        if self._index is not None:
            i = bisect.bisect_right(self._keys, self.pts[k]) - 1
            key = self._keys[i] if i >= 0 else None
        behind = self._next is None or k < self._next
        if key is None:     # sin timestamps o sin keyframe previo: desde el principio
            if behind:
                self._open_decoder()
        elif behind or key > self.pts[self._next]:
            self._container.seek(key, stream=self._stream)
            self._frames = self._container.decode(self._stream)
            self._next = bisect.bisect_left(self.pts, key)

    def _store(self, i, decoded):
        image = decoded.to_ndarray(format="rgb24")
        # Móviles: giro indicado en los metadatos (av anterior a 14 solo lo da en el stream)
        rotation = getattr(decoded, "rotation", None)
        if rotation is None:
            rotation = self._stream.side_data.get("DISPLAYMATRIX", 0)
        if rotation:
            image = np.rot90(image, round(rotation / 90)).copy()
        self._cache[i] = image
        self._cache.move_to_end(i)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def frame(self, k):
        """Devuelve el frame k como array RGB, o None si no se puede leer."""
        if not 0 <= k < self.count:
            return None
        if k in self._cache:
            self._cache.move_to_end(k)
            return self._cache[k]
        try:
            self._position_for(k)
            for decoded in self._frames:
                i = self._next if self._index is None else self._index.get(decoded.pts)
                if i is None:
                    continue
                self._next = i + 1
                if k - i < self._cache_size:
                    self._store(i, decoded)
                if i >= k:
                    break
            else:
                self._next = None  # fin del vídeo: habrá que volver a situarse
        except FFMPEG_ERROR:
            self._next = None
            return None
        return self._cache.get(k)


def predict(points, frame, last_frame):
    """Extrapola en línea recta los siguientes frames sin marcar.

    Ajusta x(frame) e y(frame) con los últimos puntos marcados hasta `frame`, así
    que los frames saltados no falsean la velocidad. Devuelve [(frame, x, y), ...].
    """
    past = sorted(f for f in points if f <= frame)[-FIT_POINTS:]
    if len(past) < 2:
        return []
    t = np.array(past) - past[-1]
    fit_x = np.polyfit(t, [points[f][0] for f in past], 1)
    fit_y = np.polyfit(t, [points[f][1] for f in past], 1)
    start = max(past[-1] + 1, frame)
    result = []
    for f in range(start, min(start + PREDICTED_FRAMES, last_frame + 1)):
        if f in points:
            break
        dt = f - past[-1]
        result.append((f, float(np.polyval(fit_x, dt)), float(np.polyval(fit_y, dt))))
    return result


def write_excel(path, rows, info):
    """Hoja "Puntos": frame, x, y, marca. Hoja "Info": datos con los que se midió."""
    book = Workbook()
    sheet = book.active
    sheet.title = "Puntos"
    sheet.append(["frame", "x", "y", "marca"])
    for row in rows:
        sheet.append(list(row))
    sheet.freeze_panes = "A2"
    details = book.create_sheet("Info")
    for row in info:
        details.append(list(row))
    details.column_dimensions["A"].width = 18
    details.column_dimensions["B"].width = 40
    book.save(path)


def _event_pos(event):
    """Posición del ratón: Qt6 usa position() y Qt5 pos()."""
    return event.position().toPoint() if hasattr(event, "position") else event.pos()


def _light_palette():
    """Mismo aspecto claro en todos los sistemas, también con el modo oscuro activado."""
    palette = QPalette(QColor("#fbfbfd"), QColor("#f5f5f7"))
    role = QPalette.ColorRole
    for name, color in ((role.WindowText, "#1d1d1f"), (role.Text, "#1d1d1f"), (role.ButtonText, "#1d1d1f"),
                        (role.Base, "#ffffff"), (role.AlternateBase, "#f0f0f2"), (role.ToolTipBase, "#ffffff"),
                        (role.ToolTipText, "#1d1d1f"), (role.Highlight, "#0a84ff"),
                        (role.HighlightedText, "#ffffff"), (role.PlaceholderText, "#8e8e93")):
        palette.setColor(name, QColor(color))
    for name in (role.WindowText, role.Text, role.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, name, QColor("#a1a1a6"))
    return palette


def _stroked(painter, color, width, draw, style=Qt.PenStyle.SolidLine):
    """Dibuja con un halo oscuro para que se vea sobre cualquier fondo."""
    for pen_color, pen_width in ((HALO_COLOR, width + 2), (color, width)):
        painter.setPen(QPen(pen_color, pen_width, style, Qt.PenCapStyle.RoundCap))
        draw()


def _draw_marker(painter, center, radius, width):
    def draw():
        painter.drawEllipse(center, radius, radius)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            direction = QPointF(dx, dy)
            painter.drawLine(center + direction * (radius * 0.35),
                             center + direction * (radius * 1.4))
    _stroked(painter, POINT_COLOR, width, draw)


class VideoView(QGraphicsView):
    """Muestra el frame, gestiona zoom/rotación/desplazamiento y dibuja las marcas.

    La escena está en píxeles del vídeo original, así que los puntos no cambian
    al hacer zoom o rotar: solo cambia la transformación de la vista.
    """

    leftClicked = pyqtSignal(int, int)
    rightClicked = pyqtSignal()
    zoomChanged = pyqtSignal(float)

    def __init__(self, width, height):
        super().__init__()
        self._image_size = (width, height)
        self._rotation = 0
        self._stretch = (1.0, 1.0)
        self._zoom = 1.0
        self._fit_mode = True
        self._pan_from = None
        self._space_held = False
        self._points, self._frame, self._predicted = {}, 0, []

        self.setScene(QGraphicsScene(self))
        self._item = self.scene().addPixmap(QPixmap())
        self.setSceneRect(QRectF(0, 0, width, height))
        self.setBackgroundBrush(QColor("#1c1c1e"))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.viewport().setCursor(Qt.CursorShape.CrossCursor)

    def set_image(self, image):
        h, w = image.shape[:2]
        qimage = QImage(image.data, w, h, image.strides[0], QImage.Format.Format_RGB888)
        self._item.setPixmap(QPixmap.fromImage(qimage))

    def set_overlay(self, points, frame, predicted):
        self._points, self._frame, self._predicted = points, frame, predicted
        self.viewport().update()

    def set_geometry(self, rotation, stretch):
        self._rotation, self._stretch = rotation, stretch
        if self._fit_mode:
            self.fit()
        else:
            self._apply_transform()

    def _base_transform(self):
        transform = QTransform()
        transform.rotate(self._rotation)
        transform.scale(*self._stretch)
        return transform

    def _apply_transform(self):
        self.setTransform(self._base_transform() * QTransform.fromScale(self._zoom, self._zoom))
        self._item.setTransformationMode(
            Qt.TransformationMode.SmoothTransformation if self._zoom < 1.5 else Qt.TransformationMode.FastTransformation)
        self.zoomChanged.emit(self._zoom)

    def fit(self):
        self._fit_mode = True
        shown = self._base_transform().mapRect(self.sceneRect())
        self._zoom = 0.99 * min(self.width() / shown.width(), self.height() / shown.height())
        self._apply_transform()

    def zoom_by(self, factor):
        self._fit_mode = False
        self._zoom = min(max(self._zoom * factor, ZOOM_MIN), ZOOM_MAX)
        self._apply_transform()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fit_mode:
            self.fit()

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta:
            self.zoom_by(1.0015 ** delta)

    def mousePressEvent(self, event):
        button = event.button()
        if button == Qt.MouseButton.MiddleButton or (button == Qt.MouseButton.LeftButton and self._space_held):
            self._pan_from = _event_pos(event)
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        elif button == Qt.MouseButton.LeftButton:
            p = self.mapToScene(_event_pos(event))
            x, y = math.floor(p.x()), math.floor(p.y())
            if 0 <= x < self._image_size[0] and 0 <= y < self._image_size[1]:
                self.leftClicked.emit(x, y)
        elif button == Qt.MouseButton.RightButton:
            self.rightClicked.emit()

    def mouseDoubleClickEvent(self, event):
        self.mousePressEvent(event)   # dos clics rápidos son dos clics, no un doble clic

    def mouseMoveEvent(self, event):
        if self._pan_from is not None:
            delta = _event_pos(event) - self._pan_from
            self._pan_from = _event_pos(event)
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())

    def mouseReleaseEvent(self, event):
        if self._pan_from is not None:
            self._pan_from = None
            self._update_cursor()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space:
            if not event.isAutoRepeat():
                self._space_held = True
                self._update_cursor()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_held = False
            self._update_cursor()

    def focusOutEvent(self, event):
        self._space_held = False
        self._update_cursor()
        super().focusOutEvent(event)

    def _update_cursor(self):
        self.viewport().setCursor(Qt.CursorShape.OpenHandCursor if self._space_held else Qt.CursorShape.CrossCursor)

    def drawForeground(self, painter, rect):
        to_view = self.viewportTransform().map

        def center(x, y):  # centro del píxel, en coordenadas de pantalla
            return to_view(QPointF(x + 0.5, y + 0.5))

        painter.save()
        painter.resetTransform()   # tamaño de las marcas fijo, sea cual sea el zoom
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        marked = sorted(self._points)
        line = QPolygonF([center(*self._points[f]) for f in marked])
        _stroked(painter, POINT_COLOR, 1.5, lambda: painter.drawPolyline(line))

        self._draw_prediction(painter, center, marked)

        for f in marked:
            if f != self._frame:
                _draw_marker(painter, center(*self._points[f]), PAST_RADIUS, 1.5)
        if self._frame in self._points:
            _draw_marker(painter, center(*self._points[self._frame]), ACTIVE_RADIUS, 2)
        painter.restore()

    def _draw_prediction(self, painter, center, marked):
        width, height = self._image_size
        inside = [(f, x, y) for f, x, y in self._predicted if 0 <= x < width and 0 <= y < height]
        if not inside:
            return
        origin = [f for f in marked if f <= self._frame][-1]
        spots = [(f, center(x, y)) for f, x, y in inside]
        previous = center(*self._points[origin])
        line = QPolygonF([previous] + [p for _, p in spots])
        _stroked(painter, PREDICTED_COLOR, 1.5, lambda: painter.drawPolyline(line), Qt.PenStyle.DashLine)
        for f, spot in spots:
            is_current = f == self._frame
            # Si el objeto va despacio, no amontonar círculos encima de él.
            if not is_current and (spot - previous).manhattanLength() < PREDICTED_MIN_GAP:
                continue
            previous = spot
            radius = ACTIVE_RADIUS - 3 if is_current else 3

            def draw(spot=spot, radius=radius):
                painter.drawEllipse(spot, radius, radius)
            _stroked(painter, PREDICTED_COLOR, 1.5, draw)


class LevelsBar(QWidget):
    """Barra de niveles con tres tiradores: negro, medios y blanco.

    Solo cambia cómo se ve la imagen: ni el vídeo ni las coordenadas se tocan.
    """

    changed = pyqtSignal()
    MARGIN = 8
    MIN_RANGE = 4

    def __init__(self):
        super().__init__()
        self.setFixedHeight(28)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.setToolTip("Arrastra negro, medios o blanco. Doble clic: restablecer.")
        self._grabbed = None
        self.black, self.white, self.mid = 0, 255, 0.5

    def reset(self):
        self.black, self.white, self.mid = 0, 255, 0.5
        self.update()
        self.changed.emit()

    @property
    def gamma(self):
        return math.log(self.mid) / math.log(0.5)

    def lut(self):
        """Tabla de 256 valores para aplicar a la imagen, o None si no hay ajuste."""
        if (self.black, self.white, self.mid) == (0, 255, 0.5):
            return None
        t = np.clip((np.arange(256) - self.black) / (self.white - self.black), 0, 1)
        return (255 * t ** (1 / self.gamma) + 0.5).astype(np.uint8)

    def _x_of(self, value):
        return self.MARGIN + value / 255 * (self.width() - 2 * self.MARGIN)

    def _value_at(self, x):
        return (x - self.MARGIN) / (self.width() - 2 * self.MARGIN) * 255

    def _handles(self):
        mid_value = self.black + self.mid * (self.white - self.black)
        return (("black", self.black, QColor("#000000")),
                ("mid", mid_value, QColor("#8e8e93")),
                ("white", self.white, QColor("#ffffff")))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._grabbed = min(self._handles(),
                                key=lambda h: abs(self._x_of(h[1]) - _event_pos(event).x()))[0]
            self.mouseMoveEvent(event)

    def mouseMoveEvent(self, event):
        value = self._value_at(_event_pos(event).x())
        if self._grabbed == "black":
            self.black = min(max(round(value), 0), self.white - self.MIN_RANGE)
        elif self._grabbed == "white":
            self.white = max(min(round(value), 255), self.black + self.MIN_RANGE)
        elif self._grabbed == "mid":
            self.mid = min(max((value - self.black) / (self.white - self.black), 0.05), 0.95)
        else:
            return
        self.update()
        self.changed.emit()

    def mouseReleaseEvent(self, event):
        self._grabbed = None

    def mouseDoubleClickEvent(self, event):
        self._grabbed = None
        self.reset()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        groove = QRectF(self.MARGIN, 3, self.width() - 2 * self.MARGIN, 10)
        gradient = QLinearGradient(self._x_of(self.black), 0, self._x_of(self.white), 0)
        gradient.setColorAt(0, QColor("#000000"))
        gradient.setColorAt(1, QColor("#ffffff"))
        painter.setPen(QPen(QColor("#b0b0b5"), 1))
        painter.setBrush(gradient)
        painter.drawRoundedRect(groove, 2, 2)
        painter.setPen(QPen(QColor("#48484a"), 1))
        for _, value, color in self._handles():
            x = self._x_of(value)
            painter.setBrush(color)
            painter.drawPolygon(QPolygonF([QPointF(x, 12), QPointF(x - 6, 24), QPointF(x + 6, 24)]))


class FrameSlider(QSlider):
    """Barra de frames: un clic en cualquier punto salta directamente ahí."""

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            option = QStyleOptionSlider()
            self.initStyleOption(option)
            handle = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, option,
                                                 QStyle.SubControl.SC_SliderHandle, self)
            if not handle.contains(_event_pos(event)):
                self.setValue(QStyle.sliderValueFromPosition(
                    self.minimum(), self.maximum(), _event_pos(event).x(), self.width()))
        super().mousePressEvent(event)


class MainWindow(QMainWindow):
    def __init__(self, reader, path):
        super().__init__()
        self.reader = reader
        self.path = Path(path)
        self.frame = 0
        self._target = 0
        self.points = {}               # frame -> (x, y) en píxeles del vídeo original
        self.ticks = [None, None]      # frame marcado como primero y como último
        self.rotation = 0
        self.size_unrotated = (reader.width, reader.height)
        self.fps = reader.fps
        self.dirty = False

        self._nav_timer = QTimer(self, singleShot=True, interval=0)
        self._nav_timer.timeout.connect(self._show_target)

        self.setWindowTitle(f"Ballistiq · {self.path.name}")
        self.view = VideoView(reader.width, reader.height)
        self.view.leftClicked.connect(self._on_left_click)
        self.view.rightClicked.connect(self._on_right_click)
        self.view.zoomChanged.connect(lambda z: self.zoom_label.setText(f"{z * 100:.0f} %"))

        root = QWidget()
        layout = QGridLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.view, 0, 0)
        layout.addWidget(self._build_navbar(), 1, 0)
        layout.addWidget(self._build_panel(), 0, 1, 2, 1)
        layout.setColumnStretch(0, 1)
        layout.setRowStretch(0, 1)
        self.setCentralWidget(root)
        self.resize(1200, 760)

        for key, step in ((Qt.Key.Key_Left, -1), (Qt.Key.Key_Right, 1)):
            QShortcut(QKeySequence(key), self, lambda step=step: self.goto(self._target + step))

        self.view.setFocus()
        self._show_target()

    # ---------------------------------------------------------------- interfaz

    def _button(self, text, slot):
        button = QPushButton(text)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(slot)
        return button

    def _number_field(self, field, maximum, value, on_change):
        """Campo numérico: aplica al confirmar y Enter devuelve el foco al vídeo."""
        field.setRange(field.minimum(), maximum)
        field.setValue(value)
        field.setKeyboardTracking(False)
        field.valueChanged.connect(on_change)   # después de setValue: no es una edición
        field.lineEdit().returnPressed.connect(self.view.setFocus)
        return field

    def _build_navbar(self):
        bar = QWidget(objectName="navbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 8, 10, 8)
        last = self.reader.count - 1

        self.slider = FrameSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, last)
        self.slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.slider.valueChanged.connect(self.goto)

        self.frame_field = self._number_field(QSpinBox(), last, 0, self.goto)
        self.frame_field.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.frame_field.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.frame_field.setFixedWidth(70)

        self.time_label = QLabel()
        self.time_label.setMinimumWidth(80)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        previous_button = self._button("◀", lambda: self.goto(self._target - 1))
        next_button = self._button("▶", lambda: self.goto(self._target + 1))
        previous_button.setFixedWidth(40)
        next_button.setFixedWidth(40)
        row.addWidget(previous_button)
        row.addWidget(self.slider, 1)
        row.addWidget(next_button)
        row.addSpacing(10)
        row.addWidget(QLabel("Frame"))
        row.addWidget(self.frame_field)
        row.addWidget(QLabel(f"de 0 a {last}"))
        row.addWidget(self.time_label)
        return bar

    def _build_panel(self):
        panel = QWidget(objectName="panel")
        panel.setFixedWidth(PANEL_WIDTH)
        column = QVBoxLayout(panel)
        column.setContentsMargins(PANEL_MARGIN, 6, PANEL_MARGIN, PANEL_MARGIN)
        column.setSpacing(6)

        def title(text):
            label = QLabel(text.upper())
            label.setProperty("role", "title")
            column.addWidget(label)

        title("Vídeo")
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        fps_spin = QDoubleSpinBox(decimals=3, minimum=0.001)
        self.fps_field = self._number_field(fps_spin, 1000000, self.fps, self._on_fps_edited)
        self.width_field = self._number_field(QSpinBox(minimum=1), 100000, self.reader.width,
                                              self._on_size_edited)
        self.height_field = self._number_field(QSpinBox(minimum=1), 100000, self.reader.height,
                                               self._on_size_edited)
        form.addRow("FPS", self.fps_field)
        form.addRow("Ancho", self.width_field)
        form.addRow("Alto", self.height_field)
        column.addLayout(form)
        if self.reader.variable_fps:
            warning = QLabel("FPS variable: el valor es la media, así que el tiempo "
                             "de cada frame es aproximado.")
            warning.setProperty("role", "warning")
            warning.setWordWrap(True)
            column.addWidget(warning)

        title("Vista")
        self.rotate_button = self._button("Rotar 90°", self._rotate)
        column.addWidget(self.rotate_button)
        zoom_row = QHBoxLayout()
        zoom_row.addWidget(self._button("−", lambda: self.view.zoom_by(1 / ZOOM_STEP)))
        zoom_row.addWidget(self._button("+", lambda: self.view.zoom_by(ZOOM_STEP)))
        zoom_row.addWidget(self._button("Ajustar", self.view.fit))
        self.zoom_label = QLabel("100 %")
        self.zoom_label.setMinimumWidth(46)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        zoom_row.addWidget(self.zoom_label)
        column.addLayout(zoom_row)

        title("Niveles")
        self.levels = LevelsBar()
        self.levels.changed.connect(lambda: self.goto(self._target))
        column.addWidget(self.levels)
        levels_row = QHBoxLayout()
        self.levels_label = QLabel()
        self.levels_label.setProperty("role", "hint")
        levels_row.addWidget(self.levels_label)
        levels_row.addStretch(1)
        levels_reset = self._button("Restablecer", self.levels.reset)
        levels_reset.setProperty("role", "link")
        levels_row.addWidget(levels_reset)
        column.addLayout(levels_row)

        title("Marcas de frame")
        self.tick_boxes, self.tick_jumps = [], []
        for which, text in enumerate(("Primer frame", "Último frame")):
            box = QCheckBox(text)
            box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            box.setToolTip(f"Marca el punto de este frame como el {text.split()[0].lower()} de la "
                           "trayectoria. Déjalo sin marcar si esta cámara no lo ve.")
            box.toggled.connect(lambda checked, which=which: self._on_tick(which, checked))
            jump = self._button("", lambda _=False, which=which: self.goto(self.ticks[which]))
            jump.setProperty("role", "link")
            jump.setToolTip("Ir a este frame (ahí se puede desmarcar)")
            row = QHBoxLayout()
            row.addWidget(box)
            row.addStretch(1)
            row.addWidget(jump)
            column.addLayout(row)
            self.tick_boxes.append(box)
            self.tick_jumps.append(jump)

        title("Puntos")
        self.point_label = QLabel()
        self.count_label = QLabel()
        column.addWidget(self.point_label)
        column.addWidget(self.count_label)

        title("Exportar")
        name_row = QHBoxLayout()
        self.name_field = QLineEdit(self.path.stem)
        self.name_field.setToolTip("Nombre del Excel. Por defecto, el del vídeo.")
        self.name_field.returnPressed.connect(self.view.setFocus)
        name_row.addWidget(self.name_field, 1)
        name_row.addWidget(QLabel(".xlsx"))
        column.addLayout(name_row)
        self.export_note = QLabel("Se guarda en la misma carpeta que el vídeo:")
        self.export_note.setProperty("role", "hint")
        self.export_note.setWordWrap(True)
        column.addWidget(self.export_note)
        folder = str(self.path.resolve().parent)
        self.folder_label = QLabel()
        self.folder_label.setProperty("role", "hint")
        self.folder_label.setToolTip(folder)
        self.folder_label.ensurePolished()   # para medir el texto con su tamaño real
        self.folder_label.setText(self.folder_label.fontMetrics().elidedText(
            folder, Qt.TextElideMode.ElideMiddle, PANEL_WIDTH - 2 * PANEL_MARGIN))
        column.addWidget(self.folder_label)
        export_button = self._button("Exportar a Excel", self._export)
        export_button.setObjectName("export")
        column.addWidget(export_button)

        column.addStretch(1)
        hint = QLabel("Clic izquierdo: poner punto\n"
                      "Clic derecho: quitar el punto de este frame\n"
                      "← →: frame anterior / siguiente\n"
                      "Rueda: zoom\n"
                      "Espacio + arrastrar: mover la imagen")
        hint.setProperty("role", "hint")
        hint.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        column.addWidget(hint)
        return panel

    # ------------------------------------------------------------- navegación

    def goto(self, k):
        # Se agrupa con un temporizador: al arrastrar la barra solo se decodifica
        # el último frame pedido y la interfaz no se queda atrás.
        self._target = max(0, min(k, self.reader.count - 1))
        self._nav_timer.start()

    def _show_target(self):
        image = self.reader.frame(self._target)
        if image is None:
            self.statusBar().showMessage(f"No se pudo leer el frame {self._target}", 6000)
            self._target = self.frame
        else:
            self.frame = self._target
            lut = self.levels.lut()
            self.view.set_image(image if lut is None else lut[image])
        self._refresh()

    def _refresh(self):
        for widget in (self.slider, self.frame_field):
            widget.blockSignals(True)
            widget.setValue(self.frame)
            widget.blockSignals(False)
        self.time_label.setText(f"{self.frame / self.fps:.3f} s")
        self.levels_label.setText(f"{self.levels.black} · {self.levels.gamma:.2f} · {self.levels.white}")

        for which, (box, jump) in enumerate(zip(self.tick_boxes, self.tick_jumps)):
            mine = self.ticks[which]
            box.blockSignals(True)
            box.setChecked(mine == self.frame)
            box.setEnabled(self._tick_available(which))
            box.blockSignals(False)
            jump.setVisible(mine is not None and mine != self.frame)
            jump.setText(f"→ {mine}")

        if self.frame in self.points:
            x, y = self._output_xy(*self.points[self.frame])
            self.point_label.setText(f"Este frame: x = {x}, y = {y}")
        else:
            self.point_label.setText("Este frame: sin punto")
        self.count_label.setText(f"Marcados: {len(self.points)}")

        predicted = predict(self.points, self.frame, self.reader.count - 1)
        self.view.set_overlay(self.points, self.frame, predicted)

    # ------------------------------------------------------- puntos y marcas

    def _on_left_click(self, x, y):
        self.points[self.frame] = (x, y)
        self.dirty = True
        self._refresh()

    def _on_right_click(self):
        if self.points.pop(self.frame, None) is not None:
            self.ticks = [None if tick == self.frame else tick for tick in self.ticks]
            self.dirty = True
            self._refresh()

    def _tick_available(self, which):
        """Cada marca va sobre un punto, una sola vez, y el primero antes que el último."""
        mine, other = self.ticks[which], self.ticks[1 - which]
        if mine == self.frame:
            return True
        if mine is not None or self.frame not in self.points:
            return False
        if other is None:
            return True
        return self.frame < other if which == FIRST else self.frame > other

    def _on_tick(self, which, checked):
        self.ticks[which] = self.frame if checked else None
        self.dirty = True
        self._refresh()

    # ------------------------------------------------------- vista y metadatos

    def _shown_size(self):
        w, h = self.size_unrotated
        return (h, w) if self.rotation in (90, 270) else (w, h)

    def _apply_geometry(self):
        w, h = self.size_unrotated
        stretch = (w / self.reader.width, h / self.reader.height)
        self.view.set_geometry(self.rotation, stretch)
        self._refresh()

    def _rotate(self):
        self.rotation = (self.rotation + 90) % 360
        for field, value in zip((self.width_field, self.height_field), self._shown_size()):
            field.blockSignals(True)
            field.setValue(value)
            field.blockSignals(False)
        self.rotate_button.setText(f"Rotar 90° (ahora {self.rotation}°)"
                                   if self.rotation else "Rotar 90°")
        self._apply_geometry()

    def _on_size_edited(self):
        w, h = self.width_field.value(), self.height_field.value()
        self.size_unrotated = (h, w) if self.rotation in (90, 270) else (w, h)
        self._apply_geometry()

    def _on_fps_edited(self, value):
        self.fps = value
        self._refresh()

    def _output_xy(self, x, y):
        """Pasa de píxel del vídeo original a píxel del vídeo tal como se ve."""
        w, h = self.size_unrotated
        px = math.floor((x + 0.5) * w / self.reader.width)
        py = math.floor((y + 0.5) * h / self.reader.height)
        if self.rotation == 90:
            return h - 1 - py, px
        if self.rotation == 180:
            return w - 1 - px, h - 1 - py
        if self.rotation == 270:
            return py, w - 1 - px
        return px, py

    # ---------------------------------------------------------------- exportar

    def _export(self):
        if len(self.points) < MIN_EXPORT_POINTS:
            QMessageBox.information(self, "Exportar",
                                    f"Hacen falta al menos {MIN_EXPORT_POINTS} puntos para exportar "
                                    f"(ahora hay {len(self.points)}).")
            return
        first, last = self.ticks
        if ((first is not None and min(self.points) < first)
                or (last is not None and max(self.points) > last)):
            answer = QMessageBox.question(self, "Exportar",
                                          "Hay puntos antes del marcado como primero o después del "
                                          "marcado como último. ¿Exportar de todos modos?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        name = self.name_field.text().strip()
        if name.lower().endswith(".xlsx"):
            name = name[:-5]
        name = name.rstrip(". ") or self.path.stem
        if any(c in name for c in INVALID_NAME_CHARS):
            QMessageBox.warning(self, "Exportar",
                                f"El nombre no puede contener: {INVALID_NAME_CHARS}")
            return
        self.name_field.setText(name)

        target = self.path.with_name(name + ".xlsx")
        if target.exists():
            answer = QMessageBox.question(self, "Exportar",
                                          f"{target.name} ya existe. ¿Quieres reemplazarlo?")
            if answer != QMessageBox.StandardButton.Yes:
                return

        marks = {first: "primero", last: "último"}
        rows = [(f, *self._output_xy(*self.points[f]), marks.get(f)) for f in sorted(self.points)]
        width, height = self._shown_size()
        info = [("Vídeo", self.path.name),
                ("FPS", self.fps),
                ("FPS variable", "sí" if self.reader.variable_fps else "no"),
                ("Ancho", width),
                ("Alto", height),
                ("Rotación (grados)", self.rotation),
                ("Primer frame", "no marcado" if first is None else first),
                ("Último frame", "no marcado" if last is None else last),
                ("Frames del vídeo", self.reader.count),
                ("Coordenadas", "píxeles; origen arriba a la izquierda, y hacia abajo")]
        try:
            write_excel(target, rows, info)
        except OSError as error:
            QMessageBox.critical(self, "Exportar",
                                 f"No se pudo guardar {target.name}.\n"
                                 f"¿Está abierto en Excel?\n\n{error}")
            return
        self.dirty = False
        self.statusBar().showMessage(f"Exportados {len(rows)} puntos a {target}", 10000)

    def closeEvent(self, event):
        if self.dirty:
            answer = QMessageBox.question(self, "Salir",
                                          "Hay cambios sin exportar. ¿Salir de todos modos?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()


def open_reader(path):
    dialog = QProgressDialog("Leyendo el vídeo…", "", 0, 100)
    dialog.setCancelButton(None)
    dialog.setWindowTitle("Ballistiq")
    dialog.setMinimumDuration(300)
    dialog.setWindowModality(Qt.WindowModality.ApplicationModal)

    def progress(fraction):
        dialog.setValue(int(fraction * 99))
        QApplication.processEvents()

    try:
        return VideoReader(path, progress)
    finally:
        dialog.close()


def main():
    parser = argparse.ArgumentParser(
        prog="ballistiq",
        description="Marca un objeto frame a frame en un vídeo y exporta sus píxeles a Excel.")
    parser.add_argument("video", nargs="?", help="vídeo que abrir (si no se indica, lo pregunta)")
    parser.add_argument("--version", action="version", version=f"ballistiq {__version__}")
    path = parser.parse_args().video

    signal.signal(signal.SIGINT, signal.SIG_DFL)   # que Ctrl+C cierre desde la terminal
    if QT_VERSION < 0x060000:   # en Qt6 ya viene activado
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling)
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setPalette(_light_palette())
    app.setStyleSheet(STYLE)

    reader = None
    while reader is None:
        if path is None:
            path, _ = QFileDialog.getOpenFileName(None, "Elige un vídeo", "", VIDEO_FILTER)
            if not path:
                return 0
        try:
            reader = open_reader(path)
        except Exception as error:  # cualquier fallo al abrir se le explica al usuario
            QMessageBox.critical(None, "Ballistiq", f"No se pudo abrir el vídeo:\n{path}\n\n{error}")
            path = None

    window = MainWindow(reader, path)
    window.show()
    window.raise_()
    window.activateWindow()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
