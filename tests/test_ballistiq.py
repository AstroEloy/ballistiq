"""Pruebas sobre la ventana real, con clics y teclas simulados y vídeos generados al vuelo."""

import math
import os
import random
from fractions import Fraction
from importlib import import_module

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import av
import numpy as np
import openpyxl
import pytest

import ballistiq as app_module
from ballistiq import (MainWindow, QApplication, QImage, QMessageBox, QPointF, Qt, VideoReader,
                       fit_line, predict)

# Las clases que solo usan las pruebas, de la misma librería de Qt que use el programa
_binding = Qt.__module__.split(".")[0]
QEvent = import_module(_binding + ".QtCore").QEvent
QPoint = import_module(_binding + ".QtCore").QPoint
QMouseEvent = import_module(_binding + ".QtGui").QMouseEvent
QWheelEvent = import_module(_binding + ".QtGui").QWheelEvent
QTest = import_module(_binding + ".QtTest").QTest
QStatusBar = import_module(_binding + ".QtWidgets").QStatusBar

WIDTH, HEIGHT, FRAMES = 640, 360, 120
BALL_COLOR = (240, 200, 40)


def ball_position(i):
    return (40 + i * (WIDTH - 80) // (FRAMES - 1),
            HEIGHT - 40 - i * (HEIGHT - 120) // (FRAMES - 1))


def picture(i):
    """Fotograma con su número grabado en bloques y una bola en posición conocida."""
    image = np.full((HEIGHT, WIDTH, 3), 40, np.uint8)
    for bit in range(10):
        image[0:16, bit * 16:(bit + 1) * 16] = 255 if (i >> bit) & 1 else 0
    cx, cy = ball_position(i)
    ys, xs = np.ogrid[:HEIGHT, :WIDTH]
    image[(xs - cx) ** 2 + (ys - cy) ** 2 <= 49] = BALL_COLOR
    return image


def frame_number(image):
    return sum((1 if image[8, bit * 16 + 8].mean() > 127 else 0) << bit for bit in range(10))


def make_video(path, seconds_of_frame):
    """H.264 con B-frames, con el instante de cada frame que se indique."""
    time_base = Fraction(1, 90000)
    with av.open(str(path), "w") as container:
        if "libx264" in av.codecs_available:
            stream = container.add_stream("libx264", rate=30, options={"bf": "2", "g": "24", "crf": "10"})
        else:   # no todas las compilaciones de av traen x264
            stream = container.add_stream("mpeg4", rate=30, options={"bf": "2", "g": "24", "qscale": "2"})
        stream.width, stream.height, stream.pix_fmt = WIDTH, HEIGHT, "yuv420p"
        stream.time_base = time_base
        stream.codec_context.time_base = time_base
        for i in range(FRAMES):
            frame = av.VideoFrame.from_ndarray(picture(i), format="rgb24")
            frame.pts = round(seconds_of_frame(i) * 90000)
            frame.time_base = time_base
            container.mux(stream.encode(frame))
        container.mux(stream.encode())


def constant_rate(i):
    return i / 30


def slow_motion_section(i):
    return i * 0.01 if i < 60 else 0.6 + (i - 60) * 0.2


@pytest.fixture(scope="session")
def qapp():
    application = QApplication.instance() or QApplication([])
    app_module.apply_style(application)   # el mismo aspecto que el programa real
    return application


@pytest.fixture(scope="session")
def video(tmp_path_factory):
    path = tmp_path_factory.mktemp("videos") / "disparo_01.mp4"
    make_video(path, constant_rate)
    return path


@pytest.fixture
def dialogs(monkeypatch):
    """Sustituye los cuadros de diálogo: guarda los textos y responde lo indicado."""
    class Dialogs:
        shown = []
        answers = []
    recorder = Dialogs()
    recorder.shown, recorder.answers = [], []

    def show(*args, **kwargs):
        recorder.shown.append(args[2])

    def ask(*args, **kwargs):
        recorder.shown.append(args[2])
        return recorder.answers.pop(0)

    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(show))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(ask))
    return recorder


@pytest.fixture
def window(qapp, video, tmp_path, dialogs):
    copy = tmp_path / video.name
    copy.write_bytes(video.read_bytes())
    win = MainWindow(VideoReader(copy), copy)
    win.resize(1200, 760)     # tamaño fijo: la ventana se adapta a la pantalla, que aquí es virtual
    win.show()
    QTest.qWaitForWindowActive(win, 1000)
    pump()
    yield win
    win.dirty = False
    win.close()


def pump():
    for _ in range(5):
        QApplication.processEvents()


def click(win, x, y, button=Qt.MouseButton.LeftButton):
    position = win.view.mapFromScene(QPointF(x + 0.5, y + 0.5))
    QTest.mouseClick(win.view.viewport(), button, Qt.KeyboardModifier.NoModifier, position)
    pump()


def pixels(points):
    """Píxel en el que cae cada punto: un clic en pantalla no da un valor exacto de la imagen."""
    return {frame: (math.floor(x), math.floor(y)) for frame, (x, y) in points.items()}


def floored(row):
    return (row[0], math.floor(row[1]), math.floor(row[2])) + tuple(row[3:])


def press(win, key):
    QTest.keyClick(win.view, key)
    pump()


def go(win, k):
    win.goto(k)
    pump()


def drag(widget, button, start, end, modifier=Qt.KeyboardModifier.NoModifier):
    """Pulsa, mueve con el botón pulsado y suelta, con los eventos que manda el sistema."""
    QTest.mousePress(widget, button, modifier, start)
    move = QMouseEvent(QEvent.Type.MouseMove, QPointF(end), QPointF(widget.mapToGlobal(end)),
                       Qt.MouseButton.NoButton, button, modifier)
    QApplication.sendEvent(widget, move)
    QTest.mouseRelease(widget, button, modifier, end)
    pump()


