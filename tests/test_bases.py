"""Testes da Central de Bases: sincronização completa/incremental, consistência, reversão,
bases tabulares, permissões por categoria, credenciais, consultas assistidas e pesquisa unificada.
Toda a rede é simulada com dados sintéticos."""
import json

import pytest

from osintlab import core, http
from osintlab.bases import assistida, credentials, store, sync
from osintlab.bases.catalog import fonte, fontes
from osintlab.bases.download import Downloaded


def _ent(i, name, op=None, **props):
    e = {"id": f"NK-{i}", "schema": props.pop("schema", "Person"), "caption": name, "target": True,
         "datasets": ["ds_sintetico"], "properties": {"name": [name], **props}}
    return {"op": op, "entity": e} if op else e


class FakeOS:
    """Simula o índice, o arquivo de entidades e os deltas do OpenSanctions."""

    def __init__(self):
        self.version = "20260101000000-aaa"
        self.files = {
            "full-20260101000000-aaa": [_ent(1, "Empresa Fictícia Alfa", schema="Company", registrationNumber=["11222333000181"]),
                                        _ent(2, "Fulano Sintético de Tal", birthDate=["1970-01-01"], country=["xx"]),
                                        _ent(3, "Beltrano Exemplo")],
        }
        self.deltas = {}

    def publish_delta(self, version, ops):
        self.deltas[version] = ops
        self.version = version

    def get_json(self, url, **kw):
        if url.endswith("/index.json"):
            return {"version": self.version, "updated_at": "2026-01-02T00:00:00", "name": "ds_sintetico",
                    "delta_url": "https://os.example/delta.json", "target_count": 3,
                    "resources": [{"name": "entities.ftm.json", "url": f"https://os.example/full-{self.version}",
                                   "size": 100, "checksum": None}]}, None
        if url.endswith("delta.json"):
            vs = {"20260101000000-aaa": "https://os.example/d-20260101000000-aaa"}
            vs.update({v: f"https://os.example/d-{v}" for v in self.deltas})
            return {"versions": vs}, None
        raise AssertionError(url)

    def fetch_file(self, url, dest, **kw):
        key = url.rsplit("/", 1)[-1]
        if key.startswith("d-"):
            lines = self.deltas.get(key[2:], [])
        else:
            lines = self.files[key] if key in self.files else self.files["full-20260101000000-aaa"]
        dest.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
        return Downloaded(dest, url, 200, False, dest.stat().st_size, "0" * 64, None, None)


@pytest.fixture
def fake_os(monkeypatch):
    f = FakeOS()
    monkeypatch.setattr(http, "get_json", f.get_json)
    import osintlab.bases.fontes.opensanctions as m
    monkeypatch.setattr(m, "fetch_file", f.fetch_file)
    return f


def test_catalogo_categorias_e_restricoes():
    reg = fontes()
    assert {"opensanctions", "fbi_wanted", "ibge_localidades", "cnes", "bnmp", "interpol_red"} <= set(reg)
    assert reg["bnmp"].categoria == "C" and not reg["bnmp"].sincronizavel and not reg["bnmp"].online
    assert "automação" in reg["bnmp"].restricoes
    assert reg["beneficios_individuais"].situacao == "indisponivel"
    for f in reg.values():
        assert f.categoria in "ABCDE" and f.nome and f.url_oficial


def test_categoria_c_nunca_sincroniza():
    r = sync.run("bnmp")
    assert r["status"] == "falhou" and "Categoria C" in r["error"]


