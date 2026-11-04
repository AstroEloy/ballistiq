"""Pruebas sobre la ventana real, con clics y teclas simulados y vídeos generados al vuelo."""

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
                       predict)

# Las clases que solo usan las pruebas, de la misma librería de Qt que use el programa
_binding = Qt.__module__.split(".")[0]
QEvent = import_module(_binding + ".QtCore").QEvent
QPoint = import_module(_binding + ".QtCore").QPoint
QMouseEvent = import_module(_binding + ".QtGui").QMouseEvent
QWheelEvent = import_module(_binding + ".QtGui").QWheelEvent
QTest = import_module(_binding + ".QtTest").QTest

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
    application.setStyle("Fusion")
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


def press(win, key):
    QTest.keyClick(win.view, key)
    pump()


def go(win, k):
    win.goto(k)
    pump()


def drag(widget, button, start, end):
    """Pulsa, mueve con el botón pulsado y suelta, con los eventos que manda el sistema."""
    QTest.mousePress(widget, button, Qt.KeyboardModifier.NoModifier, start)
    move = QMouseEvent(QEvent.Type.MouseMove, QPointF(end), QPointF(widget.mapToGlobal(end)),
                       Qt.MouseButton.NoButton, button, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, move)
    QTest.mouseRelease(widget, button, Qt.KeyboardModifier.NoModifier, end)
    pump()


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
    assert window._output_xy(*window.points[0]) == (641, 100)
    assert window.points[0] == (320, 100)


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
    assert window.points == {0: (100, 200)}
    click(window, 110, 210)
    assert window.points == {0: (110, 210)}
    click(window, 5, 5, Qt.MouseButton.RightButton)
    assert window.points == {}
    click(window, 5, 5, Qt.MouseButton.RightButton)
    assert window.points == {}


def test_los_puntos_de_otros_frames_no_se_editan(window):
    click(window, 100, 200)
    press(window, Qt.Key.Key_Right)
    click(window, 300, 300, Qt.MouseButton.RightButton)
    assert window.points == {0: (100, 200)}
    click(window, 120, 220)
    assert window.points == {0: (100, 200), 1: (120, 220)}


def test_al_volver_a_su_frame_se_puede_editar(window):
    click(window, 100, 200)
    press(window, Qt.Key.Key_Right)
    click(window, 120, 220)
    press(window, Qt.Key.Key_Left)
    click(window, 90, 190)
    assert window.points == {0: (90, 190), 1: (120, 220)}
    click(window, 90, 190, Qt.MouseButton.RightButton)
    assert window.points == {1: (120, 220)}


