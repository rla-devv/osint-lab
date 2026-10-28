"""Novas bases (CEAF, CEPIM, PEP, lista suja, IBAMA, CVM, RAB) com arquivos sintéticos no layout oficial."""
import io
import json
import zipfile

from osintlab import pesquisa
from osintlab.bases import store, sync
from osintlab.bases.download import Downloaded
from osintlab.privacy import cpf_confere_molde, moldes_cpf

CPF = "52998224725"  # sintético (dígitos verificadores válidos)


def _csv(rows, enc="latin-1", bom=False):
    txt = "\n".join(";".join(f'"{c}"' for c in r) for r in rows)
    return (("﻿" if bom else "") + txt).encode(enc)


def _zip(nome, dados):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if isinstance(dados, dict):
            for n, d in dados.items():
                z.writestr(n, d)
        else:
            z.writestr(nome, dados)
    return buf.getvalue()


def _servir(monkeypatch, modulo, arquivos):
    """fetch_file/head falsos: o nome do arquivo é o fim da URL (ou uma chave contida nela)."""
    def achar(url):
        return next(v for k, v in arquivos.items() if k in url)

    def fake_file(url, dest, **kw):
        dest.write_bytes(achar(url))
        return Downloaded(dest, url, 200, False, dest.stat().st_size, "0" * 64, None, None)

    monkeypatch.setattr(modulo, "fetch_file", fake_file)
    monkeypatch.setattr(modulo, "head", lambda url, **kw: {"status": 200, "size": 5000, "etag": '"e1"',
                                                           "last_modified": "x"})


def _instalar(fid):
    r = sync.run(fid)
    assert r["status"] == "concluida", r
    conn = store.open_current(fid)
    dump = " ".join(str(tuple(x)) for x in conn.execute("SELECT * FROM entidades"))
    conn.close()
    assert CPF not in dump and "529.982.247-25" not in dump  # nunca o CPF completo
    return dump


def test_portal_da_transparencia_ceaf_cepim_pep(monkeypatch):
    import osintlab.bases.fontes.cgu as m
    ceaf = _csv([["CADASTRO", "CÓDIGO DA SANÇÃO", "TIPO DE PESSOA", "CPF OU CNPJ DO SANCIONADO", "NOME DO SANCIONADO",
                  "CATEGORIA DA SANÇÃO", "DATA INÍCIO SANÇÃO", "CARGO EFETIVO", "ÓRGÃO DE LOTAÇÃO", "ÓRGÃO SANCIONADOR",
                  "PUBLICAÇÃO"],
                 ["CEAF", "1", "F", "***.982.247-**", "SERVIDOR SINTETICO DA SILVA", "Demissão", "01/02/2025",
                  "ANALISTA", "ORGAO SINTETICO", "ORGAO SINTETICO", "DOU Seção 2"]])
    cepim = _csv([["CNPJ ENTIDADE", "NOME ENTIDADE", "NÚMERO CONVÊNIO", "ÓRGÃO CONCEDENTE", "MOTIVO DO IMPEDIMENTO"],
                  ["11222333000181", "ASSOCIACAO SINTETICA", "123", "MINISTERIO SINTETICO", "NAO APRESENTACAO DE CONTAS"]])
    pep = _csv([["CPF", "Nome_PEP", "Sigla_Função", "Descrição_Função", "Nível_Função", "Nome_Órgão",
                 "Data_Início_Exercício", "Data_Fim_Exercício", "Data_Fim_Carência"],
                [CPF, "PREFEITA SINTETICA", "PREF", "PREFEITO", "", "MUNICIPIO SINTETICO", "01/01/2025", "", ""]])
    _servir(monkeypatch, m, {"/ceaf/": _zip("x_Expulsoes.csv", ceaf), "/cepim/": _zip("x_CEPIM.csv", cepim),
                             "/pep/": _zip("x_PEP.csv", pep)})
    _instalar("cgu_ceaf")
    _instalar("cgu_cepim")
    assert "***.982.247-**" in _instalar("cgu_pep")  # CPF completo da origem vira a máscara pública
    r = pesquisa.run("Servidor Sintetico da Silva", forcar="pessoa", confirmado=True, attrs={"cpf": CPF})
    h = next(h for h in r["resultados"] if h["fonte"] == "cgu_ceaf")
    assert h["nivel"] == "provavel" and "Demissão" in h["resumo"]
    r = pesquisa.run("11.222.333/0001-81")
    assert any(h["fonte"] == "cgu_cepim" and h["nivel"] == "documental" for h in r["resultados"])
    r = pesquisa.run("Prefeita Sintetica", forcar="pessoa", confirmado=True)
    assert any(h["fonte"] == "cgu_pep" and "PREFEITO" in h["resumo"] for h in r["resultados"])


