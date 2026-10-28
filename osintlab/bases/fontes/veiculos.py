"""Veículos: preço e ficha pela Tabela Fipe (online) e frota oficial da Senatran por município, marca/modelo e ano
(base local). Nenhuma das duas fontes recebe ou devolve placa, chassi ou proprietário."""
from __future__ import annotations

import json
import re
import time
import unicodedata
from datetime import date

from ... import http
from ..catalog import Fonte, SearchHit, registrar

FIPE = "https://fipe.parallelum.com.br/api/v2"
TIPOS_FIPE = ("cars", "motorcycles", "trucks")
NOME_TIPO = {"cars": "carro", "motorcycles": "moto", "trucks": "caminhão/ônibus"}
# apelidos usuais → como a Fipe escreve a marca
MARCAS = {"vw": "volkswagen", "volks": "volkswagen", "gm": "chevrolet", "chevy": "chevrolet", "mercedes": "mercedes-benz",
          "mb": "mercedes-benz", "harley": "harley-davidson", "land rover": "land rover", "caoa chery": "caoa chery"}
MARCAS_CONHECIDAS = (
    "ford", "chevrolet", "volkswagen", "fiat", "toyota", "honda", "hyundai", "renault", "nissan", "jeep", "peugeot",
    "citroen", "mitsubishi", "kia", "bmw", "mercedes-benz", "audi", "volvo", "land rover", "chery", "caoa chery", "byd",
    "gwm", "jac", "ram", "dodge", "suzuki", "yamaha", "kawasaki", "harley-davidson", "scania", "iveco", "troller", "lifan",
    "subaru", "porsche", "mini", "jaguar", "chrysler", "agrale", "dafra", "shineray", "triumph", "ducati", "volvo",
    "man", "daf", "foton", "jinbei", "effa", "haojue", "royal enfield", "bajaj")


def _fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


def parece_veiculo(texto: str) -> bool:
    """Marca conhecida + mais alguma palavra (modelo e/ou ano). Ex.: 'Chevrolet Onix 2020', 'Honda CG 160'."""
    t = _fold(texto)
    palavras = re.findall(r"[a-z0-9-]+", t)
    if len(palavras) < 2:
        return False
    junto = " ".join(palavras)
    if re.search(r"(^| )(company|corporation|corp|inc|ltda|eireli|sa|s-a|me|epp|do brasil|motors|motor company|comercio|"
                 r"industria|concessionaria|veiculos|automoveis|distribuidora|holding|group|grupo)( |$)", junto):
        return False  # nome de empresa (ex.: 'Ford Motor Company', 'Fiat Automóveis S.A.') segue para a pesquisa de organização
    return any(re.search(rf"(^| ){re.escape(m)}( |$)", junto) for m in (*MARCAS_CONHECIDAS, *MARCAS))


def interpretar(texto: str) -> dict:
    t = _fold(texto)
    anos = [int(a) for a in re.findall(r"\b(19[5-9]\d|20\d\d)\b", t) if 1950 <= int(a) <= date.today().year + 1]
    resto = re.sub(r"\b(19[5-9]\d|20\d\d)\b", " ", t)
    marca = None
    for apelido, nome in sorted(MARCAS.items(), key=lambda x: -len(x[0])):
        if re.search(rf"(^| ){re.escape(apelido)}( |$)", resto):
            marca, resto = nome, re.sub(rf"(^| ){re.escape(apelido)}( |$)", " ", resto)
            break
    if not marca:
        for m in sorted(set(MARCAS_CONHECIDAS), key=len, reverse=True):
            if re.search(rf"(^| ){re.escape(m)}( |$)", resto):
                marca, resto = m, re.sub(rf"(^| ){re.escape(m)}( |$)", " ", resto)
                break
    return {"marca": marca, "modelo": re.sub(r"\s+", " ", resto).strip(), "ano": anos[0] if anos else None}


# ------------------------------------------------------------------ cache local (para gastar poucas consultas)
def _cache_get(chave: str, dias: float):
    from ...config import get_settings
    p = get_settings().data_dir / "cache" / "fipe" / (re.sub(r"[^a-z0-9_-]", "_", chave) + ".json")
    try:
        if time.time() - p.stat().st_mtime < dias * 86400:
            return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return None


