"""O CLI importa e todos os comandos têm `--help` (apanha erros de sintaxe que os outros testes não veem)."""

from __future__ import annotations

import pytest

from osuml.cli import main


@pytest.mark.parametrize("argv", [["collect"], ["status"], ["export"], ["maps"], ["sync-s3"], ["panel"], ["poll"], ["categorize"]])
def test_every_command_has_help(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        main([*argv, "--help"])
    assert exc.value.code == 0 and "usage:" in capsys.readouterr().out


def test_every_subcommand_has_a_handler_and_poll_status_runs(tmp_path, monkeypatch, capsys):
    """Apanha funções em falta no despacho (já aconteceu com `cmd_poll`) e corre um comando sem rede."""
    import argparse

    from osuml.cli import _handlers

    handlers = _handlers()  # NameError se algum `cmd_*` não existir
    parser_commands = {"collect", "status", "export", "maps", "sync-s3", "panel", "poll", "categorize"}
    assert set(handlers) == parser_commands and all(callable(h) for h in handlers.values())

    monkeypatch.setenv("OSUML_DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("OSUML_DATA_DIR", str(tmp_path / "data"))
    assert main(["--env-file", str(tmp_path / "nao-existe.env"), "poll", "--status"]) == 0
    assert '"tracked": 0' in capsys.readouterr().out
    assert isinstance(argparse.ArgumentParser, type)