def test_sincronizacao_completa_incremental_e_reversao(fake_os):
    r = sync.run("opensanctions")
    assert r["status"] == "concluida" and r["method"] == "completa" and r["records"] == 3
    assert sync.run("opensanctions")["status"] == "atualizada"  # sem nova versão: nada baixado
    fake_os.publish_delta("20260102000000-bbb", [
        _ent(4, "Ciclano Novo", op="ADD"), _ent(3, "Beltrano Exemplo Corrigido", op="MOD"),
        {"op": "DEL", "entity": {"id": "NK-2"}}])
    r = sync.run("opensanctions")
    assert r["method"] == "incremental" and (r["added"], r["modified"], r["deleted"]) == (1, 1, 1)
    conn = store.open_current("opensanctions")
    ids = {x[0] for x in conn.execute("SELECT id FROM entidades")}
    conn.close()
    assert ids == {"NK-1", "NK-3", "NK-4"}  # registro removido na origem não permanece vigente
    hist = sync.history("opensanctions")[0]
    assert [s["passo"] for s in hist["steps"]][:5] == [1, 2, 3, 4, 5]
    assert sync.rollback("opensanctions") == "20260101000000-aaa"
    assert sync.state("opensanctions")["records"] == 3
    assert sync.verify_integrity("opensanctions")["ok"]


def test_falha_mantem_versao_atual(fake_os, monkeypatch):
    sync.run("opensanctions")
    fake_os.version = "20260103000000-ccc"
    fake_os.files["full-20260103000000-ccc"] = []  # arquivo vazio/truncado
    r = sync.run("opensanctions", mode="completa")
    assert r["status"] == "falhou"
    st = sync.state("opensanctions")
    assert st["current_version"] == "20260101000000-aaa" and st["fail_count"] == 1
    assert store.version_path("opensanctions", st["current_version"]).exists()
    assert not list((store.base_dir("opensanctions") / ".staging").glob("*.sqlite"))


def test_queda_brusca_de_registros_bloqueada(fake_os):
    fake_os.files["full-20260101000000-aaa"] = [_ent(i, f"Nome {i}") for i in range(10)]
    sync.run("opensanctions")
    fake_os.version = "20260104000000-ddd"
    fake_os.files["full-20260104000000-ddd"] = [_ent(1, "Nome 1")]
    r = sync.run("opensanctions", mode="completa")
    assert r["status"] == "falhou" and "queda" in r["error"]
    r = sync.run("opensanctions", mode="completa", force=True)
    assert r["status"] == "concluida", r


def test_agendamento_respeita_intervalo(fake_os):
    sync.run("opensanctions")
    sync.set_enabled("opensanctions", True)
    sync.update_state("opensanctions", interval_hours=6)
    assert "opensanctions" not in sync.due()
    sync.update_state("opensanctions", last_check="2000-01-01T00:00:00+00:00")
    assert "opensanctions" in sync.due()
    sync.update_state("opensanctions", paused=1)
    assert "opensanctions" not in sync.due()


def test_base_tabular_diff_por_chave(monkeypatch, tmp_path):
    from osintlab.bases.fontes import tabular
    from osintlab.bases.catalog import registrar, REGISTRY

    contents = {"v": "cnpj;razao;situacao\n11222333000181;Alfa Ltda;ativa\n22333444000155;Beta SA;ativa\n"}

    def fake_head(url, **kw):
        return {"status": 200, "etag": None, "last_modified": None, "size": None}

    def fake_fetch(url, dest, **kw):
        dest.write_text(contents["v"], encoding="utf-8")
        import hashlib
        return Downloaded(dest, url, 200, False, dest.stat().st_size,
                          hashlib.sha256(contents["v"].encode()).hexdigest(), None, None)

    monkeypatch.setattr(tabular, "head", fake_head)
    monkeypatch.setattr(tabular, "fetch_file", fake_fetch)

    @registrar
    class Sintetica(tabular.TabularFonte):
        id = "tab_sintetica"
        nome = "Tabela sintética"
        categoria = "B"
        url_oficial = "https://dados.example/"
        arquivos = [{"url": "https://dados.example/t.csv", "tabela": "empresas", "chave": "cnpj"}]
        titulo_cols = ["razao"]
        entradas = ["cnpj", "texto"]
    try:
        assert sync.run("tab_sintetica")["records"] == 2
        assert sync.run("tab_sintetica")["status"] == "atualizada"  # mesmo conteúdo → mesmo hash
        contents["v"] = "cnpj;razao;situacao\n11222333000181;Alfa Ltda;baixada\n33444555000166;Gama ME;ativa\n"
        r = sync.run("tab_sintetica")
        assert r["status"] == "concluida", r
        assert (r["added"], r["modified"], r["deleted"]) == (1, 1, 1)
        conn = store.open_current("tab_sintetica")
        hits = fontes()["tab_sintetica"].search_local(conn, "11.222.333/0001-81", "cnpj")
        conn.close()
        assert hits and hits[0].atributos["situacao"] == "baixada"
    finally:
        REGISTRY.pop("tab_sintetica", None)


