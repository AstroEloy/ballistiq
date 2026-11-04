"""Pruebas del instalador, incluidas las ramas de Windows y Linux aunque se ejecuten en otro sistema."""

import os
from pathlib import Path

import pytest

import install


class Result:
    def __init__(self, returncode=0, stderr=""):
        self.returncode, self.stderr = returncode, stderr


@pytest.fixture
def commands(monkeypatch):
    """Registra los comandos del sistema en lugar de ejecutarlos."""
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return Result()

    monkeypatch.setattr(install.subprocess, "run", run)
    monkeypatch.setattr(install.shutil, "which", lambda name: "/usr/bin/" + name)
    return calls


unix_only = pytest.mark.skipif(os.name == "nt", reason="crear enlaces en Windows requiere permisos")


def answer(monkeypatch, value):
    monkeypatch.setattr(install, "ask_yes", lambda question: value)


def test_rango_de_python_leido_de_pyproject():
    assert install.python_range() == (8, 14)


@pytest.mark.parametrize("typed, expected", [("", True), ("s", True), ("Sí", True), ("y", True),
                                             ("n", False), ("no", False)])
def test_preguntar(monkeypatch, typed, expected):
    monkeypatch.setattr(install.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: typed)
    assert install.ask_yes("¿?") is expected


def test_preguntar_sin_terminal_es_no(monkeypatch):
    monkeypatch.setattr(install.sys.stdin, "isatty", lambda: False, raising=False)
    assert install.ask_yes("¿?") is False


def test_ctrl_d_es_no(monkeypatch):
    def end_of_file(prompt):
        raise EOFError

    monkeypatch.setattr(install.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", end_of_file)
    assert install.ask_yes("¿?") is False


# ------------------------------------------------------------------------- Windows

def test_comando_de_windows_funciona_con_usuarios_con_acentos(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "Microsoft" / "WindowsApps"))
    assert install.create_command() == "Para empezar escribe:  ballistiq"
    shim = tmp_path / "Microsoft" / "WindowsApps" / "ballistiq.cmd"
    assert shim.read_bytes().decode("ascii") == (
        '@"%USERPROFILE%\\.ballistiq\\venv\\Scripts\\ballistiq.exe" %*\n')


def test_windows_busca_otro_python_con_el_lanzador(monkeypatch, commands):
    monkeypatch.setattr(install, "WINDOWS", True)
    assert install.find_compatible_python(8, 14) == ["py", "-3.14"]


def test_desinstalar_quita_el_comando_de_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "WINDOWS", True)
    monkeypatch.setattr(install, "HOME", tmp_path / "entorno")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    (tmp_path / "entorno").mkdir()
    shim = tmp_path / "Microsoft" / "WindowsApps" / "ballistiq.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_text("x")
    install.uninstall()
    assert not shim.exists() and not (tmp_path / "entorno").exists()


# --------------------------------------------------------------------------- Linux

def test_linux_ofrece_instalar_las_librerias_graficas_y_comprueba_de_nuevo(monkeypatch, commands):
    monkeypatch.setattr(install, "LINUX", True)
    answer(monkeypatch, True)
    problems = ["falta libEGL.so.1", None]
    monkeypatch.setattr(install, "qt_problem", lambda: problems.pop(0))
    install.check_installation()
    assert problems == []
    assert commands[0] == ["sudo", "apt-get", "update", "-qq"]
    assert commands[1][:4] == ["sudo", "apt-get", "install", "-y"]
    assert "libegl1" in commands[1] and "libxcb-cursor0" in commands[1]


def test_linux_si_rechazas_las_librerias_explica_el_comando(monkeypatch, commands, capsys):
    monkeypatch.setattr(install, "LINUX", True)
    answer(monkeypatch, False)
    monkeypatch.setattr(install, "qt_problem", lambda: "falta libEGL.so.1")
    with pytest.raises(SystemExit):
        install.check_installation()
    error = capsys.readouterr().err
    assert "sudo apt-get install -y " in error and "libegl1" in error
    assert "falta libEGL.so.1" in error
    assert commands == []


def test_linux_sin_apt_no_ejecuta_nada_y_explica(monkeypatch, commands, capsys):
    monkeypatch.setattr(install, "LINUX", True)
    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    monkeypatch.setattr(install, "qt_problem", lambda: "falta libEGL.so.1")
    with pytest.raises(SystemExit):
        install.check_installation()
    assert "otras distribuciones" in capsys.readouterr().err
    assert commands == []


