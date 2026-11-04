"""Guarda capturas de la ventana con el sistema gráfico real, para revisarlas desde el CI.

    python tests/capture_window.py carpeta/nombre

Crea nombre.png (tamaño normal) y nombre_pequena.png (pantalla pequeña), e imprime la
fuente y el ancho que pide cada fila del panel.
"""

import os
import sys
import threading
from pathlib import Path

import numpy as np

import ballistiq
from ballistiq import QApplication, QPointF

WIDTH, HEIGHT, FRAMES = 1280, 720, 60


def ball(frame):
    """Tiro parabólico: la recta de ajuste se distingue de la línea roja."""
    t = frame / (FRAMES - 1)
    return 80 + 1100 * t, 640 - 1500 * t + 1400 * t * t


class DemoVideo:
    width, height, count, fps, variable_fps = WIDTH, HEIGHT, FRAMES, 30.0, False

    def frame(self, k):
        image = np.full((HEIGHT, WIDTH, 3), 42, np.uint8)
        ys, xs = np.ogrid[:HEIGHT, :WIDTH]
        cx, cy = ball(k)
        image[(xs - cx) ** 2 + (ys - cy) ** 2 <= 81] = (240, 200, 40)
        return image


def pump(app):
    for _ in range(10):
        app.processEvents()


def describe_panel(app, window):
    font = window.point_label.font()
    screen = app.primaryScreen()
    print(f"plataforma={app.platformName()} Qt={ballistiq.QT_VERSION >> 16} "
          f"fuente={font.family()!r} puntos={font.pointSizeF()} pixeles={font.pixelSize()} "
          f"ppp={screen.logicalDotsPerInch()} escala={screen.devicePixelRatio()}")
    panel = window.notice_label.parent()
    print(f"ventana={window.width()}x{window.height()} panel={panel.width()} "
          f"visible={panel.parent().width()} el_panel_pide={panel.minimumSizeHint().width()}")
    layout = panel.layout()
    rows = []
    for i in range(layout.count()):
        item = layout.itemAt(i)
        widgets = ([item.widget()] if item.widget() else
                   [item.layout().itemAt(j).widget() for j in range(item.layout().count())]
                   if item.layout() else [])
        names = [f"{(w.text() if hasattr(w, 'text') else type(w).__name__)[:18]!r}="
                 f"{w.minimumSizeHint().width()}" for w in widgets if w]
        rows.append((item.minimumSize().width(), " ".join(names)))
    for width, names in sorted(rows, key=lambda row: -row[0])[:6]:
        print(f"  {width:4d}  {names}")


def main(output):
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")   # una consola de Windows no admite todos los signos
    watchdog = threading.Timer(60, lambda: os._exit(3))
    watchdog.daemon = True
    watchdog.start()

    app = QApplication(["ballistiq"])
    ballistiq.apply_style(app)
    window = ballistiq.MainWindow(DemoVideo(), "disparo_01.mp4")
    window.resize(1200, 760)
    window.show()
    pump(app)

    for frame in range(4, 34, 2):
        window.goto(frame)
        pump(app)
        window._on_left_click(*ball(frame))
    window.goto(4)
    pump(app)
    window.tick_boxes[ballistiq.RELEASE].click()
    window.goto(32)
    pump(app)
    window.tick_boxes[ballistiq.IMPACT].click()
    window.notice_label.setText("¡Exportado!")
    window.goto(34)
    pump(app)
    window._on_hover(QPointF(718.06, 238.43))

    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    describe_panel(app, window)
    window.grab().save(f"{path}.png")

    window.resize(1000, 600)
    pump(app)
    describe_panel(app, window)
    window.grab().save(f"{path}_pequena.png")

    window.dirty = False
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main(sys.argv[1])