def test_credenciais_nao_vazam(tmp_path):
    credentials.set("teste_api", "segredo-super-123")
    assert credentials.get("teste_api") == "segredo-super-123"
    assert "segredo" not in credentials.masked("teste_api")
    from osintlab.db import get_db
    dump = json.dumps(get_db().query("SELECT * FROM audit"))
    assert "segredo-super-123" not in dump
    credentials.delete("teste_api")
    assert credentials.get("teste_api") is None


def test_consulta_assistida_exige_justificativa_e_expira():
    pid = core.create_project("A", "Estudo de metodologia de verificação de registros oficiais")
    with pytest.raises(ValueError):
        assistida.register(pid, "bnmp", "Nome Sintético", "nao_encontrado", "", "curta")
    with pytest.raises(ValueError):
        assistida.register(pid, "opensanctions", "x", "encontrado", "", "justificativa suficientemente longa")
    lid = assistida.register(pid, "bnmp", "Nome Sintético", "nao_encontrado", "Nenhum mandado listado",
                             "Teste com dados sintéticos para validar o fluxo assistido", b"%PDF-1.4 comprovante",
                             "comprovante.pdf")
    rows = assistida.list_(pid)
    assert rows[0]["id"] == lid and rows[0]["valid_until"] and not rows[0]["vencida"]
    item = core.get_item(rows[0]["item_id"])
    assert item["evidence_sha256"] and item["metadata"]["valido_ate"]


def test_pesquisa_unificada_niveis(fake_os):
    from osintlab import pesquisa
    sync.run("opensanctions")
    r = pesquisa.run("Empresa Fictícia Alfa")
    assert r["resultados"][0]["nivel"] == "hipotese"  # só o nome coincide
    r = pesquisa.run("11.222.333/0001-81")
    assert r["consulta"]["tipos"][0] == "cnpj" and r["resultados"][0]["nivel"] == "documental"
    r = pesquisa.run("Fulano Sintético de Tal")
    assert r["requer_confirmacao"] and not r["resultados"]
    r = pesquisa.run("Fulano Sintético de Tal", confirmado=True, attrs={"nascimento": "1970-01-01"})
    assert r["resultados"][0]["nivel"] == "provavel"
    r = pesquisa.run("Fulano Sintético de Tal", confirmado=True, attrs={"nascimento": "1985-05-05"})
    assert r["resultados"][0]["nivel"] == "contraditorio"
    assert any(a["fonte"] == "bnmp" for a in r["assistidas"])
    r = pesquisa.run("529.982.247-25")
    assert r["consulta"]["tipos"] == ["cpf"] and not r["resultados"] and r["consulta"]["avisos"]
    pid = core.create_project("P", "Estudo de metodologia de pesquisa em listas de sanções")
    hit = pesquisa.run("11.222.333/0001-81")["resultados"][0]
    ids = pesquisa.save_hits(pid, [hit], "11.222.333/0001-81")
    it = core.get_item(ids[0])
    assert it["kind"] == "registro_oficial" and it["evidence_sha256"]
    assert it["metadata"]["nivel_correspondencia"] == "documental"