def test_dos_clics_rapidos_cuentan_como_dos_clics(window):
    click(window, 100, 200)
    position = QPointF(window.view.mapFromScene(QPointF(150.5, 250.5)))
    viewport = window.view.viewport()
    second_click = QMouseEvent(QEvent.Type.MouseButtonDblClick, position, QPointF(viewport.mapToGlobal(position.toPoint())),
                               Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(viewport, second_click)
    pump()
    assert window.points == {0: (150, 250)}


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
    viewport = window.view.viewport()
    center = viewport.rect().center()
    before = window.view._zoom
    event = QWheelEvent(QPointF(center), QPointF(viewport.mapToGlobal(center)), QPoint(0, 0),
                        QPoint(0, 120), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(viewport, event)
    pump()
    assert window.view._zoom / before == pytest.approx(1.0015 ** 120)


def test_desplazar_no_marca_puntos(window):
    view = window.view
    for _ in range(8):
        view.zoom_by(app_module.ZOOM_STEP)
    view.centerOn(WIDTH / 2, HEIGHT / 2)
    pump()
    viewport, center = view.viewport(), view.viewport().rect().center()

    def scrolled_by(button, delta):
        before = view.horizontalScrollBar().value()
        drag(viewport, button, center, center + QPoint(delta, 0))
        return view.horizontalScrollBar().value() - before

    assert scrolled_by(Qt.MouseButton.MiddleButton, -40) == 40
    view._space_held = True
    assert scrolled_by(Qt.MouseButton.LeftButton, 30) == -30
    view._space_held = False
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
    assert window.rotation == 0 and window.points[0] == (100, 200)
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
    assert window.points == {0: (100, 200)}


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


# --------------------------------------------------------------------------- marcas

def mark_points(win, *frames):
    for frame in frames:
        go(win, frame)
        click(win, 10 + frame, 20 + frame)


def test_las_marcas_solo_se_activan_sobre_un_frame_con_punto(window):
    first, last = window.tick_boxes
    go(window, 10)
    assert not first.isEnabled() and not last.isEnabled()
    click(window, 5, 5)
    assert first.isEnabled() and last.isEnabled()


def test_cada_marca_una_sola_vez_y_nunca_en_el_mismo_frame(window):
    first, last = window.tick_boxes
    mark_points(window, 10, 20, 30)
    go(window, 10)
    tick_click(first)
    assert window.ticks == [10, None]
    assert not last.isEnabled()

    go(window, 20)
    assert not first.isEnabled() and not first.isChecked()
    assert last.isEnabled()
    assert "10" in window.tick_jumps[0].text()
    tick_click(last)
    assert window.ticks == [10, 20]

    go(window, 30)
    assert not first.isEnabled() and not last.isEnabled()
    tick_click(first)
    assert window.ticks == [10, 20]


def test_el_primero_va_antes_que_el_ultimo(window):
    first, last = window.tick_boxes
    mark_points(window, 10, 20)
    tick_click(first)                       # primero en el 20
    go(window, 10)
    assert not last.isEnabled()             # el último no puede ir antes
    go(window, 20)
    tick_click(first)                       # se desmarca
    tick_click(last)                        # último en el 20
    go(window, 10)
    assert first.isEnabled()
    tick_click(first)
    assert window.ticks == [10, 20]


def test_quitar_el_punto_quita_su_marca(window):
    first, last = window.tick_boxes
    mark_points(window, 10, 20)
    tick_click(last)
    assert window.ticks == [None, 20]
    click(window, 5, 5, Qt.MouseButton.RightButton)
    assert window.ticks == [None, None]
    assert not last.isChecked()


def test_mover_el_punto_conserva_su_marca(window):
    mark_points(window, 10)
    tick_click(window.tick_boxes[0])
    click(window, 50, 50)
    assert window.ticks == [10, None] and window.points[10] == (50, 50)


def test_marca_se_corrige_desmarcando_en_su_frame(window):
    first, last = window.tick_boxes
    mark_points(window, 10, 20, 30)
    go(window, 10)
    tick_click(first)
    go(window, 20)
    tick_click(last)
    go(window, 30)
    QTest.mouseClick(window.tick_jumps[1], Qt.MouseButton.LeftButton)
    pump()
    assert window.frame == 20 and last.isChecked()
    tick_click(last)
    assert window.ticks == [10, None]


# ------------------------------------------------------------------------ exportar

def test_explica_que_se_guarda_en_la_carpeta_del_video(window):
    assert window.export_note.text() == "Se guarda en la misma carpeta que el vídeo:"
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


def test_con_dos_puntos_ya_se_puede_exportar(window):
    mark_points(window, 0, 1)
    assert exported(window).exists()


def test_exporta_cuatro_columnas_junto_al_video_con_su_nombre(window):
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
    assert sheet_rows(output) == [("frame", "x", "y", "marca"), (0, 10, 20, "primero"),
                                  (1, 30, 40, None), (2, 50, 60, "último")]
    info = dict(sheet_rows(output, "Info"))
    assert info["FPS"] == pytest.approx(30)
    assert (info["Ancho"], info["Alto"]) == (WIDTH, HEIGHT)
    assert (info["Primer frame"], info["Último frame"]) == (0, 2)


@pytest.mark.parametrize("marked, marks, info_first, info_last", [
    ((), (None, None, None), "no marcado", "no marcado"),            # no ve ninguno
    ((0,), ("primero", None, None), 5, "no marcado"),                # no ve el último
    ((1,), (None, None, "último"), "no marcado", 7),                 # no ve el primero
])
def test_una_camara_puede_no_ver_el_primero_o_el_ultimo(window, marked, marks, info_first, info_last):
    mark_points(window, 5, 6, 7)
    for which in marked:
        go(window, 5 if which == 0 else 7)
        tick_click(window.tick_boxes[which])
    output = exported(window)
    assert [row[3] for row in sheet_rows(output)[1:]] == list(marks)
    info = dict(sheet_rows(output, "Info"))
    assert (info["Primer frame"], info["Último frame"]) == (info_first, info_last)


def test_avisa_si_hay_puntos_fuera_de_las_marcas(window, dialogs):
    mark_points(window, 5, 6, 7)
    go(window, 6)
    tick_click(window.tick_boxes[0])        # primero en el 6, pero hay un punto en el 5
    dialogs.answers[:] = [QMessageBox.StandardButton.No]
    output = exported(window)
    assert "antes del marcado" in dialogs.shown[-1]
    assert not output.exists()
    dialogs.answers[:] = [QMessageBox.StandardButton.Yes]
    assert exported(window).exists()


def test_nombre_elegido_por_el_usuario(window):
    mark_points(window, 0, 1)
    window.name_field.setText("mi prueba.final.XLSX")
    window._export()
    assert window.path.with_name("mi prueba.final.xlsx").exists()


def test_nombre_vacio_usa_el_del_video(window):
    mark_points(window, 0, 1)
    window.name_field.setText("   ")
    assert exported(window).exists()


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
    assert sheet_rows(output)[1][:3] == (0, HEIGHT - 1 - 200, 100)
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
