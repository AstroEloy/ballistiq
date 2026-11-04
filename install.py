#!/usr/bin/env python3
"""Instalador de Ballistiq para Windows, Linux y macOS.

    python3 install.py              instala o actualiza
    python3 install.py --uninstall  desinstala

En Windows, `py install.py`.

Crea un entorno propio en ~/.ballistiq con las versiones probadas de cada librería
(las de pyproject.toml), así no choca con nada de lo que tengas instalado, y deja
disponible el comando `ballistiq`. Solo hace falta tener Python.
"""

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
HOME = Path.home() / ".ballistiq"
VENV = HOME / "venv"
WINDOWS = os.name == "nt"
LINUX = sys.platform.startswith("linux")
BIN = VENV / ("Scripts" if WINDOWS else "bin")
PYTHON = BIN / ("python.exe" if WINDOWS else "python")
COMMAND = BIN / ("ballistiq.exe" if WINDOWS else "ballistiq")
# Con la variable, y no con la ruta escrita, para que funcione con usuarios como "José":
# cmd.exe no lee bien los acentos de un .cmd.
WINDOWS_SHIM = '@"%USERPROFILE%\\.ballistiq\\venv\\Scripts\\ballistiq.exe" %*\n'
# Librerías del sistema que Qt necesita en Linux y que pip no puede instalar
LINUX_LIBRARIES = ["libdbus-1-3", "libegl1", "libfontconfig1", "libfreetype6", "libgl1",
                   "libglib2.0-0", "libice6", "libsm6", "libwayland-client0",
                   "libwayland-cursor0", "libwayland-egl1", "libx11-6", "libx11-xcb1", "libxcb1",
                   "libxcb-cursor0", "libxcb-glx0", "libxcb-icccm4", "libxcb-image0",
                   "libxcb-keysyms1", "libxcb-randr0", "libxcb-render0", "libxcb-render-util0",
                   "libxcb-shape0", "libxcb-shm0", "libxcb-sync1", "libxcb-util1",
                   "libxcb-xfixes0", "libxcb-xkb1", "libxkbcommon0", "libxkbcommon-x11-0",
                   "libzstd1", "zlib1g"]


def fail(message):
    print(f"\nERROR: {message}", file=sys.stderr)
    sys.exit(1)