LISTA = """Cadastro de Empregadores que tenham submetido trabalhadores a condições análogas à escravidão
Atualização periódica de 6 de outubro de 2026.
ID
UF Empregador CNPJ/CPF Estabelecimento Trabalhadores
Inclusão no
Cadastro de
Empregadores
1 2024 SP 11.222.333 FULANO SINTETICO
DOS SANTOS 11.222.333/0001-81 RUA SINTETICA, 1, CENTRO, CIDADE/SP 2 4399-1/03 15/05/2025 06/10/2025
2 2025 MG
EMPRESA SINTETICA DE
OBRAS LTDA
44.555.666/0001-81 FAZENDA SINTETICA, ZONA
RURAL, CIDADE/MG 5 0892-4/01 27/02/2025 06/10/2025
Página 1 de 2
Cadastro de Empregadores que tenham submetido trabalhadores a condições análogas à escravidão
ID
Inclusão no
Cadastro de
Empregadores
3 BELTRANO SINTETICO (*1) 529.982.247-25 10/04/2026
4 2023 DF FULANA SINTETICA 529.982.247-25 SITIO SINTETICO, CIDADE/DF 1 0151-2/02 25/10/2024 09/04/2025 a
12/08/2026, 04/09/2026
(*1) Incluído no Cadastro em cumprimento à sentença proferida no processo judicial sintético
Página 2 de 2"""


def test_lista_suja_le_quebras_de_pagina_e_inclusao_judicial(monkeypatch):
    from osintlab.bases.fontes import fiscalizacao as m
    regs, atualizacao = m.parse_lista_suja(LISTA)
    assert atualizacao == "6 de outubro de 2026" and [r["id"] for r in regs] == ["1", "2", "3", "4"]
    assert regs[0]["empregador"] == "FULANO SINTETICO DOS SANTOS" and regs[0]["trabalhadores"] == 2
    assert regs[1]["empregador"] == "EMPRESA SINTETICA DE OBRAS LTDA" and regs[1]["uf"] == "MG"
    assert regs[2]["judicial"].startswith("Incluído no Cadastro em cumprimento") and regs[2]["documento"] == "***.982.247-**"
    assert regs[3]["inclusao"] == "04/09/2026"

    class Pagina:
        def extract_text(self):
            return LISTA

    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", lambda caminho: type("R", (), {"pages": [Pagina()]})())
    _servir(monkeypatch, m, {"cadastro_de_empregadores": b"%PDF-1.4 sintetico"})
    _instalar("mte_lista_suja")
    r = pesquisa.run("44.555.666/0001-81")
    h = next(h for h in r["resultados"] if h["fonte"] == "mte_lista_suja")
    assert h["nivel"] == "documental" and "5 trabalhador(es)" in h["resumo"]


