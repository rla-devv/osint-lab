"""Backup automático cifrado: retenção, abertura com a senha e espera pelo desbloqueio do cofre."""
import pytest

pytest.importorskip("cryptography")

from osintlab import backup_auto, cofre  # noqa: E402


def test_backup_automatico_diario_com_retencao(tmp_path, demo):
    pasta = tmp_path / "nuvem"
    backup_auto.ativar("senha-de-backup-1", str(pasta), manter=2)
    assert "senha-de-backup-1" not in backup_auto._arquivo().read_text()  # só a chave derivada
    for _ in range(3):
        assert backup_auto.executar(forcar=True)
    arquivos = sorted(pasta.glob("osintlab-auto-*.osintlab"))
    assert len(arquivos) <= 2
    assert backup_auto.executar() is None  # já fez hoje
    r = cofre.restore(arquivos[-1], "senha-de-backup-1", tmp_path / "restaurado")
    assert r["arquivos"] > 0 and (tmp_path / "restaurado" / "osintlab.db").exists()
    with pytest.raises(ValueError):
        cofre.restore(arquivos[-1], "senha-errada-123", tmp_path / "x")
    backup_auto.desativar()
    assert not backup_auto.estado()["ativo"] and backup_auto.executar(forcar=True) is None


def test_pasta_indisponivel_vira_aviso(tmp_path, demo):
    backup_auto.ativar("senha-de-backup-1", str(tmp_path / "pendrive"))
    (tmp_path / "pendrive").rmdir()
    (tmp_path / "pendrive").write_text("agora é um arquivo")  # pasta "sumiu"
    assert backup_auto.executar(forcar=True) is None and backup_auto.estado()["erro"]
