"""Importação de conversas exportadas pelo próprio WhatsApp ("Exportar conversa", com ou sem mídia).

Aceita o .txt da conversa ou o .zip com o .txt e os anexos, nos formatos do Android e do iPhone (português ou
inglês). A conversa vira um item com as mensagens em ordem e datas; cada anexo vira um item próprio com a data e o
autor da mensagem (fotos com EXIF, áudios transcritos se a transcrição estiver ativa). O arquivo original fica
preservado como evidência.

Use só com conversas suas ou recebidas legitimamente: elas contêm dados pessoais de terceiros.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

_ANDROID = re.compile(r"^(\d{1,2}/\d{1,2}/\d{2,4}),? (\d{1,2}:\d{2})(?::\d{2})?(?:\s?[ap]\.?m\.?)? - (.*)$", re.I)
_IOS = re.compile(r"^‎?\[(\d{1,2}/\d{1,2}/\d{2,4}),? (\d{1,2}:\d{2}(?::\d{2})?)(?:\s?[AP]M)?\] (.*)$")
_ANEXO = [re.compile(r"^‎?(.+?\.\w{2,5}) \((?:arquivo anexado|file attached)\)\s*$", re.I),
          re.compile(r"^‎?<(?:anexado|attached):\s*(.+?)>\s*$", re.I)]
_OMITIDO = re.compile(r"^<(?:Mídia oculta|Media omitted|arquivo de mídia oculto)>$", re.I)


@dataclass
class Mensagem:
    data: str          # AAAA-MM-DD
    hora: str          # HH:MM
    autor: str         # vazio = aviso do sistema
    texto: str
    anexo: str | None = None
    extras: list[str] = field(default_factory=list)


def _data_iso(d: str) -> str:
    dia, mes, ano = (int(x) for x in d.split("/"))
    if ano < 100:
        ano += 2000
    if mes > 12 and dia <= 12:  # formato americano (mês/dia)
        dia, mes = mes, dia
    return f"{ano:04d}-{mes:02d}-{dia:02d}"


def parece_conversa(texto: str) -> bool:
    linhas = [ln for ln in texto.splitlines()[:30] if ln.strip()]
    ok = sum(1 for ln in linhas if _ANDROID.match(ln.replace(" ", " ")) or _IOS.match(ln.replace(" ", " ")))
    return len(linhas) >= 2 and ok >= max(2, len(linhas) // 2)


def ler(texto: str) -> list[Mensagem]:
    msgs: list[Mensagem] = []
    for bruta in texto.splitlines():
        ln = bruta.replace(" ", " ").rstrip()
        m = _ANDROID.match(ln) or _IOS.match(ln)
        if not m:
            if msgs and ln.strip():
                msgs[-1].texto += "\n" + ln
            continue
        data, hora, resto = m.group(1), m.group(2)[:5], m.group(3)
        autor, corpo = ("", resto)
        if ": " in resto:
            autor, corpo = resto.split(": ", 1)
        anexo = None
        for rx in _ANEXO:
            a = rx.match(corpo)
            if a:
                anexo = a.group(1).strip()
                break
        msgs.append(Mensagem(_data_iso(data), hora.zfill(5), autor.strip().lstrip("‎"), corpo.strip(), anexo))
    return msgs


def _conteudo(data: bytes, nome: str) -> tuple[str, dict[str, bytes]]:
    """(texto da conversa, {nome do anexo: bytes})."""
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            txts = [n for n in z.namelist() if n.lower().endswith(".txt")]
            if not txts:
                raise ValueError("ZIP sem o arquivo .txt da conversa.")
            principal = next((n for n in txts if "chat" in n.lower() or "conversa" in n.lower()), txts[0])
            texto = z.read(principal).decode("utf-8-sig", errors="replace")
            midias = {Path(n).name: z.read(n) for n in z.namelist()
                      if not n.endswith("/") and n != principal and z.getinfo(n).file_size < 200 * 2 ** 20}
            return texto, midias
    return data.decode("utf-8-sig", errors="replace"), {}


def eh_exportacao(data: bytes, nome: str) -> bool:
    try:
        if data[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                txts = [n for n in z.namelist() if n.lower().endswith(".txt")]
                return bool(txts) and parece_conversa(z.read(txts[0])[:20000].decode("utf-8-sig", errors="replace"))
        return Path(nome).suffix.lower() == ".txt" and parece_conversa(data[:20000].decode("utf-8-sig", errors="replace"))
    except (zipfile.BadZipFile, KeyError, ValueError):
        return False


def importar(pid: int, data: bytes, nome: str, source_url: str | None = None) -> int | None:
    from .. import audit, core, evidence
    from ..db import get_db
    texto, midias = _conteudo(data, nome)
    msgs = ler(texto)
    if not msgs:
        raise ValueError("Não reconheci mensagens do WhatsApp neste arquivo.")
    participantes = sorted({m.autor for m in msgs if m.autor})
    sha = evidence.store(data, original_name=nome, source_url=source_url, collector="whatsapp",
                         notes="Exportação de conversa do WhatsApp (original)")
    linhas = [f"[{m.data} {m.hora}] {m.autor or '(aviso)'}: {m.texto}" for m in msgs]
    titulo = "Conversa do WhatsApp: " + (", ".join(participantes[:4]) + ("…" if len(participantes) > 4 else ""))
    meta = {"whatsapp": {"participantes": participantes, "mensagens": len(msgs), "inicio": f"{msgs[0].data} {msgs[0].hora}",
                         "fim": f"{msgs[-1].data} {msgs[-1].hora}", "anexos_citados": sum(1 for m in msgs if m.anexo),
                         "anexos_no_arquivo": len(midias),
                         "por_autor": {p: sum(1 for m in msgs if m.autor == p) for p in participantes}},
            "arquivo": nome}
    iid = core.add_item(pid, kind="conversa", title=titulo[:200], url=source_url, text="\n".join(linhas), metadata=meta,
                        evidence_sha256=sha, published_at=msgs[0].data, source_reliability="F", collector="whatsapp")
    if iid is None:
        return None
    for p in participantes:  # quem fala na conversa: telefone (contato não salvo) ou nome
        tipo = "telefone" if re.fullmatch(r"[+\d\s()-]{8,}", p) else "pessoa"
        core._add_mention(pid, iid, tipo, p, p.lower(), -1, "participante da conversa", True)
    db = get_db()
    importados = 0
    for m in msgs:
        if not m.anexo or m.anexo not in midias:
            continue
        aid = core.ingest_file(pid, midias[m.anexo], m.anexo, note=f"Anexo da conversa #{iid} enviado por {m.autor} "
                               f"em {m.data} {m.hora}")
        if aid:
            importados += 1
            it = core.get_item(aid)
            db.execute("UPDATE items SET published_at=?, metadata=? WHERE id=?",
                       (f"{m.data}T{m.hora}:00", __import__("json").dumps(
                           {**it["metadata"], "conversa": iid, "autor_mensagem": m.autor, "legenda": m.texto[:300]},
                           ensure_ascii=False), aid))
    audit.log("whatsapp_importado", project_id=pid, target=str(iid),
              details={"mensagens": len(msgs), "participantes": len(participantes), "anexos": importados})
    return iid
