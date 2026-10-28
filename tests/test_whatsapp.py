"""Importação de conversa exportada do WhatsApp (Android e iPhone), com anexos."""
import io
import zipfile

from PIL import Image

from osintlab import core
from osintlab.db import get_db
from osintlab.ingest import whatsapp

ANDROID = """12/03/2024 09:15 - As mensagens e as chamadas são protegidas com a criptografia de ponta a ponta.
12/03/2024 09:16 - Fulano Sintetico: Bom dia, segue a foto da obra
12/03/2024 09:16 - Fulano Sintetico: IMG-20240312-WA0001.jpg (arquivo anexado)
12/03/2024 09:20 - +55 61 99876-5432: Recebi. O contrato da Construtora Horizonte Azul
é o 015/2024, CNPJ 11.222.333/0001-81
13/03/2024 18:02 - Fulano Sintetico: <Mídia oculta>
"""
IOS = """[12/03/2024, 09:16:05] Beltrana Sintetica: Olá
[12/03/2024, 09:17:44] Fulano Sintetico: ‎<anexado: 00000003-PHOTO-2024-03-12-09-17-44.jpg>
"""


def _jpg():
    b = io.BytesIO()
    Image.new("RGB", (40, 30), (10, 120, 200)).save(b, "JPEG")
    return b.getvalue()


def test_leitura_android_e_ios():
    m = whatsapp.ler(ANDROID)
    assert [x.autor for x in m] == ["", "Fulano Sintetico", "Fulano Sintetico", "+55 61 99876-5432", "Fulano Sintetico"]
    assert m[2].anexo == "IMG-20240312-WA0001.jpg" and m[3].data == "2024-03-12" and "015/2024" in m[3].texto
    i = whatsapp.ler(IOS)
    assert i[1].anexo == "00000003-PHOTO-2024-03-12-09-17-44.jpg" and i[0].hora == "09:16"
    assert whatsapp.parece_conversa(ANDROID) and not whatsapp.parece_conversa("texto comum\nsem datas")


def test_importa_zip_com_anexo(demo):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Conversa do WhatsApp com Fulano Sintetico.txt", ANDROID)
        z.writestr("IMG-20240312-WA0001.jpg", _jpg())
    iid = core.ingest_file(demo, buf.getvalue(), "WhatsApp Chat - Fulano.zip")
    it = core.get_item(iid)
    assert it["kind"] == "conversa" and it["metadata"]["whatsapp"]["mensagens"] == 5
    assert it["metadata"]["whatsapp"]["anexos_no_arquivo"] == 1 and "[2024-03-12 09:20]" in it["content_text"]
    anexo = get_db().one("SELECT * FROM items WHERE project_id=? AND title='IMG-20240312-WA0001.jpg'", (demo,))
    assert anexo["published_at"].startswith("2024-03-12T09:16") and '"autor_mensagem": "Fulano Sintetico"' in anexo["metadata"]
    tipos = {r["type"] for r in get_db().query("SELECT e.type FROM entities e JOIN mentions m ON m.entity_id=e.id "
                                               "WHERE m.item_id=?", (iid,))}
    assert {"pessoa", "telefone", "cnpj"} <= tipos
    assert core.ingest_file(demo, ANDROID.encode(), "conversa.txt") is None  # mesma conversa: não duplica
    assert core.get_item(core.ingest_file(demo, IOS.encode(), "_chat.txt"))["kind"] == "conversa"  # .txt solto (iPhone)