def _cache_set(chave: str, valor) -> None:
    from ...config import get_settings
    p = get_settings().data_dir / "cache" / "fipe" / (re.sub(r"[^a-z0-9_-]", "_", chave) + ".json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(valor, ensure_ascii=False), encoding="utf-8")


def _get(caminho: str, chave: str | None = None, dias: float = 7):
    if chave:
        v = _cache_get(chave, dias)
        if v is not None:
            return v
    data, _ = http.get_json(FIPE + caminho, collector="consulta:fipe", preserve=False, retries=2)
    if chave:
        _cache_set(chave, data)
    return data


@registrar
class Fipe(Fonte):
    id = "fipe"
    nome = "Tabela Fipe — preço médio e ficha de veículos"
    organizacao = "Fundação Instituto de Pesquisas Econômicas (Fipe), via API pública Parallelum"
    pais = "Brasil"
    descricao = ("Preço médio de mercado, código Fipe, combustível e versões por marca, modelo e ano-modelo. "
                 "Consulta por veículo (ex.: 'Chevrolet Onix 2020'), nunca por placa.")
    tema = "veiculos"
    categoria = "A"
    url_oficial = "https://veiculos.fipe.org.br/"
    documentacao = "https://deividfortuna.github.io/fipe/v2/"
    formato = "JSON (REST)"
    licenca = "Dados da Fipe (citar a fonte); API Parallelum gratuita com limite diário"
    restricoes = ("A Fipe não oferece API própria aberta; usa-se o espelho público e documentado da Parallelum "
                  "(500 consultas/dia sem chave). Respostas ficam em cache local por 7 dias.")
    periodicidade = "Mensal (tabela de referência)"
    situacao = "testada"
    prioridade = 2
    entradas = ["veiculo"]
    online = True

    def search_online(self, ctx, query, kind, limit=25):
        from rapidfuzz import fuzz
        q = interpretar(query)
        if not q["marca"]:
            return []
        hits = []
        for tipo in TIPOS_FIPE:
            marcas = _get(f"/{tipo}/brands", f"marcas_{tipo}")
            marca = next((m for m in marcas if _fold(m["name"]) == q["marca"] or q["marca"] in _fold(m["name"]).split(" - ")
                          or _fold(m["name"]).endswith(q["marca"]) or _fold(m["name"]).startswith(q["marca"])), None)
            if not marca:
                continue
            modelos = _get(f"/{tipo}/brands/{marca['code']}/models", f"modelos_{tipo}_{marca['code']}")
            if q["modelo"]:
                cand = sorted(((fuzz.token_set_ratio(q["modelo"], _fold(m["name"])), m) for m in modelos),
                              key=lambda x: -x[0])
                cand = [m for s, m in cand if s >= 70][:3]
            else:
                cand = modelos[:3]
            for m in cand:
                anos = _get(f"/{tipo}/brands/{marca['code']}/models/{m['code']}/years",
                            f"anos_{tipo}_{marca['code']}_{m['code']}")
                if q["ano"]:
                    anos = [a for a in anos if a["name"].startswith(str(q["ano"]))]
                    if not anos:
                        continue
                a = anos[0]
                v = _get(f"/{tipo}/brands/{marca['code']}/models/{m['code']}/years/{a['code']}",
                         f"preco_{tipo}_{marca['code']}_{m['code']}_{a['code']}", dias=3)
                hits.append(SearchHit(
                    fonte=self.id, registro_id=f"{v.get('codeFipe')}:{v.get('modelYear')}:{v.get('fuelAcronym')}",
                    titulo=f"{v.get('brand')} {v.get('model')} {v.get('modelYear')}", tipo=f"veículo ({NOME_TIPO[tipo]})",
                    resumo=f"{v.get('price')} · {v.get('fuel')} · código Fipe {v.get('codeFipe')} · referência "
                           f"{v.get('referenceMonth')}" + (f" · outros anos: {', '.join(x['name'] for x in anos[1:4])}"
                                                          if len(anos) > 1 else ""),
                    url="https://veiculos.fipe.org.br/", identificadores=[f"fipe:{v.get('codeFipe')}"],
                    nomes=[f"{v.get('brand')} {v.get('model')}"], atributos=v, origem="online",
                    data_referencia=v.get("referenceMonth")))
                if len(hits) >= limit:
                    return hits
            if hits:
                break
        return hits

    def test_connectivity(self, ctx):
        refs = _get("/references")
        return {"ok": True, "detalhe": f"tabela de referência atual: {refs[0]['month']}"}


# ------------------------------------------------------------------ frota oficial (Senatran / Renavam, agregada)
CKAN_FROTA = "https://dados.transportes.gov.br/api/3/action/package_show?id=12686da0-3d71-4499-b432-d270f785c907"
SIGLAS = {"ACRE": "AC", "ALAGOAS": "AL", "AMAPA": "AP", "AMAZONAS": "AM", "BAHIA": "BA", "CEARA": "CE",
          "DISTRITO FEDERAL": "DF", "ESPIRITO SANTO": "ES", "GOIAS": "GO", "MARANHAO": "MA", "MATO GROSSO": "MT",
          "MATO GROSSO DO SUL": "MS", "MINAS GERAIS": "MG", "PARA": "PA", "PARAIBA": "PB", "PARANA": "PR",
          "PERNAMBUCO": "PE", "PIAUI": "PI", "RIO DE JANEIRO": "RJ", "RIO GRANDE DO NORTE": "RN",
          "RIO GRANDE DO SUL": "RS", "RONDONIA": "RO", "RORAIMA": "RR", "SANTA CATARINA": "SC", "SAO PAULO": "SP",
          "SERGIPE": "SE", "TOCANTINS": "TO"}
# como a Senatran abrevia as marcas em "Marca Modelo" (ex.: CHEV/ONIX, VW/GOL, I/FORD KA, M.BENZ/...)
MARCA_FROTA = {"chevrolet": ["chev", "gm"], "volkswagen": ["vw"], "mercedes-benz": ["m.benz", "mbenz", "mercedes"],
               "harley-davidson": ["h.davidson", "harley"], "land rover": ["lr", "land rover"], "caoa chery": ["caoachery"],
               "citroen": ["citroen"], "peugeot": ["peugeot"], "mitsubishi": ["mmc", "mitsubishi"]}
FROTA_DDL = """
CREATE TABLE IF NOT EXISTS municipios (id INTEGER PRIMARY KEY, uf TEXT, nome TEXT, nome_norm TEXT);
CREATE TABLE IF NOT EXISTS modelos (id INTEGER PRIMARY KEY, nome TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS frota (modelo_id INTEGER, municipio_id INTEGER, ano INTEGER, qtd INTEGER,
                                  PRIMARY KEY (modelo_id, municipio_id, ano)) WITHOUT ROWID;
CREATE VIRTUAL TABLE IF NOT EXISTS modelos_fts USING fts5(nome, content='modelos', content_rowid='id',
                                                          tokenize="unicode61 remove_diacritics 2");
"""


def _recurso_frota_mais_recente() -> dict:
    data, _ = http.get_json(CKAN_FROTA, collector="sincronizacao:frota", preserve=False, retries=4)
    recursos = [r for r in data["result"]["resources"]
                if "marca_e_modelo_ano" in (r.get("url") or "").lower() and r.get("url", "").lower().endswith(".zip")]
    if not recursos:
        raise RuntimeError("Nenhum arquivo 'frota por UF, município, marca e modelo e ano' no catálogo.")
    return max(recursos, key=lambda r: (r.get("created") or "", r.get("url")))


@registrar
class FrotaSenatran(Fonte):
    id = "frota_senatran"
    nome = "Frota de veículos — Senatran (por município, marca/modelo e ano)"
    organizacao = "Secretaria Nacional de Trânsito (Senatran) — Renavam, dados agregados"
    pais = "Brasil"
    descricao = ("Quantidade de veículos registrados por UF, município, marca/modelo e ano de fabricação, publicada "
                 "mensalmente. Responde 'quantos veículos deste modelo existem nesta cidade?', útil para avaliar se uma "
                 "coincidência é rara ou comum. Não contém placa, chassi nem proprietário.")
    tema = "veiculos"
    categoria = "B"
    url_oficial = "https://dados.transportes.gov.br/dataset/registro-nacional-de-veiculos-automotores-renavam"
    documentacao = "https://www.gov.br/transportes/pt-br/assuntos/transito/conteudo-Senatran/frota-de-veiculos-2026"
    formato = "ZIP com TXT (UF;Município;Marca Modelo;Ano;Qtd.)"
    licenca = "Domínio público (dados.transportes.gov.br)"
    restricoes = "Arquivo mensal de ~137 MB (1,2 GB descompactado). Use a opção 'ufs' para importar só alguns estados."
    periodicidade = "Mensal"
    situacao = "testada"
    prioridade = 2
    entradas = ["veiculo"]
    sincronizavel = True
    intervalo_horas = 24 * 30
    tamanho_estimado = "~450 MB (Brasil inteiro); ~25 MB por estado médio"
    opcoes_padrao = {"ufs": []}

    def remote_info(self, ctx):
        from ..catalog import RemoteInfo
        r = _recurso_frota_mais_recente()
        nome = r["url"].rsplit("/", 1)[-1]
        mes = re.search(r"ano_([a-z]+_\d{4})\.zip$", nome.lower())
        return RemoteInfo(version=(mes.group(1) if mes else nome), updated_at=r.get("last_modified") or r.get("created"),
                          size=r.get("size"), extra={"url": r["url"], "arquivo": nome})

    def full_import(self, ctx, conn, remote) -> dict:
        import io
        import zipfile

        from ..download import fetch_file
        from ..store import set_meta
        ufs = {u.upper() for u in ({**self.opcoes_padrao, **(ctx.options or {})}.get("ufs") or [])}
        conn.executescript(FROTA_DDL)
        dest = ctx.workdir() / "frota.zip"
        d = fetch_file(remote.extra["url"], dest, cancel=ctx.cancel, expected_size=remote.size, timeout=300)
        ctx.add_file(remote.extra["url"], d.sha256, d.size, dest.name)
        munis: dict = {}
        modelos: dict = {}
        lote, n, veiculos = [], 0, 0
        with zipfile.ZipFile(dest) as z:
            info = max(z.infolist(), key=lambda i: i.file_size)
            with z.open(info) as f:
                texto = io.TextIOWrapper(f, encoding="utf-8", errors="replace")
                next(texto)  # cabeçalho
                for i, ln in enumerate(texto):
                    if ctx.cancel.is_set():
                        raise RuntimeError("Cancelado.")
                    p = ln.rstrip("\r\n").split(";")
                    if len(p) != 5:
                        continue
                    uf = SIGLAS.get(_fold(p[0]).upper().strip(), p[0][:2])
                    if ufs and uf not in ufs:
                        continue
                    mk = (uf, p[1])
                    mid = munis.get(mk)
                    if mid is None:
                        mid = munis[mk] = len(munis) + 1
                    md = modelos.get(p[2])
                    if md is None:
                        md = modelos[p[2]] = len(modelos) + 1
                    ano = int(p[3]) if p[3].strip().isdigit() else 0
                    qtd = int(float(p[4] or 0))
                    lote.append((md, mid, ano, qtd))
                    n += 1
                    veiculos += qtd
                    if len(lote) >= 200_000:
                        conn.executemany("INSERT OR REPLACE INTO frota VALUES (?,?,?,?)", lote)
                        lote.clear()
                        ctx.step(7, f"{n:,} linhas importadas".replace(",", "."))
        if lote:
            conn.executemany("INSERT OR REPLACE INTO frota VALUES (?,?,?,?)", lote)
        conn.executemany("INSERT INTO municipios VALUES (?,?,?,?)",
                         [(i, uf, nome, _fold(nome)) for (uf, nome), i in munis.items()])
        conn.executemany("INSERT INTO modelos VALUES (?,?)", [(i, nome) for nome, i in modelos.items()])
        conn.execute("INSERT INTO modelos_fts(modelos_fts) VALUES ('rebuild')")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_munis_nome ON municipios(nome_norm)")
        dest.unlink(missing_ok=True)
        set_meta(conn, tabela_principal="frota", schema_versao="frota-senatran-1", arquivo=remote.extra["arquivo"],
                 ufs=",".join(sorted(ufs)) or "todas", veiculos=veiculos)
        return {"mensagem": f"{n:,} linhas, {len(modelos):,} modelos, {len(munis):,} municípios, "
                            f"{veiculos:,} veículos".replace(",", ".")}

    def validate(self, conn) -> dict:
        n = conn.execute("SELECT COUNT(*) FROM frota").fetchone()[0]
        m = conn.execute("SELECT COUNT(*) FROM modelos").fetchone()[0]
        neg = conn.execute("SELECT COUNT(*) FROM frota WHERE qtd < 0").fetchone()[0]
        return {"ok": n > 0 and m > 0 and neg == 0, "registros": n, "modelos": m}

    # ---------------------------------------------------------------- consulta
    def _municipio_na_consulta(self, conn, texto: str) -> tuple[list[int], str, str]:
        """Procura um município no fim da consulta ('onix 2020 porto alegre'). Devolve ids, nome e o resto."""
        palavras = texto.split()
        for k in range(min(5, len(palavras) - 1), 0, -1):
            cand = " ".join(palavras[-k:])
            rows = conn.execute("SELECT id, nome, uf FROM municipios WHERE nome_norm=?", (cand,)).fetchall()
            if rows:
                return [r[0] for r in rows], f"{rows[0][1]}/{rows[0][2]}" + (" e homônimos" if len(rows) > 1 else ""), \
                    " ".join(palavras[:-k])
        return [], "", texto

    def search_local(self, conn, query, kind, limit=25):
        q = interpretar(query)
        if not q["marca"]:
            return []
        resto = _fold(q["modelo"])
        munis, nome_muni, resto = self._municipio_na_consulta(conn, resto)
        tokens = [t for t in re.findall(r"[a-z0-9]+", resto) if len(t) >= 2][:4]
        marcas = [q["marca"], *MARCA_FROTA.get(q["marca"], [])]
        expr_marca = " OR ".join(f'"{m}"' for m in marcas)
        expr = f"({expr_marca})" + "".join(f' AND "{t}"*' for t in tokens)
        try:
            ids = [r[0] for r in conn.execute("SELECT rowid FROM modelos_fts WHERE modelos_fts MATCH ? LIMIT 400", (expr,))]
        except Exception:
            return []
        if not ids:
            return []
        marca_ids = ",".join(map(str, ids))
        filtro_ano = " AND f.ano=?" if q["ano"] else ""
        args = [q["ano"]] if q["ano"] else []
        # modelos mais numerosos que casam com a consulta
        tops = conn.execute(f"""SELECT m.id, m.nome, SUM(f.qtd) t FROM frota f JOIN modelos m ON m.id=f.modelo_id
                                WHERE f.modelo_id IN ({marca_ids}){filtro_ano} GROUP BY m.id ORDER BY t DESC LIMIT ?""",
                            [*args, min(limit, 6)]).fetchall()
        from ..store import get_meta
        ufs_base = get_meta(conn).get("ufs", "todas")
        abrangencia = "no Brasil" if ufs_base in ("todas", "", None) else f"em {ufs_base.replace(',', ', ')} (estados importados)"
        hits = []
        for mid, nome, total in tops:
            por_uf = conn.execute(f"""SELECT mu.uf, SUM(f.qtd) t FROM frota f JOIN municipios mu ON mu.id=f.municipio_id
                                      WHERE f.modelo_id=?{filtro_ano} GROUP BY mu.uf ORDER BY t DESC LIMIT 5""",
                                  [mid, *args]).fetchall()
            por_muni = conn.execute(f"""SELECT mu.nome, mu.uf, SUM(f.qtd) t FROM frota f JOIN municipios mu ON mu.id=f.municipio_id
                                        WHERE f.modelo_id=?{filtro_ano} GROUP BY mu.id ORDER BY t DESC LIMIT 5""",
                                    [mid, *args]).fetchall()
            no_muni = None
            if munis:
                no_muni = conn.execute(f"""SELECT SUM(qtd) FROM frota f WHERE f.modelo_id=? AND f.municipio_id IN
                                           ({','.join(map(str, munis))}){filtro_ano}""", [mid, *args]).fetchone()[0] or 0
            anos = conn.execute("SELECT ano, SUM(qtd) FROM frota WHERE modelo_id=? GROUP BY ano ORDER BY 2 DESC LIMIT 5",
                                (mid,)).fetchall()
            rotulo = f"{nome}" + (f", fabricação {q['ano']}" if q["ano"] else "")
            resumo = (f"{total:,} {abrangencia}".replace(",", ".")
                      + (f" · {no_muni:,} em {nome_muni}".replace(",", ".") if munis else "")
                      + " · mais comuns em " + ", ".join(f"{u} ({t:,})".replace(",", ".") for u, t in por_uf[:3]))
            hits.append(SearchHit(
                fonte=self.id, registro_id=f"modelo:{mid}:{q['ano'] or 'todos'}", titulo=rotulo, tipo="frota (agregado)",
                resumo=resumo, url=self.url_oficial, nomes=[nome],
                atributos={"total": total, "abrangencia": abrangencia, "no_municipio": no_muni, "municipio": nome_muni or None,
                           "por_uf": dict(por_uf), "top_municipios": [f"{a}/{b}: {c}" for a, b, c in por_muni],
                           "anos_mais_comuns": {str(a or 'sem informação'): c for a, c in anos}},
                origem="local"))
        return hits