def ask_yes(question):
    """Pregunta sí/no; sin terminal o con Ctrl+D la respuesta es no."""
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(f"{question} [S/n] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("", "s", "si", "sí", "y", "yes")


def apt_command(packages):
    return ["sudo", "apt-get", "install", "-y"] + packages


def offer_apt(reason, packages):
    """Ofrece instalar paquetes del sistema con apt-get. Devuelve si se ha intentado."""
    command = apt_command(packages)
    if not shutil.which("apt-get"):
        return False
    if not ask_yes(f"\n{reason} ¿Lo instalo con este comando?\n  {' '.join(command)}\n"):
        return False
    subprocess.run(["sudo", "apt-get", "update", "-qq"], check=False)
    subprocess.run(command, check=False)
    return True


def python_range():
    """Versiones de Python admitidas, leídas de pyproject.toml: (menor, mayor)."""
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    low, high = re.search(r'requires-python = ">=3\.(\d+),<3\.(\d+)"', text).groups()
    return int(low), int(high) - 1


def find_compatible_python(low, high):
    """Busca entre los Python ya instalados uno admitido, del más nuevo al más viejo."""
    for minor in range(high, low - 1, -1):
        command = ["py", f"-3.{minor}"] if WINDOWS else [f"python3.{minor}"]
        if shutil.which(command[0]):
            check = subprocess.run(command + ["-c", "import venv"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if check.returncode == 0:
                return command
    return None


def ensure_compatible_python():
    """Si este Python no sirve, continúa con otro compatible o explica qué hacer."""
    low, high = python_range()
    if sys.version_info[0] == 3 and low <= sys.version_info[1] <= high:
        return
    current = "{}.{}".format(*sys.version_info[:2])
    other = find_compatible_python(low, high)
    if other is None:
        fail(f"Ballistiq necesita Python 3.{low} a 3.{high} y este es {current}.\n"
             "Instala uno desde https://www.python.org/downloads/ y vuelve a ejecutar esto.")
    print(f"Python {current} no es compatible; se usará {' '.join(other)}.")
    sys.exit(subprocess.call(other + [str(Path(__file__).resolve())] + sys.argv[1:]))


def create_environment():
    print(f"Creando el entorno en {VENV} ...")
    command = [sys.executable, "-m", "venv", "--clear", str(VENV)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0 and LINUX:   # Debian y Ubuntu traen Python sin este módulo
        package = "python3.{}-venv".format(sys.version_info[1])
        if offer_apt("A este Python le falta el módulo venv.", [package]):
            result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            fail(f"No se pudo crear el entorno de Python. Prueba con:\n"
                 f"  {' '.join(apt_command([package]))}\n\n{result.stderr.strip()}")
    if result.returncode != 0:
        fail(f"No se pudo crear el entorno de Python.\n\n{result.stderr.strip()}")


def install_packages():
    print("Instalando Ballistiq y sus librerías (puede tardar unos minutos) ...")
    subprocess.run([str(PYTHON), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
                   check=False)
    # Se instala desde una copia para no dejar restos de compilación en el repositorio.
    # --only-binary: nunca se intenta compilar nada en tu equipo.
    with tempfile.TemporaryDirectory() as source:
        for name in ("ballistiq.py", "pyproject.toml", "README.md"):
            shutil.copy(str(REPO / name), source)
        result = subprocess.run([str(PYTHON), "-m", "pip", "install", "--quiet",
                                 "--only-binary=:all:", source])
    if result.returncode != 0:
        python = "{}.{}".format(*sys.version_info[:2])
        fail(f"No se pudieron instalar las librerías en este sistema ({platform.machine()}, "
             f"Python {python}).\nComprueba la conexión a internet y prueba con un Python más "
             "reciente.\nSi aun así no hay paquetes, consulta «Otras plataformas» en el README: "
             "también funciona con los paquetes de tu propio sistema.")


def qt_problem():
    """Arranca Qt sin pantalla y, si hay una en Linux, con ella. Devuelve el error o None."""
    environments = [dict(os.environ, QT_QPA_PLATFORM="offscreen")]
    if LINUX and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        environments.append({k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"})
    for environment in environments:
        # Desde otra carpeta, para comprobar la copia instalada y no la del repositorio.
        result = subprocess.run([str(PYTHON), "-c", "import ballistiq; ballistiq.QApplication([])"],
                                env=environment, cwd=str(HOME), capture_output=True, text=True)
        if result.returncode != 0:
            return result.stderr.strip() or f"código de salida {result.returncode}"
    return None


def check_installation():
    print("Comprobando la instalación ...")
    problem = qt_problem()
    if problem and LINUX:
        if offer_apt("Faltan librerías gráficas del sistema.", LINUX_LIBRARIES):
            problem = qt_problem()
        if problem:
            fail("Faltan librerías gráficas del sistema. En Debian o Ubuntu se instalan con:\n"
                 f"  {' '.join(apt_command(LINUX_LIBRARIES))}\n"
                 f"En otras distribuciones, instala las de Qt 6.\n\n{problem}")
    if problem:
        fail("La instalación no funciona:\n" + problem)


def offer_path(folder):
    """Devuelve si las terminales nuevas encontrarán el comando."""
    line = 'export PATH="$HOME/.local/bin:$PATH"'
    shell = os.path.basename(os.environ.get("SHELL", ""))
    bash_profile = ".bash_profile" if sys.platform == "darwin" else ".bashrc"
    profile = {"zsh": ".zshrc", "bash": bash_profile}.get(shell)
    if profile:
        path = Path.home() / profile
        if path.exists() and line in path.read_text(encoding="utf-8", errors="replace"):
            return True   # ya se añadió antes: esta terminal es simplemente anterior
        if ask_yes(f"\nHay que añadir {folder} al PATH en ~/{profile}. ¿Lo añado?"):
            with open(str(path), "a", encoding="utf-8") as file:
                file.write(f"\n# Comando ballistiq\n{line}\n")
            return True
    print(f"\nPara usar el comando, añade esta línea a tu perfil de terminal:\n    {line}")
    return False


def command_link():
    """Dónde queda el comando visible para el usuario."""
    if WINDOWS:   # esa carpeta ya está en el PATH de Windows
        return Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "WindowsApps" / "ballistiq.cmd"
    return Path.home() / ".local" / "bin" / "ballistiq"


def create_command():
    """Crea el comando y devuelve qué hay que hacer para empezar a usarlo."""
    full_path = f"Para empezar, usa la ruta completa:\n    {COMMAND}"
    link = command_link()
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            link.unlink()
        if WINDOWS:
            link.write_text(WINDOWS_SHIM, encoding="ascii")
        else:
            link.symlink_to(COMMAND)
    except OSError as error:
        print(f"\nNo se pudo crear el comando ({error}).")
        return full_path
    folders = [os.path.normcase(p) for p in os.environ.get("PATH", "").split(os.pathsep)]
    if os.path.normcase(str(link.parent)) in folders:
        return "Para empezar escribe:  ballistiq"
    if not WINDOWS and offer_path(link.parent):
        # Una terminal ya abierta no ve el cambio del PATH: ningún programa se lo puede aplicar.
        return f"Abre una terminal nueva y escribe:  ballistiq\nO, en esta misma terminal:\n    {COMMAND}"
    return full_path


def install():
    ensure_compatible_python()
    create_environment()
    install_packages()
    check_installation()
    print(f"\nListo. {create_command()}")


def uninstall():
    link = command_link()
    if link.is_symlink() or link.exists():
        link.unlink()
    shutil.rmtree(str(HOME), ignore_errors=True)
    print("Ballistiq se ha desinstalado.")


def main():
    # Sin esto, una consola de Windows que no admita algún acento haría fallar el instalador.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="Instala Ballistiq en un entorno propio.")
    parser.add_argument("--uninstall", action="store_true", help="quita lo instalado")
    if parser.parse_args().uninstall:
        uninstall()
    else:
        install()


if __name__ == "__main__":
    main()
