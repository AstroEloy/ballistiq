#!/usr/bin/env python3
"""Ballistiq: marca a mano la posición de un objeto en cada frame de un vídeo
y exporta la tabla frame -> píxel a un Excel que queda junto al vídeo.

Los frames se numeran desde 0. Las coordenadas son píxeles del vídeo tal como se
ve (con la rotación y el ancho/alto indicados), con dos decimales y con origen arriba a
la izquierda: el píxel n va de n a n + 1.
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
                                 QMessageBox, QProgressDialog, QPushButton, QScrollArea, QSizePolicy,
                                 QSlider, QSpinBox, QStyle, QStyleOptionButton, QStyleOptionSlider,
                                 QVBoxLayout, QWidget)
except ImportError as qt6_error:
    # Reserva para sistemas sin Qt6 que traen PyQt5 en sus propios paquetes.
    try:
        from PyQt5.QtCore import QT_VERSION, QPointF, QRectF, Qt, QTimer, pyqtSignal
        from PyQt5.QtGui import (QColor, QImage, QKeySequence, QLinearGradient, QPainter, QPalette,
                                 QPen, QPixmap, QPolygonF, QTransform)
        from PyQt5.QtWidgets import (QAbstractSpinBox, QApplication, QCheckBox, QDoubleSpinBox,
                                     QFileDialog, QFormLayout, QFrame, QGraphicsScene, QGraphicsView,
                                     QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                                     QMessageBox, QProgressDialog, QPushButton, QScrollArea, QShortcut,
                                     QSizePolicy, QSlider, QSpinBox, QStyle, QStyleOptionButton,
                                     QStyleOptionSlider, QVBoxLayout, QWidget)
    except ImportError:
        raise qt6_error

__version__ = "1.4.0"

PREDICTED_FRAMES = 5   # cuántos frames futuros se predicen
FIT_POINTS = 5         # con cuántos puntos recientes se calcula la predicción
CACHE_BYTES = 512 * 1024 * 1024
ZOOM_STEP = 1.25
ZOOM_MIN, ZOOM_MAX = 0.02, 64.0
MIN_EXPORT_POINTS = 2
PANEL_WIDTH, PANEL_MARGIN = 270, 14
PANEL_TEXT_WIDTH = PANEL_WIDTH - 2 * PANEL_MARGIN - 20   # con sitio para su barra de desplazamiento
NOTICE_MS = 10000      # cuánto se muestra un aviso, en milisegundos
RELEASE, IMPACT = 0, 1     # posición de cada anotación en MainWindow.ticks

POINT_COLOR = QColor("#ff3b30")
PREDICTED_COLOR = QColor("#00d1ff")
FIT_COLOR = QColor("#ffd60a")
HALO_COLOR = QColor(0, 0, 0, 170)
ACTIVE_RADIUS, PAST_RADIUS = 14, 5
PREDICTED_MIN_GAP = 12  # separación mínima en pantalla entre círculos de predicción

VIDEO_FILTER = ("Vídeos (*.mp4 *.mov *.avi *.mkv *.m4v *.wmv *.mpg *.mpeg *.webm "
                "*.MP4 *.MOV *.AVI *.MKV *.M4V *.WMV *.MPG *.MPEG *.WEBM);;"
                "Todos los archivos (*)")
INVALID_NAME_CHARS = '<>:"/\\|?*'
PAN_KEYS = (Qt.Key.Key_Space, Qt.Key.Key_Control, Qt.Key.Key_Meta)   # con ellas, arrastrar mueve
# En macOS, Qt llama Control a la tecla Command y Meta a la tecla Control.
PAN_MODIFIERS = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier
PAN_KEY_NAME = "⌘ o Ctrl" if sys.platform == "darwin" else "Ctrl"
FFMPEG_ERROR = getattr(av, "FFmpegError", av.error.FFmpegError)  # el nombre cambia según la versión

STYLE = """
#panel { background: #f5f5f7; border-left: 1px solid #d9d9de; }
#navbar { background: #f5f5f7; border-top: 1px solid #d9d9de; }
QLabel[role="title"] { color: #6e6e73; font-size: 11px; font-weight: 600; margin-top: 8px; }
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


def fit_line(points, width, height):
    """Recta que mejor se ajusta a todos los puntos, de borde a borde de la imagen.

    Es un ajuste ortogonal: minimiza la distancia a la recta, no solo en la vertical, así
    que vale para cualquier orientación, también para trayectorias verticales. Devuelve
    sus dos extremos, o None si no hay al menos dos puntos distintos.
    """
    xy = np.array(list(points.values()), dtype=float).reshape(-1, 2)
    if len(xy) < 2 or np.ptp(xy, axis=0).max() == 0:
        return None
    center = xy.mean(axis=0)
    direction = np.linalg.svd(xy - center, full_matrices=False)[2][0]
    t_min, t_max = -np.inf, np.inf
    for c, d, size in zip(center, direction, (width, height)):
        if abs(d) > 1e-12:
            low, high = sorted(((0 - c) / d, (size - c) / d))
            t_min, t_max = max(t_min, low), min(t_max, high)
    return tuple(center + t_min * direction), tuple(center + t_max * direction)


def write_excel(path, rows, info):
    """Hoja "Puntos": frame, pixel x, pixel y, anotación. Hoja "Info": datos de la medida."""
    book = Workbook()
    sheet = book.active
    sheet.title = "Puntos"
    sheet.append(["frame", "pixel x", "pixel y", "anotación"])
    for row in rows:
        sheet.append(list(row))
    sheet.freeze_panes = "A2"
    details = book.create_sheet("Info")
    for row in info:
        details.append(list(row))
    details.column_dimensions["A"].width = 18
    details.column_dimensions["B"].width = 40
    book.save(path)


def _exact_pos(event):
    """Posición del ratón con decimales: en pantallas de alta densidad hay fracciones de píxel."""
    return event.position() if hasattr(event, "position") else event.localPos()   # Qt6 / Qt5


def _event_pos(event):
    return _exact_pos(event).toPoint()


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

    leftClicked = pyqtSignal(float, float)
    rightClicked = pyqtSignal()
    zoomChanged = pyqtSignal(float)
    hovered = pyqtSignal(object)   # punto de la imagen bajo el ratón, o None si está fuera

    def __init__(self, width, height):
        super().__init__()
        self._image_size = (width, height)
        self._rotation = 0
        self._stretch = (1.0, 1.0)
        self._zoom = 1.0
        self._fit_mode = True
        self._image_rect = QRectF(0, 0, width, height)
        self._pan_from = None
        self._pan_keys = set()
        self._zoom_anchor = None    # (posición en pantalla, punto de la imagen) del zoom en curso
        self._points, self._frame, self._predicted, self._fit_line = {}, 0, [], None
        self._show_guides = True

        self.setScene(QGraphicsScene(self))
        self._item = self.scene().addPixmap(QPixmap())
        self.setSceneRect(self._image_rect)
        self.setBackgroundBrush(QColor("#1c1c1e"))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        # La posición la lleva _apply_transform; se mueve arrastrando, sin barras.
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.viewport().setMouseTracking(True)
        self.viewport().setCursor(Qt.CursorShape.CrossCursor)

    def set_image(self, image):
        h, w = image.shape[:2]
        qimage = QImage(image.data, w, h, image.strides[0], QImage.Format.Format_RGB888)
        self._item.setPixmap(QPixmap.fromImage(qimage))

    def set_overlay(self, points, frame, predicted, line=None):
        self._points, self._frame, self._predicted, self._fit_line = points, frame, predicted, line
        self.viewport().update()

    def set_guides_visible(self, visible):
        self._show_guides = visible
        self.viewport().update()

    def set_geometry(self, rotation, stretch):
        center = self._image_point_at(self._screen_center())
        self._rotation, self._stretch = rotation, stretch
        self._zoom_anchor = None
        if self._fit_mode:
            self.fit()
        else:
            self._apply_transform(center)

    def _screen_center(self):
        return QRectF(self.viewport().rect()).center()

    def _image_point_at(self, screen):
        return self.viewportTransform().inverted()[0].map(QPointF(screen))

    def _base_transform(self):
        transform = QTransform()
        transform.rotate(self._rotation)
        transform.scale(*self._stretch)
        return transform

    def _apply_transform(self, fixed=None, screen=None):
        """Aplica zoom y giro dejando el punto `fixed` de la imagen en `screen` de la pantalla.

        Por defecto, el centro de la imagen en el centro de la pantalla.
        """
        transform = self._base_transform() * QTransform.fromScale(self._zoom, self._zoom)
        self.setTransform(transform)
        inverse = transform.inverted()[0]
        # Con un margen de una pantalla, cualquier punto de la imagen puede estar bajo el
        # cursor. Sin él, Qt la mantiene centrada mientras cabe y el zoom se aleja del cursor.
        visible = inverse.mapRect(QRectF(self.viewport().rect()))
        self.setSceneRect(self._image_rect.adjusted(-visible.width(), -visible.height(),
                                                    visible.width(), visible.height()))
        fixed = self._image_rect.center() if fixed is None else fixed
        where = self._screen_center() if screen is None else QPointF(screen)
        scroll = transform.map(fixed) - where
        self.horizontalScrollBar().setValue(round(scroll.x()))
        self.verticalScrollBar().setValue(round(scroll.y()))
        self._item.setTransformationMode(
            Qt.TransformationMode.SmoothTransformation if self._zoom < 1.5 else Qt.TransformationMode.FastTransformation)
        self.zoomChanged.emit(self._zoom)

    def fit(self):
        self._fit_mode = True
        self._zoom_anchor = None
        shown = self._base_transform().mapRect(self._image_rect)
        self._zoom = 0.99 * min(self.width() / shown.width(), self.height() / shown.height())
        self._apply_transform()

    def _anchored_point(self, screen):
        """Punto de la imagen que hay en `screen`.

        Mientras el zoom siga en el mismo sitio se usa siempre el mismo punto: medirlo en
        cada paso acumularía el redondeo a píxeles y la imagen se iría desplazando.
        """
        screen = QPointF(screen)
        if self._zoom_anchor is None or self._zoom_anchor[0] != screen:
            self._zoom_anchor = (screen, self._image_point_at(screen))
        return self._zoom_anchor[1]

    def zoom_by(self, factor, screen=None):
        """Cambia el zoom sin mover lo que hay en `screen`; por defecto, el centro de la pantalla."""
        screen = self._screen_center() if screen is None else screen
        fixed = self._anchored_point(screen)
        self._fit_mode = False
        self._zoom = min(max(self._zoom * factor, ZOOM_MIN), ZOOM_MAX)
        self._apply_transform(fixed, screen)

    def resizeEvent(self, event):
        old = event.oldSize()
        center = self._image_point_at(QPointF(old.width() / 2, old.height() / 2))
        super().resizeEvent(event)
        self._zoom_anchor = None
        if self._fit_mode:
            self.fit()
        else:
            self._apply_transform(center)

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta:
            self.zoom_by(1.0015 ** delta, _exact_pos(event))
            self._report_hover(_exact_pos(event))

    def _report_hover(self, screen):
        point = self._image_point_at(screen)
        self.hovered.emit(point if self._image_rect.contains(point) else None)

    def leaveEvent(self, event):
        self.hovered.emit(None)
        super().leaveEvent(event)

    def _is_pan_click(self, event):
        button = event.button()
        if button == Qt.MouseButton.MiddleButton:
            return True
        with_modifier = bool(event.modifiers() & PAN_MODIFIERS)
        if button == Qt.MouseButton.LeftButton:
            return with_modifier or Qt.Key.Key_Space in self._pan_keys
        # macOS convierte Control + clic en clic derecho: aquí debe mover, no borrar el punto.
        return button == Qt.MouseButton.RightButton and with_modifier

    def mousePressEvent(self, event):
        button = event.button()
        if self._is_pan_click(event):
            self._pan_from = _event_pos(event)
            self._zoom_anchor = None
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        elif button == Qt.MouseButton.LeftButton:
            point = self._image_point_at(_exact_pos(event))
            if self._image_rect.contains(point):
                self.leftClicked.emit(point.x(), point.y())
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
        self._report_hover(_exact_pos(event))

    def mouseReleaseEvent(self, event):
        if self._pan_from is not None:
            self._pan_from = None
            self._update_cursor()

    def keyPressEvent(self, event):
        if event.key() in PAN_KEYS:
            if not event.isAutoRepeat():
                self._pan_keys.add(event.key())
                self._update_cursor()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() in PAN_KEYS and not event.isAutoRepeat():
            self._pan_keys.discard(event.key())
            self._update_cursor()

    def focusOutEvent(self, event):
        self._pan_keys.clear()
        self._update_cursor()
        super().focusOutEvent(event)

    def _update_cursor(self):
        self.viewport().setCursor(Qt.CursorShape.OpenHandCursor if self._pan_keys
                                  else Qt.CursorShape.CrossCursor)

    def drawForeground(self, painter, rect):
        to_view = self.viewportTransform().map

        def center(x, y):  # en coordenadas de pantalla
            return to_view(QPointF(x, y))

        painter.save()
        painter.resetTransform()   # tamaño de las marcas fijo, sea cual sea el zoom
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        marked = sorted(self._points)
        if self._show_guides:
            if self._fit_line:   # lo primero, para que quede al fondo
                start, end = (to_view(QPointF(*p)) for p in self._fit_line)
                _stroked(painter, FIT_COLOR, 1.2, lambda: painter.drawLine(start, end))
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


class BoxedCheckBox(QCheckBox):
    """Casilla con el borde bien visible: el de Fusion casi no se ve sobre un panel claro."""

    def paintEvent(self, event):
        super().paintEvent(event)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        box = self.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, self)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#8e8e93" if self.isEnabled() else "#c7c7cc"), 1))
        painter.drawRoundedRect(QRectF(box).adjusted(0.5, 0.5, -0.5, -0.5), 2, 2)


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
        self.points = {}               # frame -> (x, y) exactos, en píxeles del vídeo original
        self.ticks = [None, None]      # frame de la suelta y del impacto
        self.rotation = 0
        self.size_unrotated = (reader.width, reader.height)
        self.fps = reader.fps
        self.dirty = False

        self._nav_timer = QTimer(self, singleShot=True, interval=0)
        self._nav_timer.timeout.connect(self._show_target)
        self._notice_timer = QTimer(self, singleShot=True, interval=NOTICE_MS)

        self.setWindowTitle(f"Ballistiq · {self.path.name}")
        self.view = VideoView(reader.width, reader.height)
        self.view.leftClicked.connect(self._on_left_click)
        self.view.rightClicked.connect(self._on_right_click)
        self.view.zoomChanged.connect(lambda z: self.zoom_label.setText(f"{z * 100:.0f} %"))
        self.view.hovered.connect(self._on_hover)

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
        screen = QApplication.primaryScreen().availableSize()
        self.resize(min(1200, screen.width()), min(760, screen.height() - 40))   # 40: barra de título

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
        for text, action in (("−", lambda: self.view.zoom_by(1 / ZOOM_STEP)),
                             ("+", lambda: self.view.zoom_by(ZOOM_STEP)),
                             ("Ajustar", self.view.fit)):
            button = self._button(text, action)
            button.setMinimumWidth(30)   # el mínimo del estilo Fusion, 80, no cabría en el panel
            zoom_row.addWidget(button)
        self.zoom_label = QLabel("100 %")
        self.zoom_label.setMinimumWidth(46)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        zoom_row.addWidget(self.zoom_label)
        column.addLayout(zoom_row)
        self.guides_box = BoxedCheckBox("Mostrar líneas guía")
        self.guides_box.setChecked(True)
        self.guides_box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.guides_box.setToolTip("Línea roja entre puntos, recta de ajuste amarilla y predicción "
                                   "azul. Los puntos se ven siempre.")
        self.guides_box.toggled.connect(self.view.set_guides_visible)
        column.addWidget(self.guides_box)

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

        title("Anotaciones")
        self.tick_boxes, self.tick_jumps = [], []
        for which, (text, event) in enumerate((("Suelta", "la suelta"), ("Impacto", "el impacto"))):
            box = BoxedCheckBox(text)
            box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            box.setToolTip(f"Marca el punto de este frame como {event}. "
                           "Déjalo sin marcar si no se ve en este vídeo.")
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
        self.hover_label = QLabel()
        self._on_hover(None)
        column.addWidget(self.hover_label)
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
        self.export_note = QLabel("Se guarda en la carpeta del vídeo:")
        self.export_note.setProperty("role", "hint")
        column.addWidget(self.export_note)
        folder = str(self.path.resolve().parent)
        self.folder_label = QLabel()
        self.folder_label.setProperty("role", "hint")
        self.folder_label.setToolTip(folder)
        self.folder_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.folder_label.ensurePolished()   # para medir el texto con su tamaño real
        self.folder_label.setText(self.folder_label.fontMetrics().elidedText(
            folder, Qt.TextElideMode.ElideMiddle, PANEL_TEXT_WIDTH))
        column.addWidget(self.folder_label)
        export_button = self._button("Exportar a Excel", self._export)
        export_button.setObjectName("export")
        column.addWidget(export_button)
        # Con su hueco reservado siempre, para que un aviso no mueva nada de la ventana.
        self.notice_label = QLabel()
        self.notice_label.setProperty("role", "hint")
        self.notice_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.notice_label.ensurePolished()
        self.notice_label.setFixedHeight(self.notice_label.fontMetrics().height() + 2)
        self._notice_timer.timeout.connect(self.notice_label.clear)
        column.addWidget(self.notice_label)

        column.addStretch(1)
        hint = QLabel("Clic izquierdo: poner punto\n"
                      "Clic derecho: quitar el punto\n"
                      "← →: frame anterior / siguiente\n"
                      "Rueda: zoom hacia el cursor\n"
                      "Rueda pulsada + arrastrar: mover\n"
                      f"{PAN_KEY_NAME} + arrastrar: mover")
        hint.setProperty("role", "hint")
        hint.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        column.addWidget(hint)

        # Dentro de un área con desplazamiento, para que la ventana quepa en pantallas pequeñas.
        area = QScrollArea()
        area.setWidget(panel)
        area.setWidgetResizable(True)
        area.setFixedWidth(PANEL_WIDTH)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return area

    # ------------------------------------------------------------- navegación

    def goto(self, k):
        # Se agrupa con un temporizador: al arrastrar la barra solo se decodifica
        # el último frame pedido y la interfaz no se queda atrás.
        self._target = max(0, min(k, self.reader.count - 1))
        self._nav_timer.start()

    def _show_target(self):
        image = self.reader.frame(self._target)
        if image is None:
            self._notify(f"No se pudo leer el frame {self._target}")
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
            self.point_label.setText(f"Este frame: x = {x:.2f}, y = {y:.2f}")
        else:
            self.point_label.setText("Este frame: sin punto")
        self.count_label.setText(f"Marcados: {len(self.points)}")

        predicted = predict(self.points, self.frame, self.reader.count - 1)
        line = fit_line(self.points, self.reader.width, self.reader.height)
        self.view.set_overlay(self.points, self.frame, predicted, line)

    def _notify(self, text):
        label = self.notice_label
        label.setText(label.fontMetrics().elidedText(text, Qt.TextElideMode.ElideMiddle,
                                                     PANEL_TEXT_WIDTH))
        label.setToolTip(text)
        self._notice_timer.start()

    def _on_hover(self, point):
        if point is None:
            self.hover_label.setText("Ratón: fuera de la imagen")
        else:
            x, y = self._output_xy(point.x(), point.y())
            self.hover_label.setText(f"Ratón: x = {x:.2f}, y = {y:.2f}")

    # --------------------------------------------------- puntos y anotaciones

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
        """Cada anotación va sobre un punto, una sola vez, y la suelta antes que el impacto."""
        mine, other = self.ticks[which], self.ticks[1 - which]
        if mine == self.frame:
            return True
        if mine is not None or self.frame not in self.points:
            return False
        if other is None:
            return True
        return self.frame < other if which == RELEASE else self.frame > other

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
        """Pasa de un punto del vídeo original a coordenadas del vídeo tal como se ve."""
        w, h = self.size_unrotated
        x, y = x * w / self.reader.width, y * h / self.reader.height
        if self.rotation == 90:
            x, y = h - y, x
        elif self.rotation == 180:
            x, y = w - x, h - y
        elif self.rotation == 270:
            x, y = y, w - x
        return round(x, 2), round(y, 2)

    # ---------------------------------------------------------------- exportar

    def _export(self):
        if len(self.points) < MIN_EXPORT_POINTS:
            QMessageBox.information(self, "Exportar",
                                    f"Hacen falta al menos {MIN_EXPORT_POINTS} puntos para exportar "
                                    f"(ahora hay {len(self.points)}).")
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

        release, impact = self.ticks
        events = {release: "suelta", impact: "impacto"}
        rows = [(f, *self._output_xy(*self.points[f]), events.get(f)) for f in sorted(self.points)]
        width, height = self._shown_size()
        info = [("Vídeo", self.path.name),
                ("FPS", self.fps),
                ("FPS variable", "sí" if self.reader.variable_fps else "no"),
                ("Ancho", width),
                ("Alto", height),
                ("Rotación (grados)", self.rotation),
                ("Frame de suelta", "no marcado" if release is None else release),
                ("Frame de impacto", "no marcado" if impact is None else impact),
                ("Frames del vídeo", self.reader.count),
                ("Coordenadas", "píxeles con decimales; origen arriba a la izquierda, y hacia abajo; "
                                "el píxel n va de n a n + 1")]
        try:
            write_excel(target, rows, info)
        except OSError as error:
            QMessageBox.critical(self, "Exportar",
                                 f"No se pudo guardar {target.name}.\n"
                                 f"¿Está abierto en Excel?\n\n{error}")
            return
        self.dirty = False
        self._notify(f"Exportado: {target.name}")

    def closeEvent(self, event):
        if self.dirty:
            answer = QMessageBox.question(self, "Salir",
                                          "Hay cambios sin exportar. ¿Salir de todos modos?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()


def apply_style(app):
    app.setStyle("Fusion")
    app.setPalette(_light_palette())
    app.setStyleSheet(STYLE)


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
    apply_style(app)

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