def test_ibama_cvm_e_rab(monkeypatch):
    from osintlab.bases.fontes import fiscalizacao as m
    auto_cab = ["SEQ_AUTO_INFRACAO", "NUM_AUTO_INFRACAO", "DAT_HORA_AUTO_INFRACAO", "DES_STATUS_FORMULARIO",
                "SIT_CANCELADO", "VAL_AUTO_INFRACAO", "DES_INFRACAO", "MUNICIPIO", "UF", "NOME_INFRATOR",
                "CPF_CNPJ_INFRATOR"]
    autos = {"auto_infracao_1990.csv": _csv([auto_cab, ["1", "A1", "1990-01-01", "Lavrado", "N", "10", "Antiga", "X",
                                                        "PA", "INFRATOR ANTIGO", "11222333000181"]], "utf-8", True),
             "auto_infracao_2024.csv": _csv([auto_cab, ["2", "A2", "2024-03-01 10:00", "Lavrado", "N", "5000,00",
                                                        "Desmatamento sintético", "CIDADE", "PA", "FAZENDEIRO SINTETICO",
                                                        CPF]], "utf-8", True)}
    emb = _csv([["SEQ_TAD", "NUM_TAD", "DAT_EMBARGO", "DES_STATUS_FORMULARIO", "SIT_CANCELADO", "NOME_EMBARGADO",
                 "CPF_CNPJ_EMBARGADO", "QTD_AREA_EMBARGADA", "NOME_IMOVEL", "MUNICIPIO", "UF", "SIT_DESEMBARGO",
                 "DAT_DESEMBARGO"],
                ["9", "T9", "2024-04-01 00:00", "Lavrado", "N", "FAZENDEIRO SINTETICO", CPF, "71,9", "SITIO SINTETICO",
                 "CIDADE", "PA", "", ""]], "utf-8", True)
    cvm = _zip("", {"processo_sancionador.csv": _csv([["NUP", "Objeto", "Ementa", "Data_Abertura", "Fase_Atual",
                                                       "Subfase_Atual", "Data_Ultima_Movimentacao"],
                                                      ["123", "Apurar irregularidades sintéticas", "", "2026-01-01",
                                                       "Citação e envio de defesas", "", "2026-02-01"]]),
                    "processo_sancionador_acusado.csv": _csv([["NUP", "Nome_Acusado", "Situacao", "Data_Situacao"],
                                                              ["123", "DIRETOR SINTETICO", "Citado", "2026-01-10"],
                                                              ["123", "GESTORA SINTETICA LTDA", "Citado", "2026-01-10"]])})
    donos = json.dumps([{"NOME": "TAXI AEREO SINTETICO LTDA", "DOCUMENTO": "11222333000181", "PERCENTUAL": "100",
                         "UF": "SP"}])
    oper = json.dumps([{"NOME": "PILOTO SINTETICO", "DOCUMENTO": "529XXXXXX25", "UF": "SP"}])
    rab = ("Atualizado em: 2026-10-27\n" + ";".join(f'"{c}"' for c in ["MARCAS", "PROPRIETARIOS", "OPERADORES",
                                                                     "DS_MODELO", "NM_FABRICANTE", "NR_ANO_FABRICACAO",
                                                                     "DS_GRAVAME", "TP_OPERACAO"]) + "\n"
           + ";".join('"' + c.replace('"', '""') + '"' for c in ["PRXYZ", donos, oper, "172M", "CESSNA SINTETICA",
                                                                  "1999", "NENHUM GRAVAME REGISTRADO", "PRIVADO"]))
    _servir(monkeypatch, m, {"auto_infracao_csv.zip": _zip("", autos), "termo_de_embargo.csv": emb,
                             "processo_sancionador.zip": cvm, "dados_aeronaves.csv": ("﻿" + rab).encode()})
    dump = _instalar("ibama_fiscalizacao")
    conn = store.open_current("ibama_fiscalizacao")
    assert conn.execute("SELECT COUNT(*) FROM cpf_idx WHERE molde='***982247**'").fetchone()[0] == 2  # índice de CPF
    conn.close()
    assert "INFRATOR ANTIGO" not in dump and "***.982.247-**" in dump  # antes de 2005 fica de fora
    r = pesquisa.run("Fazendeiro Sintetico", forcar="pessoa", confirmado=True, attrs={"cpf": CPF})
    ib = [h for h in r["resultados"] if h["fonte"] == "ibama_fiscalizacao"]
    assert len(ib) == 2 and {h["nivel"] for h in ib} == {"provavel"}
    assert any("Embargo nº T9" in h["resumo"] for h in ib) and any("R$ 5000,00" in h["resumo"] for h in ib)
    _instalar("cvm_sancionador")
    r = pesquisa.run("Gestora Sintetica Ltda", forcar="organizacao")
    assert any(h["fonte"] == "cvm_sancionador" and "Citação" in h["resumo"] for h in r["resultados"])
    _instalar("anac_rab")
    assert pesquisa.classify("PR-XYZ")["tipos"] == ["aeronave"] and pesquisa.classify("PRATA")["tipos"] != ["aeronave"]
    r = pesquisa.run("pr xyz", forcar="aeronave")
    h = next(h for h in r["resultados"] if h["fonte"] == "anac_rab")
    assert "CESSNA SINTETICA" in h["resumo"] and "TAXI AEREO SINTETICO LTDA" in h["resumo"]
    assert any(h["fonte"] == "anac_rab" for h in pesquisa.run("11.222.333/0001-81")["resultados"])
    r = pesquisa.run("Piloto Sintetico", forcar="pessoa", confirmado=True, attrs={"cpf": CPF})
    assert next(h for h in r["resultados"] if h["fonte"] == "anac_rab")["nivel"] == "provavel"  # molde da ANAC


def test_moldes_de_cpf_publicados():
    moldes = moldes_cpf("a ***.982.247-** b ***982247** c 529XXXXXX25 d ***.982.***-**")
    assert sorted(moldes) == sorted(["***982247**", "***982*****", "***982247**", "529******25"])
    assert all(cpf_confere_molde(CPF, x) for x in moldes)
    assert not cpf_confere_molde("11112345663", "529******25")


def test_consultas_assistidas_novas_e_paginas():
    from fastapi.testclient import TestClient

    from osintlab.web.app import app
    r = pesquisa.run("11.222.333/0001-81")
    links = {a["fonte"]: a["url"] for a in r["assistidas"]}
    assert {"cenprot_protestos", "ri_digital", "juntas_comerciais", "dou_busca", "escavador"} <= set(links)
    assert links["dou_busca"].startswith("https://www.in.gov.br/consulta/-/buscar/dou?q=%2211.222.333")
    r = pesquisa.run("Construtora Sintetica Ltda", forcar="organizacao")
    assert {"occrp_aleph", "equasis", "dou_busca"} <= {a["fonte"] for a in r["assistidas"]}
    c = TestClient(app)
    for fid in ("cgu_ceaf", "cgu_cepim", "cgu_pep", "mte_lista_suja", "ibama_fiscalizacao", "cvm_sancionador",
                "anac_rab", "cenprot_protestos", "occrp_aleph"):
        assert c.get(f"/central/fonte/{fid}").status_code == 200, fid
    assert c.get("/central").status_code == 200
