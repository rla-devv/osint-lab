"""Conectores com cadastro gratuito (respostas sintéticas): EDGAR, Companies House, urlscan, OTX, AbuseIPDB."""
import pytest

from osintlab import http, pesquisa
from osintlab.bases import credentials
from osintlab.connectors import registros as r

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>x</title>
<entry><title>EMPRESA SINTETICA INC</title><content type="text/xml"><company-info><cik>0000000001</cik>
<conformed-name>EMPRESA SINTETICA INC</conformed-name><state>DE</state><assigned-sic-desc>SERVICES</assigned-sic-desc>
</company-info></content></entry></feed>"""


class Resp:
    def __init__(self, content=b"", url="https://x.example"):
        self.content, self.final_url, self.evidence_sha256, self.status = content, url, None, 200


def test_edgar_sem_chave(monkeypatch):
    vistos = {}
    monkeypatch.setattr(http, "fetch", lambda url, **kw: vistos.update(kw) or Resp(ATOM))
    d = r.sec_edgar("Empresa Sintetica")
    assert d["dados"]["empresas"][0] == {"nome": "EMPRESA SINTETICA INC", "cik": "0000000001", "estado": "DE",
                                         "setor": "SERVICES"}
    assert vistos["headers"]["User-Agent"].startswith("OSINTLab/")


def test_chave_obrigatoria_e_opcional(monkeypatch):
    monkeypatch.setattr(credentials, "get", lambda nome: None)
    with pytest.raises(RuntimeError, match="Credenciais"):
        r.abuseipdb("203.0.113.5")
    chamadas = []
    monkeypatch.setattr(http, "get_json", lambda url, **kw: chamadas.append((url, kw.get("headers"))) or
                        ({"results": [{"task": {"time": "2026-10-01"}, "page": {"url": "https://exemplo.example/",
                                                                                 "ip": "203.0.113.5", "country": "BR"}}],
                          "total": 1}, Resp()))
    d = r.urlscan("exemplo.example")  # funciona sem chave
    assert chamadas[-1][1] == {} and "exemplo.example" in d["texto"]
    monkeypatch.setattr(credentials, "get", lambda nome: "chave-sintetica" if nome == "urlscan" else None)
    r.urlscan("exemplo.example")
    assert chamadas[-1][1] == {"API-Key": "chave-sintetica"}


def test_pesquisa_omite_conector_sem_chave(monkeypatch):
    from osintlab import plugins
    monkeypatch.setattr(credentials, "get", lambda nome: None)
    reg = plugins.load_all()
    for n in ("rdap", "dns", "crtsh", "wayback", "urlscan", "otx", "asn", "internetdb"):
        monkeypatch.setattr(reg[n], "func", lambda v: {"texto": "ok"})
    nomes = {c["conector"] for c in pesquisa.run("203.0.113.5", buscar_complementares=True, timeout_online=2)["complementares"]}
    assert "abuseipdb" not in nomes and {"urlscan", "otx"} <= nomes  # AbuseIPDB exige chave não cadastrada
