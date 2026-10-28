"""Projeto de demonstração com dados 100% SINTÉTICOS.

Organizações, domínios (.example, reservado pela RFC 2606), pessoas e números são
fictícios. O CNPJ 11.222.333/0001-81 é o exemplo didático usual de validação.
"""
from __future__ import annotations

import io

from . import claims, core, evidence
from .analysis.correlation import correlate
from .ingest.web import extract_html

PAGES = [
    ("https://portal-noticias.example/cidade/ponte-vila-serena", "B", """<html lang="pt-BR"><head><title>Vila Serena contrata construtora para nova ponte</title>
<meta property="article:published_time" content="2024-01-15T10:00:00"></head><body><article>
<h1>Vila Serena contrata construtora para nova ponte</h1>
<p>A Prefeitura de Vila Serena assinou em 12/01/2024 contrato com a Construtora Horizonte Azul Ltda, CNPJ 11.222.333/0001-81,
para construir a ponte sobre o Rio Claro. O valor é de R$ 4,2 milhões e a previsão de entrega é 10 de março de 2024.</p>
<p>A obra fica nas coordenadas -15.799000, -47.864500. Contato da empresa: imprensa@horizonteazul.example.
Mais informações no blog <a href="https://blog-cidadao.example/ponte">Blog Cidadão</a>.</p></article></body></html>"""),
    ("https://outro-jornal.example/2024/01/ponte", "C", """<html lang="pt-BR"><head><title>Prefeitura fecha contrato de ponte</title>
<meta name="date" content="2024-01-16"></head><body><article>
<p>A Prefeitura de Vila Serena assinou em 12/01/2024 contrato com a Construtora Horizonte Azul Ltda, CNPJ 11.222.333/0001-81,
para construir a ponte sobre o Rio Claro. O valor é de R$ 4,2 milhões e a previsão de entrega é 10 de março de 2024.</p>
<p>Fonte: <a href="https://portal-noticias.example/cidade/ponte-vila-serena">Portal Notícias</a>.</p></article></body></html>"""),
    ("https://blog-cidadao.example/ponte", "D", """<html lang="pt-BR"><head><title>Ponte de Vila Serena: atraso?</title>
<meta property="article:published_time" content="2024-04-02"></head><body><article>
<p>Moradores relatam que a ponte não foi concluída em 10 de março de 2024, como prometido. A Construtora Horizonte Azul
mantém o site horizonteazul.example e o canteiro segue aberto perto de -15.799700, -47.865700.</p>
<p>Segundo <a href="https://outro-jornal.example/2024/01/ponte">Outro Jornal</a>, o contrato previa entrega em março.
Uma empresa de nome parecido, Construtora Horizonte Azul S.A., atua em outro estado — não confundir.</p></article></body></html>"""),
    ("https://transparencia.vilaserena.example/obras/ponte", "B", """<html lang="pt-BR"><head><title>Portal da Transparência – Obra da ponte (sintético)</title>
<meta name="date" content="2024-05-20"></head><body><main>
<p>Contrato 015/2024. Contratada: Construtora Horizonte Azul Ltda (CNPJ 11.222.333/0001-81). Valor: R$ 4.200.000,00.
Assinatura: 12/01/2024. Aditivo de prazo publicado em 01/03/2024: nova data de entrega 30/06/2024. Situação: em execução.</p>
</main></body></html>"""),
]

CSV_V1 = "contrato;empresa;valor;situacao\n015/2024;Construtora Horizonte Azul Ltda;4200000;em execução\n016/2024;Pavimenta Sul Ltda;850000;concluído\n017/2024;Iluminar Vila ME;120000;em execução\n"
CSV_V2 = "contrato;empresa;valor;situacao\n015/2024;Construtora Horizonte Azul Ltda;4650000;em execução\n016/2024;Pavimenta Sul Ltda;850000;concluído\n018/2024;Verde Praça Ltda;95000;licitação\n"


def synthetic_image() -> bytes:
    """Imagem sintética com EXIF de GPS (para demonstrar extração de metadados)."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (480, 320), (120, 170, 210))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 220, 480, 320], fill=(90, 140, 80))
    d.rectangle([60, 180, 420, 200], fill=(130, 130, 130))
    d.text((20, 20), "PONTE RIO CLARO - OBRA (imagem sintetica)", fill=(255, 255, 255))
    exif = Image.Exif()
    exif[0x010F] = "CameraFicticia"
    exif[0x0110] = "Modelo-X"
    exif[0x0132] = "2024:04:01 09:30:00"
    exif[0x0131] = "EditorDeImagens 2.0"
    gps = exif.get_ifd(0x8825)
    gps.update({1: "S", 2: (15.0, 47.0, 56.0), 3: "W", 4: (47.0, 51.0, 52.0)})
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def load_demo() -> int:
    pid = core.create_project(
        "Demonstração – Ponte de Vila Serena (sintético)",
        "Estudo didático de verificação de informações públicas sobre uma obra fictícia, com dados sintéticos.",
        "Verificar data de entrega e valor do contrato da ponte, comparando notícias, blog e portal de transparência.")
    for url, rel, html in PAGES:
        sha = evidence.store(html.encode(), original_name="index.html", source_url=url, mime="text/html",
                             collector="demonstracao")
        ex = extract_html(html, base_url=url)
        core.add_item(pid, kind="web", title=ex.title, url=url, text=ex.text, metadata=ex.metadata,
                      evidence_sha256=sha, source_reliability=rel, collector="demonstracao")
    core.ingest_file(pid, CSV_V1.encode(), "contratos_2024_v1.csv", note="planilha sintética – versão 1")
    core.ingest_file(pid, CSV_V2.encode(), "contratos_2024_v2.csv", note="planilha sintética – versão 2")
    core.ingest_file(pid, synthetic_image(), "foto_obra_sintetica.jpg", note="imagem sintética com EXIF")
    correlate(pid)
    items = {i["title"]: i["id"] for i in core.list_items(pid)}
    oficial = next(v for k, v in items.items() if k.startswith("Portal da Transparência"))
    claims.add(pid, "O contrato 015/2024 tem valor de R$ 4,2 milhões.", "documental", [oficial], confidence="B2",
               rationale="Consta no portal de transparência (sintético) e em duas notícias.")
    claims.add(pid, "A entrega foi adiada para 30/06/2024 por aditivo.", "documental", [oficial], confidence="B3",
               rationale="Somente a fonte oficial menciona o aditivo; as notícias de janeiro são anteriores.")
    claims.add(pid, "A empresa 'Horizonte Azul S.A.' é a mesma contratada.", "hipotese", [],
               rationale="Nome semelhante; não há identificador único que confirme. Provável homonímia.")
    return pid