def test_datajud_numero_cnj_e_consulta_simulada(monkeypatch):
    from osintlab.bases.fontes import datajud as dj
    p = dj.parse_cnj("0001234-56.2024.8.24.0023")
    assert dj.alias_tribunal(p) == "tjsc" and p["digitos"] == "00012345620248240023"
    assert dj.alias_tribunal(dj.parse_cnj("5000001-00.2023.4.04.7200")) == "trf4"
    assert dj.alias_tribunal(dj.parse_cnj("0000001-00.2022.6.07.0001")) == "tre-df"
    # dígito verificador ISO 7064 MOD 97-10: monta um número válido e confere
    base = "0001234" + "2024" + "8" + "24" + "0023"
    dv = 98 - (int(base + "00") % 97)
    assert dj.parse_cnj(f"0001234-{dv:02d}.2024.8.24.0023")["dv_valido"]
    calls = {}

    class R:
        status_code = 200
        content = b"{}"

        def raise_for_status(self):
            pass

        def json(self):
            return {"hits": {"hits": [{"_source": {"numeroProcesso": "00012345620248240023", "grau": "G1",
                    "classe": {"nome": "Procedimento Comum"}, "assuntos": [{"nome": "Indenização"}],
                    "orgaoJulgador": {"nome": "1ª Vara Cível"}, "dataAjuizamento": "2024-01-10T00:00:00",
                    "movimentos": [{"nome": "Distribuição", "dataHora": "2024-01-10T10:00:00"},
                                   {"nome": "Sentença", "dataHora": "2024-09-01T10:00:00"}]}}]}}

    def fake_post(url, json=None, headers=None, timeout=None):
        calls["url"], calls["auth"], calls["body"] = url, headers["Authorization"], json
        return R()

    monkeypatch.setattr(dj.httpx, "post", fake_post)
    monkeypatch.setattr(dj, "current_key", lambda refresh=False: dj.CHAVE_PUBLICA)
    from osintlab.config import get_settings
    get_settings().offline = False
    monkeypatch.setattr(dj.http, "check_url_allowed", lambda url: None)
    hits = fontes()["datajud"].search_online(None, "0001234-56.2024.8.24.0023", "processo")
    assert calls["url"].endswith("api_publica_tjsc/_search") and calls["auth"].startswith("APIKey ")
    assert calls["body"]["query"]["match"]["numeroProcesso"] == "00012345620248240023"
    assert hits[0].atributos["movimentos"][-1].startswith("2024-09-01")


def test_parsers_ue_uk_e_mascaramento_tcu(monkeypatch, tmp_path):
    import xml.etree.ElementTree as ET
    from osintlab.bases.fontes import sancoes_internacionais as si, tcu
    eu = ET.fromstring("""<export xmlns="http://eu.europa.ec/fpi/fsd/export" generationDate="2026-10-15">
      <sanctionEntity euReferenceNumber="EU.1.1" logicalId="9" unitedNationId="">
        <regulation programme="XYZ" numberTitle="1/2026"><publicationUrl>https://eur-lex.example/1</publicationUrl></regulation>
        <subjectType code="person"/><nameAlias wholeName="Pessoa Ficticia Um"/><nameAlias wholeName="Apelido Ficticio"/>
        <citizenship countryIso2Code="XX"/><birthdate birthdate="1970-01-01" year="1970"/>
        <identification number="P123" identificationTypeCode="passport"/></sanctionEntity></export>""")
    rows, gen = fontes()["ue_sancoes"].parse(eu)
    assert gen == "2026-10-15" and rows[0][2] == "Pessoa Ficticia Um" and "passport:P123" in rows[0][6]
    uk = ET.fromstring("""<Designations><DateGenerated>16/10/2026</DateGenerated><Designation><UniqueID>XX0001</UniqueID>
      <Names><Name><Name1>FULANO</Name1><Name6>FICTICIO</Name6><NameType>Primary Name</NameType></Name></Names>
      <IndividualEntityShip>Individual</IndividualEntityShip><RegimeName>Regime Ficticio</RegimeName>
      <IndividualDetails><Individual><DOBs><DOB>dd/mm/1960</DOB><DOB>02/03/1961</DOB></DOBs>
      <Nationalities><Nationality>Ficticia</Nationality></Nationalities></Individual></IndividualDetails></Designation></Designations>""")
    rows, gen = fontes()["uk_sancoes"].parse(uk)
    assert rows[0][2] == "FULANO FICTICIO" and rows[0][4] == "1960 | 1961-03-02" and gen == "16/10/2026"
    pages = {"inidoneos": [{"nome": "EMPRESA FICTICIA LTDA", "cpf_cnpj": "11222333000181", "processo": "1",
                            "deliberacao": "AC 1/2026"}],
             "inabilitados": [{"nome": "PESSOA FICTICIA", "cpf": "52998224725", "processo": "2", "deliberacao": "AC 2/2026"}]}
    monkeypatch.setattr(tcu, "_page_all", lambda lista, cancel=None: pages[lista])
    r = sync.run("tcu_condenacoes")
    assert r["status"] == "concluida" and r["records"] == 2
    conn = store.open_current("tcu_condenacoes")
    dump = " ".join(str(tuple(x)) for x in conn.execute("SELECT * FROM entidades"))
    conn.close()
    assert "52998224725" not in dump and "***.982.247-**" in dump and "11.222.333/0001-81" in dump