def wheel(view, position, delta=120):
    viewport = view.viewport()
    event = QWheelEvent(QPointF(position), QPointF(viewport.mapToGlobal(position)), QPoint(0, 0),
                        QPoint(0, delta), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                        Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(viewport, event)
    pump()


def distance(a, b):
    return abs(a.x() - b.x()) + abs(a.y() - b.y())


def tick_click(box):
    QTest.mouseClick(box, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(8, box.height() // 2))
    pump()


def as_rgb(qimage):
    image = qimage.convertToFormat(QImage.Format.Format_RGB888)
    bits = image.constBits()
    bits.setsize(image.sizeInBytes())
    rows = np.frombuffer(bits, np.uint8).reshape(image.height(), image.bytesPerLine())
    return rows[:, :image.width() * 3].reshape(image.height(), image.width(), 3).copy()


def centroid(image):
    mask = np.abs(image.astype(int) - BALL_COLOR).sum(axis=2) < 70
    ys, xs = np.nonzero(mask)
    return xs.mean(), ys.mean()


def sheet_rows(path, name="Puntos"):
    return [tuple(row) for row in openpyxl.load_workbook(path)[name].iter_rows(values_only=True)]


# ------------------------------------------------------------------ lectura de vídeo

@pytest.mark.parametrize("timing", [constant_rate, slow_motion_section])
def test_cualquier_acceso_devuelve_el_frame_exacto(tmp_path, timing):
    path = tmp_path / "v.mp4"
    make_video(path, timing)
    reader = VideoReader(path)
    assert reader.count == FRAMES
    random.seed(1)
    order = list(range(30)) + [random.randrange(FRAMES) for _ in range(120)] \
        + list(range(FRAMES - 1, 80, -1)) + [0, FRAMES - 1, 0]
    for k in order:
        assert frame_number(reader.frame(k)) == k


def test_detecta_fps_variable(tmp_path):
    constant, variable = tmp_path / "c.mp4", tmp_path / "v.mp4"
    make_video(constant, constant_rate)
    make_video(variable, slow_motion_section)
    assert not VideoReader(constant).variable_fps
    assert VideoReader(variable).variable_fps


def test_fuera_de_rango_devuelve_none(video):
    reader = VideoReader(video)
    assert reader.frame(-1) is None and reader.frame(FRAMES) is None


def test_archivo_que_no_es_video_da_error(tmp_path):
    path = tmp_path / "nota.txt"
    path.write_text("hola")
    with pytest.raises(Exception):
        VideoReader(path)


# ------------------------------------------------------------------------ predicción

def test_prediccion_necesita_dos_puntos():
    assert predict({}, 0, 99) == []
    assert predict({5: (1, 1)}, 5, 99) == []


def test_prediccion_es_una_recta():
    result = predict({0: (10, 20), 1: (13, 18)}, 1, 99)
    assert [(f, round(x, 6), round(y, 6)) for f, x, y in result] == [
        (2, 16, 16), (3, 19, 14), (4, 22, 12), (5, 25, 10), (6, 28, 8)]


def test_prediccion_respeta_frames_saltados():
    frame, x, _ = predict({0: (0, 0), 10: (100, 50)}, 10, 99)[0]
    assert frame == 11 and x == pytest.approx(110)


def test_prediccion_incluye_el_frame_actual_sin_marcar():
    frame, x, _ = predict({0: (0, 0), 1: (10, 0)}, 4, 99)[0]
    assert frame == 4 and x == pytest.approx(40)


def test_prediccion_para_en_el_siguiente_marcado_y_en_el_ultimo_frame():
    assert [r[0] for r in predict({0: (0, 0), 1: (10, 0), 3: (30, 0)}, 1, 99)] == [2]
    assert [r[0] for r in predict({0: (0, 0), 1: (10, 0)}, 1, 3)] == [2, 3]


def test_prediccion_usa_los_ultimos_puntos_e_ignora_los_futuros():
    points = {0: (500, 0), 1: (0, 0), 2: (10, 0), 3: (20, 0), 4: (30, 0), 5: (40, 0), 6: (50, 0)}
    assert predict(points, 6, 99)[0][1] == pytest.approx(60)
    assert predict({0: (0, 0), 1: (10, 0), 20: (5000, 0)}, 1, 99)[0][1] == pytest.approx(20)


# ------------------------------------------------------------------- recta de ajuste

def test_recta_necesita_dos_puntos_distintos():
    assert fit_line({}, 100, 100) is None
    assert fit_line({0: (5, 5)}, 100, 100) is None
    assert fit_line({0: (5, 5), 1: (5, 5), 2: (5, 5)}, 100, 100) is None


def on_line(line, point):
    (ax, ay), (bx, by) = line
    px, py = point
    return abs((bx - ax) * (py - ay) - (by - ay) * (px - ax)) / np.hypot(bx - ax, by - ay)


@pytest.mark.parametrize("points", [
    {0: (10, 50), 1: (20, 50), 2: (30, 50)},        # horizontal
    {0: (50, 10), 1: (50, 20), 2: (50, 30)},        # vertical: un ajuste y(x) fallaría aquí
    {0: (10, 10), 1: (20, 20), 2: (40, 40)},        # diagonal
    {0: (10, 90), 1: (30, 70)},                     # solo dos puntos
])
def test_recta_pasa_por_puntos_alineados_y_llega_a_los_bordes(points):
    line = fit_line(points, 100, 100)
    assert all(on_line(line, p) < 1e-9 for p in points.values())
    for end in line:
        assert min(end[0], end[1], 100 - end[0], 100 - end[1]) == pytest.approx(0, abs=1e-9)
        assert all(0 - 1e-9 <= value <= 100 + 1e-9 for value in end)


def test_recta_ajusta_a_todos_los_puntos_y_no_solo_a_los_ultimos():
    # Sube y baja alrededor de y = 50: el ajuste es horizontal aunque el final suba
    points = {i: (10 + i * 10, 50 + (5 if i % 2 else -5)) for i in range(8)}
    (ax, ay), (bx, by) = fit_line(points, 200, 100)
    assert abs((by - ay) / (bx - ax)) < 0.1
    assert abs(ay - 50.5) < 5


def test_recta_es_la_que_menos_distancia_deja():
    random.seed(4)
    points = {i: (i * 4 + random.randint(-2, 2), i * 2 + 10 + random.randint(-2, 2)) for i in range(40)}
    line = fit_line(points, 400, 400)
    best = sum(on_line(line, p) ** 2 for p in points.values())
    (ax, ay), (bx, by) = line
    for dx, dy in ((0, 5), (5, 0), (0, -5), (-5, 0)):
        other = ((ax + dx, ay + dy), (bx - dx, by - dy))
        assert sum(on_line(other, p) ** 2 for p in points.values()) > best


def test_la_recta_usa_todos_los_puntos_en_cualquier_frame(window):
    mark_points(window, 5, 10, 40)
    go(window, 5)
    first_view = window.view._fit_line
    go(window, 100)
    assert window.view._fit_line == first_view == fit_line(window.points, WIDTH, HEIGHT)


def color_near(image, position, wanted, radius=3):
    x, y = position.x(), position.y()
    area = image[y - radius:y + radius + 1, x - radius:x + radius + 1].astype(int)
    return np.abs(area - wanted).sum(axis=2).min()


def test_la_recta_se_pinta_de_borde_a_borde_y_al_fondo(window):
    view = window.view
    click(window, 100, 100)
    go(window, 1)
    click(window, 200, 200)
    pump()
    yellow = (0xff, 0xd6, 0x0a)
    image = as_rgb(view.viewport().grab().toImage())
    # Antes y después de los puntos solo está la recta. La predicción acaba en (300, 300):
    # la siguiente, en (400, 400), ya cae fuera de la imagen.
    for beyond in ((50.5, 50.5), (340.5, 340.5)):
        assert color_near(image, view.mapFromScene(QPointF(*beyond)), yellow) < 60
    between = view.mapFromScene(QPointF(150.5, 150.5))    # entre puntos: la línea roja va encima
    assert color_near(image, between, (0xff, 0x3b, 0x30)) < 60
    assert color_near(image, between, yellow) > 100


def test_sin_puntos_suficientes_no_hay_recta(window):
    click(window, 100, 100)
    assert window.view._fit_line is None
    go(window, 1)
    click(window, 200, 200)
    assert window.view._fit_line is not None
    click(window, 200, 200, Qt.MouseButton.RightButton)
    assert window.view._fit_line is None


# ---------------------------------------------------------------------- líneas guía

YELLOW, RED, CYAN = (0xff, 0xd6, 0x0a), (0xff, 0x3b, 0x30), (0x00, 0xd1, 0xff)


def guides_image(window):
    click(window, 100, 100)
    go(window, 1)
    click(window, 200, 200)
    pump()
    return as_rgb(window.view.viewport().grab().toImage())


def test_la_casilla_de_guias_empieza_activada_y_las_muestra(window):
    assert window.guides_box.isChecked()
    image = guides_image(window)
    view = window.view
    assert color_near(image, view.mapFromScene(QPointF(50.5, 50.5)), YELLOW) < 60      # recta
    assert color_near(image, view.mapFromScene(QPointF(150.5, 150.5)), RED) < 60       # línea roja
    assert color_near(image, view.mapFromScene(QPointF(300.5, 300.5)), CYAN) < 60      # predicción


def test_desactivar_las_guias_las_oculta_pero_no_los_puntos(window):
    guides_image(window)
    view = window.view
    tick_click(window.guides_box)
    assert not window.guides_box.isChecked()
    image = as_rgb(view.viewport().grab().toImage())
    assert color_near(image, view.mapFromScene(QPointF(50.5, 50.5)), YELLOW) > 200
    assert color_near(image, view.mapFromScene(QPointF(150.5, 150.5)), RED) > 200
    assert color_near(image, view.mapFromScene(QPointF(300.5, 300.5)), CYAN) > 200
    assert color_near(image, view.mapFromScene(QPointF(100.5, 100.5)), RED) < 60       # el punto


def test_las_guias_ocultas_siguen_asi_al_cambiar_de_frame_y_se_pueden_recuperar(window):
    guides_image(window)
    view = window.view
    tick_click(window.guides_box)
    go(window, 2)
    press(window, Qt.Key.Key_Right)
    click(window, 250, 250)
    image = as_rgb(view.viewport().grab().toImage())
    assert color_near(image, view.mapFromScene(QPointF(50.5, 50.5)), YELLOW) > 200
    tick_click(window.guides_box)
    image = as_rgb(view.viewport().grab().toImage())
    assert color_near(image, view.mapFromScene(QPointF(50.5, 50.5)), YELLOW) < 60


def test_ocultar_las_guias_no_cambia_lo_que_se_exporta(window):
    guides_image(window)
    expected = sheet_rows(exported(window))
    tick_click(window.guides_box)
    assert pixels(window.points) == {0: (100, 100), 1: (200, 200)}
    window.name_field.setText("sin_guias")
    assert sheet_rows(exported(window, "sin_guias")) == expected


def test_las_casillas_se_ven_aunque_esten_sin_marcar(window):
    # El borde debe distinguirse del fondo claro del panel, también desactivada
    for box, darkest_allowed in ((window.guides_box, 170), (window.tick_boxes[0], 215)):
        if box is window.guides_box:
            tick_click(box)
        assert not box.isChecked()
        image = as_rgb(box.grab().toImage())
        ratio = image.shape[1] / box.width()
        indicator = image[:, :int(18 * ratio)]
        assert indicator.mean(axis=2).min() < darkest_allowed


def test_la_casilla_de_guias_no_quita_las_flechas(window):
    tick_click(window.guides_box)
    press(window, Qt.Key.Key_Right)
    assert window.frame == 1
    assert not window.guides_box.hasFocus()


# ------------------------------------------------------------------ posición del ratón

def hover(view, position):
    viewport = view.viewport()
    event = QMouseEvent(QEvent.Type.MouseMove, QPointF(position), QPointF(viewport.mapToGlobal(QPointF(position).toPoint())),
                        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(viewport, event)
    pump()


def test_el_ratón_se_sigue_sin_pulsar_nada(window):
    assert window.view.viewport().hasMouseTracking()


def test_muestra_la_posicion_del_raton_con_decimales(window):
    view = window.view
    for _ in range(8):
        wheel(view, view.viewport().rect().center())
    position = QPointF(view.viewport().rect().center()) + QPointF(0.25, 0.75)
    hover(view, position)
    point = view._image_point_at(position)
    assert window.hover_label.text() == f"Ratón: x = {point.x():.2f}, y = {point.y():.2f}"
    assert point.x() != int(point.x())


@pytest.mark.parametrize("turns", [0, 1, 2, 3])
def test_se_exporta_lo_que_muestra_el_raton(window, turns):
    view = window.view
    for _ in range(turns):
        window._rotate()
    pump()
    for scene in ((100.3, 200.6), (333.7, 40.2), (620.5, 351.1)):
        position = view.mapFromScene(QPointF(*scene))
        hover(view, position)
        text = window.hover_label.text()
        x, y = (float(part.split("=")[1]) for part in text.replace("Ratón:", "").split(","))
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, position)
        pump()
        assert window._output_xy(*window.points[0]) == pytest.approx((x, y), abs=0.011)


def test_fuera_de_la_imagen_lo_indica(window):
    view = window.view
    view.zoom_by(0.3)
    pump()
    hover(view, QPoint(2, 2))
    assert window.hover_label.text() == "Ratón: fuera de la imagen"
    hover(view, view.viewport().rect().center())
    assert window.hover_label.text().startswith("Ratón: x = ")
    QApplication.sendEvent(view, QEvent(QEvent.Type.Leave))
    pump()
    assert window.hover_label.text() == "Ratón: fuera de la imagen"


def test_hacer_zoom_actualiza_la_posicion_del_raton(window):
    view = window.view
    position = view.viewport().rect().center()
    hover(view, position)
    wheel(view, position)
    point = view._image_point_at(position)
    assert window.hover_label.text() == f"Ratón: x = {point.x():.2f}, y = {point.y():.2f}"


# ----------------------------------------------------------------------- metadatos

def test_lee_metadatos_y_nombre(window):
    assert window.fps_field.value() == pytest.approx(30)
    assert (window.width_field.value(), window.height_field.value()) == (WIDTH, HEIGHT)
    assert (window.slider.minimum(), window.slider.maximum()) == (0, FRAMES - 1)
    assert window.name_field.text() == "disparo_01"


def test_ancho_editado_escala_las_coordenadas_sin_tocar_el_punto(window):
    click(window, 320, 100)
    window.width_field.setValue(WIDTH * 2)
    pump()
    x, y = window.points[0]
    assert pixels(window.points)[0] == (320, 100)
    assert window._output_xy(x, y) == (round(x * 2, 2), round(y, 2))


def test_alto_editado_con_rotacion_va_al_eje_correcto(window):
    window._rotate()
    assert (window.width_field.value(), window.height_field.value()) == (HEIGHT, WIDTH)
    window.height_field.setValue(1000)
    pump()
    assert window.size_unrotated == (1000, HEIGHT)


def test_fps_editado_cambia_el_tiempo(window):
    window.fps_field.setValue(1000)
    pump()
    go(window, 100)
    assert window.time_label.text() == "0.100 s"


# ---------------------------------------------------------------------- navegación

def test_flechas_cambian_de_frame_sin_salirse(window):
    press(window, Qt.Key.Key_Right)
    assert window.frame == 1
    press(window, Qt.Key.Key_Left)
    press(window, Qt.Key.Key_Left)
    assert window.frame == 0
    go(window, FRAMES - 1)
    press(window, Qt.Key.Key_Right)
    assert window.frame == FRAMES - 1


def test_clic_en_la_barra_salta_a_esa_posicion(window):
    slider = window.slider
    QTest.mouseClick(slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(slider.width() // 2, slider.height() // 2))
    pump()
    assert FRAMES * 0.4 < window.frame < FRAMES * 0.6
    assert window.frame_field.value() == window.frame


def test_escribir_el_numero_de_frame(window):
    field = window.frame_field
    field.setFocus()
    field.lineEdit().setText("42")
    QTest.keyClick(field.lineEdit(), Qt.Key.Key_Return)
    pump()
    assert window.frame == 42


# -------------------------------------------------------------------------- puntos

def test_un_solo_punto_por_frame_y_clic_derecho_lo_quita(window):
    click(window, 100, 200)
    assert pixels(window.points) == {0: (100, 200)}
    click(window, 110, 210)
    assert pixels(window.points) == {0: (110, 210)}
    click(window, 5, 5, Qt.MouseButton.RightButton)
    assert window.points == {}
    click(window, 5, 5, Qt.MouseButton.RightButton)
    assert window.points == {}


def test_los_puntos_de_otros_frames_no_se_editan(window):
    click(window, 100, 200)
    press(window, Qt.Key.Key_Right)
    click(window, 300, 300, Qt.MouseButton.RightButton)
    assert pixels(window.points) == {0: (100, 200)}
    click(window, 120, 220)
    assert pixels(window.points) == {0: (100, 200), 1: (120, 220)}


def test_al_volver_a_su_frame_se_puede_editar(window):
    click(window, 100, 200)
    press(window, Qt.Key.Key_Right)
    click(window, 120, 220)
    press(window, Qt.Key.Key_Left)
    click(window, 90, 190)
    assert pixels(window.points) == {0: (90, 190), 1: (120, 220)}
    click(window, 90, 190, Qt.MouseButton.RightButton)
    assert pixels(window.points) == {1: (120, 220)}


def test_dos_clics_rapidos_cuentan_como_dos_clics(window):
    click(window, 100, 200)
    position = QPointF(window.view.mapFromScene(QPointF(150.5, 250.5)))
    viewport = window.view.viewport()
    second_click = QMouseEvent(QEvent.Type.MouseButtonDblClick, position, QPointF(viewport.mapToGlobal(position.toPoint())),
                               Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(viewport, second_click)
    pump()
    assert pixels(window.points) == {0: (150, 250)}


def test_clic_fuera_de_la_imagen_se_ignora(window):
    window.view.zoom_by(0.2)
    pump()
    QTest.mouseClick(window.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(2, 2))
    pump()
    assert window.points == {}


# ----------------------------------------------------------------- zoom y desplazamiento

def test_el_zoom_se_mantiene_al_cambiar_de_frame(window):
    view = window.view
    for _ in range(6):
        view.zoom_by(app_module.ZOOM_STEP)
    view.centerOn(WIDTH / 2, HEIGHT / 2)
    pump()
    transform = view.transform()
    scroll = view.horizontalScrollBar().value(), view.verticalScrollBar().value()
    press(window, Qt.Key.Key_Right)
    go(window, 80)
    go(window, 7)
    assert window.frame == 7
    assert view.transform() == transform
    assert (view.horizontalScrollBar().value(), view.verticalScrollBar().value()) == scroll


def test_rueda_hace_zoom(window):
    before = window.view._zoom
    wheel(window.view, window.view.viewport().rect().center())
    assert window.view._zoom / before == pytest.approx(1.0015 ** 120)


@pytest.mark.parametrize("turns", [0, 1])
@pytest.mark.parametrize("point", [(30, HEIGHT - 30), (WIDTH / 2, HEIGHT / 2), (WIDTH - 30, 30)])
def test_el_zoom_sigue_al_cursor_al_acercar_y_alejar(window, point, turns):
    view = window.view
    for _ in range(turns):
        window._rotate()
    pump()
    position = view.mapFromScene(QPointF(*point))
    target = view.mapToScene(position)     # el punto exacto que hay bajo el cursor
    for _ in range(15):
        wheel(view, position)
    assert view._zoom > 5
    assert distance(view.mapFromScene(target), position) <= 2
    for _ in range(15):
        wheel(view, position, -120)
    assert distance(view.mapFromScene(target), position) <= 2


def test_el_zoom_no_se_desplaza_con_pasos_pequenos_de_trackpad(window):
    view = window.view
    position = view.mapFromScene(QPointF(100, 300))
    target = view.mapToScene(position)
    for _ in range(150):
        wheel(view, position, 8)
    assert view._zoom > 5
    assert distance(view.mapFromScene(target), position) <= 2


def test_mover_el_cursor_cambia_el_punto_del_zoom(window):
    view = window.view
    first = view.mapFromScene(QPointF(100, 100))
    for _ in range(4):
        wheel(view, first)
    second = first + QPoint(150, 60)
    target = QPointF(view.mapToScene(second))
    for _ in range(4):
        wheel(view, second)
    assert distance(view.mapFromScene(target), second) <= 2


def test_el_zoom_tras_arrastrar_usa_el_punto_nuevo(window):
    view = window.view
    position = view.viewport().rect().center()
    for _ in range(4):
        wheel(view, position)
    drag(view.viewport(), Qt.MouseButton.MiddleButton, position, position + QPoint(80, 0))
    target = QPointF(view.mapToScene(position))
    for _ in range(4):
        wheel(view, position)
    assert distance(view.mapFromScene(target), position) <= 2


def test_los_botones_de_zoom_mantienen_el_centro_de_la_pantalla(window):
    view = window.view
    middle = view._screen_center()
    target = view._image_point_at(middle)
    for _ in range(6):
        view.zoom_by(app_module.ZOOM_STEP)
    assert distance(view.viewportTransform().map(target), middle) <= 2


def test_ajustar_vuelve_a_centrar_tras_mover(window):
    view = window.view
    middle = view.viewport().rect().center()
    for _ in range(6):
        wheel(view, middle + QPoint(200, 100))
    view.fit()
    pump()
    assert distance(view.mapFromScene(QPointF(WIDTH / 2, HEIGHT / 2)), middle) <= 2


def test_redimensionar_con_zoom_conserva_el_centro(window):
    view = window.view
    for _ in range(6):
        view.zoom_by(app_module.ZOOM_STEP)
    pump()
    target = QPointF(view.mapToScene(view.viewport().rect().center()))
    window.resize(window.width() - 200, window.height() - 100)
    pump()
    assert distance(view.mapFromScene(target), view.viewport().rect().center()) <= 2


def test_la_imagen_no_se_puede_perder_arrastrando(window):
    view = window.view
    center = view.viewport().rect().center()
    for _ in range(40):
        drag(view.viewport(), Qt.MouseButton.MiddleButton, center, center + QPoint(300, 200))
    image = view.mapFromScene(view.scene().items()[0].sceneBoundingRect()).boundingRect()
    assert image.intersects(view.viewport().rect().adjusted(-2, -2, 2, 2))


def test_desplazar_no_marca_puntos(window):
    view = window.view
    for _ in range(8):
        view.zoom_by(app_module.ZOOM_STEP)
    view.centerOn(WIDTH / 2, HEIGHT / 2)
    pump()
    viewport, center = view.viewport(), view.viewport().rect().center()

    def scrolled_by(button, delta, modifier=Qt.KeyboardModifier.NoModifier):
        before = view.horizontalScrollBar().value()
        drag(viewport, button, center, center + QPoint(delta, 0), modifier)
        return view.horizontalScrollBar().value() - before

    # Rueda pulsada, Ctrl (o Command en Mac) y Control en Mac, que Qt entrega como Meta
    assert scrolled_by(Qt.MouseButton.MiddleButton, -40) == 40
    assert scrolled_by(Qt.MouseButton.LeftButton, 30, Qt.KeyboardModifier.ControlModifier) == -30
    assert scrolled_by(Qt.MouseButton.LeftButton, 20, Qt.KeyboardModifier.MetaModifier) == -20
    # Espacio, con la tecla de verdad
    QTest.keyPress(view, Qt.Key.Key_Space)
    assert view.viewport().cursor().shape() == Qt.CursorShape.OpenHandCursor
    assert scrolled_by(Qt.MouseButton.LeftButton, 10) == -10
    QTest.keyRelease(view, Qt.Key.Key_Space)
    assert view.viewport().cursor().shape() == Qt.CursorShape.CrossCursor
    assert window.points == {}


def test_control_clic_en_mac_mueve_y_no_borra_el_punto(window):
    # macOS convierte Control + clic en clic derecho con el modificador Meta
    view = window.view
    click(window, 100, 200)
    center = view.viewport().rect().center()
    before = view.horizontalScrollBar().value()
    drag(view.viewport(), Qt.MouseButton.RightButton, center, center + QPoint(30, 0),
         Qt.KeyboardModifier.MetaModifier)
    assert view.horizontalScrollBar().value() == before - 30
    assert pixels(window.points) == {0: (100, 200)}


def test_tras_arrastrar_el_clic_normal_sigue_marcando(window):
    view = window.view
    center = view.viewport().rect().center()
    drag(view.viewport(), Qt.MouseButton.LeftButton, center, center + QPoint(30, 0),
         Qt.KeyboardModifier.ControlModifier)
    assert view.viewport().cursor().shape() == Qt.CursorShape.CrossCursor
    click(window, 100, 200)
    assert pixels(window.points) == {0: (100, 200)}
    click(window, 100, 200, Qt.MouseButton.RightButton)
    assert window.points == {}


# ------------------------------------------------------------------------ rotación

@pytest.mark.parametrize("turns", [0, 1, 2, 3])
def test_coordenadas_de_lo_que_se_ve_al_rotar(window, turns):
    frame = 60
    go(window, frame)
    bx, by = ball_position(frame)
    click(window, bx, by)
    for _ in range(turns):
        window._rotate()
    pump()
    exported = window._output_xy(*window.points[frame])

    # Contra el fotograma girado en sentido horario
    rotated = np.rot90(window.reader.frame(frame), -turns)
    cx, cy = centroid(rotated)
    assert abs(exported[0] - cx) < 2 and abs(exported[1] - cy) < 2

    # Y, de forma independiente, contra lo que se pinta realmente en pantalla
    view = window.view
    view.set_overlay({}, frame, [])
    pump()
    sx, sy = centroid(as_rgb(view.viewport().grab().toImage()))
    transform = view.viewportTransform()
    corners = [transform.map(QPointF(a, b)) for a in (0, WIDTH) for b in (0, HEIGHT)]
    left, top = min(c.x() for c in corners), min(c.y() for c in corners)
    assert abs((sx - left) / view._zoom - exported[0]) < 3
    assert abs((sy - top) / view._zoom - exported[1]) < 3


def test_cuatro_giros_dejan_todo_igual(window):
    click(window, 100, 200)
    for _ in range(4):
        window._rotate()
    assert window.rotation == 0 and pixels(window.points)[0] == (100, 200)
    assert (window.width_field.value(), window.height_field.value()) == (WIDTH, HEIGHT)


# ------------------------------------------------------------------------- niveles

def test_niveles_sin_ajuste_no_tocan_la_imagen(window):
    assert window.levels.lut() is None


def test_tabla_de_niveles(window):
    levels = window.levels
    levels.black, levels.white, levels.mid = 50, 200, 0.5
    lut = levels.lut()
    assert lut[0] == 0 and lut[50] == 0 and lut[200] == 255 and lut[255] == 255
    assert abs(int(lut[125]) - 128) <= 1
    levels.black, levels.white, levels.mid = 0, 255, 0.25
    assert levels.gamma == pytest.approx(2)
    assert levels.lut()[64] > 100   # medios hacia el negro: la imagen se aclara


def test_arrastrar_niveles_cambia_la_imagen_y_no_las_coordenadas(window):
    click(window, 100, 200)
    go(window, 0)
    levels = window.levels
    raw = window.reader.frame(0).copy()
    before = as_rgb(window.view._item.pixmap().toImage())

    y = levels.height() // 2
    white_handle = QPoint(levels.width() - levels.MARGIN, y)
    target = QPoint(levels.width() // 2, y)
    drag(levels, Qt.MouseButton.LeftButton, white_handle, target)

    assert 110 < levels.white < 145 and levels.black == 0
    after = as_rgb(window.view._item.pixmap().toImage())
    assert after.mean() > before.mean()
    assert np.array_equal(window.reader.frame(0), raw)   # el vídeo original no se toca
    assert pixels(window.points) == {0: (100, 200)}


def test_niveles_se_mantienen_al_cambiar_de_frame_y_se_restablecen(window):
    window.levels.white = 100
    go(window, 1)
    press(window, Qt.Key.Key_Right)
    assert window.levels.white == 100
    shown = as_rgb(window.view._item.pixmap().toImage())
    assert np.array_equal(shown, window.levels.lut()[window.reader.frame(window.frame)])
    window.levels.reset()
    pump()
    assert window.levels.lut() is None
    assert np.array_equal(as_rgb(window.view._item.pixmap().toImage()), window.reader.frame(window.frame))


def test_los_tiradores_no_se_cruzan(window):
    levels = window.levels
    y = levels.height() // 2
    drag(levels, Qt.MouseButton.LeftButton, QPoint(levels.MARGIN, y), QPoint(levels.width() - 1, y))
    assert levels.white == 255 and levels.black == 255 - levels.MIN_RANGE


# -------------------------------------------------------------------------- eventos

def mark_points(win, *frames):
    for frame in frames:
        go(win, frame)
        click(win, 10 + frame, 20 + frame)


def test_los_eventos_solo_se_marcan_sobre_un_frame_con_punto(window):
    release, impact = window.tick_boxes
    go(window, 10)
    assert not release.isEnabled() and not impact.isEnabled()
    click(window, 5, 5)
    assert release.isEnabled() and impact.isEnabled()


def test_cada_evento_una_sola_vez_y_nunca_en_el_mismo_frame(window):
    release, impact = window.tick_boxes
    mark_points(window, 10, 20, 30)
    go(window, 10)
    tick_click(release)
    assert window.ticks == [10, None]
    assert not impact.isEnabled()

    go(window, 20)
    assert not release.isEnabled() and not release.isChecked()
    assert impact.isEnabled()
    assert "10" in window.tick_jumps[0].text()
    tick_click(impact)
    assert window.ticks == [10, 20]

    go(window, 30)
    assert not release.isEnabled() and not impact.isEnabled()
    tick_click(release)
    assert window.ticks == [10, 20]


def test_la_suelta_va_antes_que_el_impacto(window):
    release, impact = window.tick_boxes
    mark_points(window, 10, 20)
    tick_click(release)                       # suelta en el 20
    go(window, 10)
    assert not impact.isEnabled()             # el impacto no puede ir antes
    go(window, 20)
    tick_click(release)                       # se desmarca
    tick_click(impact)                        # impacto en el 20
    go(window, 10)
    assert release.isEnabled()
    tick_click(release)
    assert window.ticks == [10, 20]


def test_quitar_el_punto_quita_su_evento(window):
    release, impact = window.tick_boxes
    mark_points(window, 10, 20)
    tick_click(impact)
    assert window.ticks == [None, 20]
    click(window, 5, 5, Qt.MouseButton.RightButton)
    assert window.ticks == [None, None]
    assert not impact.isChecked()


def test_mover_el_punto_conserva_su_evento(window):
    mark_points(window, 10)
    tick_click(window.tick_boxes[0])
    click(window, 50, 50)
    assert window.ticks == [10, None] and pixels(window.points)[10] == (50, 50)


def test_evento_se_corrige_desmarcando_en_su_frame(window):
    release, impact = window.tick_boxes
    mark_points(window, 10, 20, 30)
    go(window, 10)
    tick_click(release)
    go(window, 20)
    tick_click(impact)
    go(window, 30)
    QTest.mouseClick(window.tick_jumps[1], Qt.MouseButton.LeftButton)
    pump()
    assert window.frame == 20 and impact.isChecked()
    tick_click(impact)
    assert window.ticks == [10, None]


# ------------------------------------------------------------------------ exportar

def test_explica_que_se_guarda_en_la_carpeta_del_video(window):
    assert window.export_note.text() == "Se guarda en la carpeta del vídeo:"
    folder = str(window.path.resolve().parent)
    assert window.folder_label.toolTip() == folder
    label = window.folder_label
    assert label.fontMetrics().horizontalAdvance(label.text()) <= window.export_note.parent().width()


def exported(window, name="disparo_01"):
    window._export()
    return window.path.with_name(name + ".xlsx")


@pytest.mark.parametrize("points", [0, 1])
def test_con_menos_de_dos_puntos_avisa_y_no_crea_archivo(window, dialogs, points):
    mark_points(window, *range(points))
    output = exported(window)
    assert "al menos 2" in dialogs.shown[-1]
    assert not output.exists()


def test_el_excel_lleva_la_posicion_exacta_con_dos_decimales(window):
    view = window.view
    position = view.mapFromScene(QPointF(100.3, 200.6))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, position)
    pump()
    point = view.mapToScene(position)
    mark_points(window, 1)
    _, x, y, _ = sheet_rows(exported(window))[1]
    assert (x, y) == pytest.approx((point.x(), point.y()), abs=0.011)
    assert (x, y) == (round(x, 2), round(y, 2))
    assert x != int(x) or y != int(y)


def test_con_dos_puntos_ya_se_puede_exportar(window):
    mark_points(window, 0, 1)
    assert exported(window).exists()


def test_el_aviso_de_exportar_no_mueve_nada_de_la_ventana(window):
    mark_points(window, 0, 1)
    pump()
    assert window.findChild(QStatusBar) is None
    widgets = (window.view, window.slider, window.notice_label, window.hover_label, window.name_field)
    before = [w.geometry() for w in widgets]
    window._export()
    pump()
    assert window.notice_label.text() == "Exportado: disparo_01.xlsx"
    assert [w.geometry() for w in widgets] == before
    assert window.findChild(QStatusBar) is None
    window._notice_timer.timeout.emit()      # al desaparecer tampoco
    pump()
    assert window.notice_label.text() == ""
    assert [w.geometry() for w in widgets] == before


def test_la_ventana_cabe_en_pantallas_pequenas(window):
    # Un portátil de 1366 x 768 con barra de tareas deja unos 690 píxeles de alto
    assert window.minimumSizeHint().height() < 600
    window.resize(1000, 600)
    pump()
    assert window.height() <= 620
    assert window.view.height() > 300
    # El panel sigue siendo accesible, ahora con desplazamiento, y sin cortar el texto a lo ancho
    panel = window.notice_label.parent()
    area = panel.parent().parent()
    assert area.verticalScrollBar().maximum() > 0
    assert panel.width() == area.viewport().width()
    assert panel.minimumSizeHint().width() <= panel.width()


def test_un_aviso_largo_no_ensancha_el_panel(window):
    panel_width = window.notice_label.parent().width()
    window._notify("Exportado: " + "nombre_muy_largo_" * 20 + ".xlsx")
    pump()
    assert window.notice_label.parent().width() == panel_width
    assert "…" in window.notice_label.text()
    assert "nombre_muy_largo_" * 20 in window.notice_label.toolTip()


def test_exporta_frame_posicion_y_evento_junto_al_video_con_su_nombre(window):
    for frame, (x, y) in {2: (50, 60), 0: (10, 20), 1: (30, 40)}.items():
        go(window, frame)
        click(window, x, y)
    go(window, 0)
    window.tick_boxes[0].click()
    go(window, 2)
    window.tick_boxes[1].click()
    pump()
    assert window.dirty
    output = exported(window)
    assert not window.dirty
    rows = sheet_rows(output)
    assert rows[0] == ("frame", "pixel x", "pixel y", "anotación")
    assert [floored(row) for row in rows[1:]] == [(0, 10, 20, "suelta"), (1, 30, 40, None),
                                                  (2, 50, 60, "impacto")]
    info = dict(sheet_rows(output, "Info"))
    assert info["FPS"] == pytest.approx(30)
    assert (info["Ancho"], info["Alto"]) == (WIDTH, HEIGHT)
    assert (info["Frame de suelta"], info["Frame de impacto"]) == (0, 2)


@pytest.mark.parametrize("marked, events, info_release, info_impact", [
    ((), (None, None, None), "no marcado", "no marcado"),            # no se ve ninguno
    ((0,), ("suelta", None, None), 5, "no marcado"),                 # no se ve el impacto
    ((1,), (None, None, "impacto"), "no marcado", 7),                # no se ve la suelta
])
def test_un_video_puede_no_mostrar_la_suelta_o_el_impacto(window, marked, events, info_release, info_impact):
    mark_points(window, 5, 6, 7)
    for which in marked:
        go(window, 5 if which == 0 else 7)
        tick_click(window.tick_boxes[which])
    output = exported(window)
    assert [row[3] for row in sheet_rows(output)[1:]] == list(events)
    info = dict(sheet_rows(output, "Info"))
    assert (info["Frame de suelta"], info["Frame de impacto"]) == (info_release, info_impact)


def test_puede_haber_puntos_antes_de_la_suelta_y_despues_del_impacto(window, dialogs):
    mark_points(window, 5, 6, 7, 8)
    go(window, 6)
    tick_click(window.tick_boxes[0])
    go(window, 7)
    tick_click(window.tick_boxes[1])
    output = exported(window)                # sin preguntar nada: la lista de respuestas está vacía
    assert dialogs.shown == []
    assert [row[3] for row in sheet_rows(output)[1:]] == [None, "suelta", "impacto", None]


def test_nombre_elegido_por_el_usuario(window):
    mark_points(window, 0, 1)
    window.name_field.setText("mi prueba.final.XLSX")
    window._export()
    assert window.path.with_name("mi prueba.final.xlsx").exists()


def test_nombre_vacio_usa_el_del_video(window):
    mark_points(window, 0, 1)
    window.name_field.setText("   ")
    assert exported(window).exists()


def test_rutas_con_acentos_y_espacios(qapp, video, tmp_path, dialogs):
    folder = tmp_path / "Mis vídeos de cañón ñ"
    folder.mkdir()
    copy = folder / "disparo número 1.mp4"
    copy.write_bytes(video.read_bytes())
    win = MainWindow(VideoReader(copy), copy)
    win.show()
    pump()
    try:
        assert frame_number(win.reader.frame(50)) == 50
        assert win.name_field.text() == "disparo número 1"
        assert win.folder_label.toolTip() == str(folder.resolve())
        mark_points(win, 0, 1)
        output = exported(win, "disparo número 1")
        assert output.parent == folder and output.exists()
        assert sheet_rows(output)[0] == ("frame", "pixel x", "pixel y", "anotación")
    finally:
        win.dirty = False
        win.close()


def test_no_permite_guardar_fuera_de_la_carpeta(window, dialogs):
    mark_points(window, 0, 1)
    window.name_field.setText("../fuera")
    window._export()
    assert "no puede contener" in dialogs.shown[-1]
    assert not window.path.parent.parent.joinpath("fuera.xlsx").exists()


def test_pregunta_antes_de_reemplazar(window, dialogs):
    mark_points(window, 0, 1)
    output = exported(window)
    mark_points(window, 2)
    dialogs.answers[:] = [QMessageBox.StandardButton.No]
    window._export()
    assert len(sheet_rows(output)) == 3
    dialogs.answers[:] = [QMessageBox.StandardButton.Yes]
    window._export()
    assert len(sheet_rows(output)) == 4


def test_fallo_al_guardar_avisa_y_conserva_los_cambios(window, dialogs, monkeypatch):
    mark_points(window, 0, 1)

    def blocked(*args):
        raise PermissionError("bloqueado")

    monkeypatch.setattr(app_module, "write_excel", blocked)
    window._export()
    assert "bloqueado" in dialogs.shown[-1]
    assert window.dirty


def test_exporta_coordenadas_rotadas(window):
    click(window, 100, 200)
    go(window, 1)
    click(window, 110, 210)
    window._rotate()
    output = exported(window)
    assert floored(sheet_rows(output)[1])[:3] == (0, HEIGHT - 1 - 200, 100)
    info = dict(sheet_rows(output, "Info"))
    assert (info["Ancho"], info["Alto"], info["Rotación (grados)"]) == (HEIGHT, WIDTH, 90)


def test_pregunta_al_cerrar_con_cambios_sin_exportar(window, dialogs):
    click(window, 1, 1)
    dialogs.answers[:] = [QMessageBox.StandardButton.No]
    window.close()
    assert window.isVisible()
    dialogs.answers[:] = [QMessageBox.StandardButton.Yes]
    window.close()
    assert not window.isVisible()
