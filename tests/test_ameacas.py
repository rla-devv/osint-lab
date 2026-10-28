"""Inteligência de ameaças: hash (MalwareBazaar/ThreatFox), VirusTotal/Shodan/GreyNoise/Censys (chave), tudo com
respostas sintéticas e só leitura. Conectores com chave ausente ficam 'não executada' na pesquisa."""
import pytest

from osintlab import http, orquestrador as orq, pesquisa, plugins
from osintlab.bases import credentials
from osintlab.connectors import ameacas


class Resp:
    evidence_sha256 = None
    status = 200


def test_tipo_hash():
    assert ameacas.tipo_hash("44d88612fea8a8f36de82e1278abb02f") == "md5"
    assert ameacas.tipo_hash("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855") == "sha256"
    assert ameacas.tipo_hash("xyz") is None


def test_malwarebazaar_e_threatfox(monkeypatch):
    monkeypatch.setattr(credentials, "get", lambda n: "chave-sintetica" if n == "abusech" else None)
    monkeypatch.setattr(ameacas.httpx, "post", lambda url, **kw: type("R", (), {
        "raise_for_status": lambda self: None,
        "json": lambda self: {"query_status": "ok", "data": [{"sha256_hash": "abc", "file_name": "x.exe",
            "signature": "AgentTesla", "file_size": 1024, "tags": ["exe"]}]} if "mb-api" in url else
            {"query_status": "ok", "data": [{"ioc": "1.2.3.4:443", "threat_type": "botnet_cc",
             "malware_printable": "Cobalt Strike", "confidence_level": 90}]}})())
    d = ameacas.malwarebazaar("44d88612fea8a8f36de82e1278abb02f")
    assert d["dados"]["assinatura"] == "AgentTesla" and "sample/abc" in d["url_consulta"]
    d = ameacas.threatfox("1.2.3.4")
    assert d["itens"][0]["malware"] == "Cobalt Strike"


def test_abusech_sem_chave_levanta(monkeypatch):
    monkeypatch.setattr(credentials, "get", lambda n: None)
    monkeypatch.delenv("OSINTLAB_ABUSECH_KEY", raising=False)
    with pytest.raises(RuntimeError, match="abuse.ch"):
        ameacas.malwarebazaar("44d88612fea8a8f36de82e1278abb02f")


def test_virustotal_leitura(monkeypatch):
    monkeypatch.setattr(credentials, "get", lambda n: "vt" if n == "virustotal" else None)
    chamada = {}
    def fake(url, **kw):
        chamada["url"] = url; chamada["headers"] = kw.get("headers")
        return {"data": {"attributes": {"last_analysis_stats": {"malicious": 3, "harmless": 60},
                "reputation": -5, "popular_threat_classification": {"suggested_threat_label": "trojan.x"}}}}, Resp()
    monkeypatch.setattr(http, "get_json", fake)
    d = ameacas.virustotal("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
    assert "/files/" in chamada["url"] and chamada["headers"]["x-apikey"] == "vt"  # hash → files, só leitura
    assert d["dados"]["maliciosos"] == 3 and d["dados"]["rotulo"] == "trojan.x"


def test_greynoise_404_e_censys_formato(monkeypatch):
    from osintlab.http import APIError
    monkeypatch.setattr(credentials, "get", lambda n: {"greynoise": "g", "censys": "id:seg"}.get(n))
    monkeypatch.setattr(http, "get_json", lambda url, **kw: (_ for _ in ()).throw(APIError("HTTP 404 em x")))
    assert ameacas.greynoise("203.0.113.5")["dados"] == {"visto": False}
    with pytest.raises(RuntimeError, match="id_da_api:segredo"):
        monkeypatch.setattr(credentials, "get", lambda n: "sem-dois-pontos")
        ameacas.censys("203.0.113.5")


def test_na_pesquisa_conectores_com_chave_ficam_nao_executada(monkeypatch):
    monkeypatch.setattr(credentials, "get", lambda n: None)
    monkeypatch.delenv("OSINTLAB_ABUSECH_KEY", raising=False)
    ex = orq.Execucao("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", conectores=True, timeout_online=2)
    e = {x.id: x for x in ex.etapas}
    assert e["conector:malwarebazaar"].estado == "nao_executada" and "abusech" in e["conector:malwarebazaar"].motivo
    assert e["conector:virustotal"].estado == "nao_executada" and "virustotal" in e["conector:virustotal"].motivo
    # com a chave abuse.ch, o malwarebazaar passa a ser executável
    monkeypatch.setattr(credentials, "get", lambda n: "k" if n == "abusech" else None)
    ex2 = orq.Execucao("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", conectores=True, timeout_online=2)
    e2 = {x.id: x for x in ex2.etapas}
    assert e2["conector:malwarebazaar"].estado == "pendente"