def test_si_las_librerias_no_lo_arreglan_falla_con_el_error_real(monkeypatch, commands, capsys):
    monkeypatch.setattr(install, "LINUX", True)
    answer(monkeypatch, True)
    monkeypatch.setattr(install, "qt_problem", lambda: "error persistente")
    with pytest.raises(SystemExit):
        install.check_installation()
    assert "error persistente" in capsys.readouterr().err


def test_linux_sin_modulo_venv_lo_instala_y_reintenta(monkeypatch):
    monkeypatch.setattr(install, "LINUX", True)
    answer(monkeypatch, True)
    monkeypatch.setattr(install.shutil, "which", lambda name: "/usr/bin/" + name)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        is_venv = "venv" in command and "-m" in command
        return Result(1 if is_venv and len([c for c in calls if "-m" in c]) == 1 else 0)

    monkeypatch.setattr(install.subprocess, "run", run)
    install.create_environment()
    package = "python3.{}-venv".format(install.sys.version_info[1])
    assert ["sudo", "apt-get", "install", "-y", package] in calls
    assert len([c for c in calls if "-m" in c]) == 2


def test_qt_se_prueba_tambien_con_pantalla_en_linux(monkeypatch):
    monkeypatch.setattr(install, "LINUX", True)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    platforms = []

    def run(command, env, **kwargs):
        platforms.append(env.get("QT_QPA_PLATFORM"))
        return Result(1 if len(platforms) == 2 else 0, "no se pudo cargar xcb")

    monkeypatch.setattr(install.subprocess, "run", run)
    assert install.qt_problem() == "no se pudo cargar xcb"
    assert platforms == ["offscreen", None]


# ----------------------------------------------------------------- común

@pytest.fixture
def unix_home(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "WINDOWS", False)
    monkeypatch.setattr(install, "COMMAND", tmp_path / "real")
    monkeypatch.setattr(install.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setenv("PATH", "/usr/bin")
    return tmp_path


@unix_only
def test_comando_unix_es_un_enlace_al_entorno(unix_home, monkeypatch):
    monkeypatch.setenv("PATH", str(unix_home / ".local" / "bin"))
    assert install.create_command() == "Para empezar escribe:  ballistiq"
    assert (unix_home / ".local" / "bin" / "ballistiq").resolve() == (unix_home / "real").resolve()


@unix_only
def test_al_anadir_al_path_pide_una_terminal_nueva_y_da_la_ruta(unix_home, monkeypatch):
    answer(monkeypatch, True)
    message = install.create_command()
    assert "terminal nueva" in message and str(unix_home / "real") in message
    assert "Para empezar escribe:  ballistiq" not in message
    assert 'export PATH="$HOME/.local/bin:$PATH"' in (unix_home / ".zshrc").read_text()


@unix_only
def test_repetir_en_una_terminal_antigua_no_duplica_el_path(unix_home, monkeypatch):
    answer(monkeypatch, True)
    install.create_command()

    def must_not_ask(question):
        raise AssertionError("no debe volver a preguntar")

    monkeypatch.setattr(install, "ask_yes", must_not_ask)
    assert "terminal nueva" in install.create_command()
    assert (unix_home / ".zshrc").read_text().count("export PATH") == 1


@unix_only
def test_si_rechazas_el_path_da_la_ruta_y_no_toca_el_perfil(unix_home, monkeypatch, capsys):
    answer(monkeypatch, False)
    message = install.create_command()
    assert "ruta completa" in message and str(unix_home / "real") in message
    assert "terminal nueva" not in message
    assert not (unix_home / ".zshrc").exists()
    assert "export PATH" in capsys.readouterr().out


def test_si_no_se_puede_crear_el_comando_indica_la_ruta(tmp_path, monkeypatch, capsys):
    blocked = tmp_path / "soy_un_archivo"
    blocked.write_text("")
    monkeypatch.setattr(install, "command_link", lambda: blocked / "ballistiq")
    assert str(install.COMMAND) in install.create_command()
    assert "No se pudo crear el comando" in capsys.readouterr().out


def test_el_instalador_copia_los_archivos_que_necesita_pip():
    for name in ("ballistiq.py", "pyproject.toml", "README.md"):
        assert (Path(install.__file__).parent / name).exists()