def test_icij_grafo_sintetico_e_vinculos_no_projeto(monkeypatch):
    import io
    import zipfile
    from osintlab.bases.fontes import icij
    from osintlab import pesquisa
    from osintlab.analysis.correlation import relations
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("GENERATED_ON_20260909.txt", "")
        z.writestr("nodes-entities.csv", "node_id,name,original_name,former_name,jurisdiction,jurisdiction_description,"
                   "company_type,address,internal_id,incorporation_date,inactivation_date,struck_off_date,dorm_date,"
                   "status,service_provider,ibc\n1,OFFSHORE FICTICIA LTD,,,XX,Pais Ficticio,,Rua X 1,,01-JAN-2010,,,,"
                   "Active,,\n")
        z.writestr("nodes-officers.csv", "node_id,name,countries,country_codes,sourceID,valid_until,note\n"
                   "2,DIRETOR FICTICIO,Ficticia,XXX,Leaks Ficticios,,\n")
        z.writestr("nodes-intermediaries.csv", "node_id,name,status,internal_id,address,countries,country_codes,sourceID,valid_until,note\n")
        z.writestr("nodes-others.csv", "node_id,name,type,incorporation_date,struck_off_date,closed_date,jurisdiction,"
                   "jurisdiction_description,countries,country_codes,sourceID,valid_until,note\n")
        z.writestr("relationships.csv", "node_id_start,node_id_end,rel_type,link,status,start_date,end_date,sourceID\n"
                   "2,1,officer_of,director of,,01-JAN-2011,,Leaks Ficticios\n2,9,registered_address,,,,,x\n")
    data = buf.getvalue()

    def fake_fetch(url, dest, **kw):
        dest.write_bytes(data)
        return Downloaded(dest, url, 200, False, len(data), "0" * 64, None, None)
    monkeypatch.setattr(icij, "fetch_file", fake_fetch)
    monkeypatch.setattr(icij, "head", lambda url: {"status": 200, "etag": '"abc-1"', "size": len(data)})
    monkeypatch.setattr(fontes()["icij_offshore"], "validate", lambda conn: {"ok": True, "problemas": []})
    assert sync.run("icij_offshore")["status"] == "concluida"
    conn = store.open_current("icij_offshore")
    assert conn.execute("SELECT COUNT(*) FROM relacoes").fetchone()[0] == 1  # endereço não importado
    assert "Rua X" not in str(conn.execute("SELECT propriedades FROM entidades").fetchall())
    conn.close()
    r = pesquisa.run("Offshore Ficticia Ltd")
    hit = next(h for h in r["resultados"] if h["fonte"] == "icij_offshore")
    assert hit["atributos"]["conexoes"][0]["nome"] == "DIRETOR FICTICIO"
    pid = core.create_project("ICIJ", "Estudo de metodologia de análise de vínculos com dados sintéticos")
    pesquisa.save_hits(pid, [hit], "Offshore Ficticia Ltd")
    rels = relations(pid)
    assert rels and rels[0]["level"] == "possivel" and "não implica ilicitude" in rels[0]["rationale"]
    assert rels[0]["src_value"] == "DIRETOR FICTICIO"  # direção preservada (diretor → empresa)


def test_fonte_online_lenta_nao_bloqueia_resultados(fake_os, monkeypatch):
    import time
    from osintlab import pesquisa
    sync.run("opensanctions")
    cnes = fontes()["cnes"]
    monkeypatch.setattr(cnes, "search_online", lambda *a, **k: time.sleep(3) or [])
    t = time.monotonic()
    r = pesquisa.run("11.222.333/0001-81", timeout_online=0.5)
    assert time.monotonic() - t < 2.5
    assert r["resultados"] and r["resultados"][0]["fonte"] == "opensanctions"
    assert any(e["fonte"] == "cnes" and "Tempo esgotado" in e["erro"] for e in r["erros"])


def test_vigilancia_de_registros_salvos_apos_delta(fake_os):
    from osintlab import monitor, pesquisa
    sync.run("opensanctions")
    pid = core.create_project("V", "Estudo de metodologia de acompanhamento de listas públicas de sanções")
    hits = pesquisa.run("11.222.333/0001-81")["resultados"]
    (iid,) = pesquisa.save_hits(pid, hits, "11.222.333/0001-81")
    it = core.get_item(iid)
    assert it["metadata"]["situacao_registro"] == "inalterado" and it["metadata"]["hash_registro"]
    # 1ª atualização: o registro muda de nome na origem
    fake_os.publish_delta("20260102000000-bbb", [_ent(1, "Empresa Fictícia Alfa Renomeada", op="MOD", schema="Company",
                                                      registrationNumber=["11222333000181"])])
    r = sync.run("opensanctions")
    assert r["vigilancia"]["alterados"] == 1
    it = core.get_item(iid)
    assert it["metadata"]["situacao_registro"] == "alterado" and it["status"] == "revisar"
    ev = monitor.events(pid)
    assert ev[0]["change_type"] == "alterado" and "nome:" in ev[0]["diff"]
    # 2ª atualização: o registro é removido da origem — o item não é apagado, fica marcado como histórico
    fake_os.publish_delta("20260103000000-ccc", [{"op": "DEL", "entity": {"id": "NK-1"}}])
    r = sync.run("opensanctions")
    assert r["vigilancia"]["removidos"] == 1
    assert core.get_item(iid)["metadata"]["situacao_registro"] == "removido"
    assert monitor.events(pid)[0]["change_type"] == "removido"


def test_monitor_de_pesquisa_local_alerta_novas_correspondencias(fake_os):
    from osintlab import monitor
    sync.run("opensanctions")
    pid = core.create_project("M", "Estudo de metodologia de vigilância de listas públicas")
    with pytest.raises(ValueError):  # nome de pessoa exige finalidade no rótulo
        monitor.create(pid, "pesquisa", "Fulano Sintético de Tal")
    mid = monitor.create(pid, "pesquisa", "Beltrano Exemplo", label="Acompanhamento didático com dados sintéticos")
    assert monitor.run(mid)["eventos"][0]["tipo"] == "linha_de_base"
    assert monitor.run(mid)["eventos"] == []
    fake_os.publish_delta("20260102000000-bbb", [_ent(7, "Beltrano Exemplo Junior", op="ADD")])
    sync.run("opensanctions")  # dispara os monitores de pesquisa que usam esta base
    ev = monitor.events(pid)
    assert ev[0]["change_type"] == "novos_itens" and "Beltrano Exemplo Junior" in ev[0]["diff"]
    assert any(i["kind"] == "registro_oficial" for i in core.list_items(pid))
