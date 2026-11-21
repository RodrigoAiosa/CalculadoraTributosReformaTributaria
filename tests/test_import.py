"""Testes da importação de .xlsx (motor simulado)."""

import io
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from api import index  # noqa: E402

client = TestClient(index.app)
CAMPOS = {"dhFatoGerador": "2027-03-10T10:00:00-03:00", "municipio": "4314902", "uf": "RS", "cbs": "8.8", "ibsEstadual": "0.05", "ibsMunicipal": "0.05"}
CAB = ["Tipo", "NCM / NBS", "Descrição", "CST", "cClassTrib", "Base de cálculo", "Quantidade", "Unidade"]


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ENGINE_REGULAR_URL", "http://regular.test")
    monkeypatch.setenv("ENGINE_SIMPLES_URL", "http://simples.test")


def xlsx(linhas, cabecalho=CAB):
    wb = Workbook()
    ws = wb.active
    if cabecalho:
        ws.append(cabecalho)
    for l in linhas:
        ws.append(l)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def enviar(conteudo, campos=CAMPOS):
    return client.post("/api/regime-geral/importar", data=campos, files={"arquivo": ("p.xlsx", conteudo, "application/octet-stream")})


def motor(monkeypatch, estado=lambda it: "CALCULO_SIMULADO"):
    corpos = []

    def handler(req: httpx.Request) -> httpx.Response:
        corpo = json.loads(req.content)
        corpos.append(corpo)
        objs = []
        for it in corpo["itens"]:
            est = estado(it)
            o = {"nObj": it["numero"], "estadoItem": est}
            if est == "CALCULO_SIMULADO":
                o["tribCalc"] = {"IBSCBS": {"gIBSCBS": {"vBC": f"{it['baseCalculo']:.2f}", "gCBS": {"vCBS": "1.00"}, "vIBS": "0.50"}}}
            else:
                o["erros"] = [{"code": "REG-011", "title": "NCM não vinculada", "detail": "x"}]
            objs.append(o)
        return httpx.Response(200, json={"objetos": objs}, headers={"x-warning-dados-simulados": "6"})

    monkeypatch.setattr(index, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    return corpos


def test_importa_e_totaliza(monkeypatch):
    corpos = motor(monkeypatch)
    r = enviar(xlsx([["NCM", 2013000, "Carne", 200, 200035, 80, 2, "KG"], ["nbs", "122011200", "Educação", "200", "200028", "1200,50", None, None]]))
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["resumo"] == {"produtos": 2, "blocos": 1, "porEstado": {"CALCULO_SIMULADO": 2}, "comErro": 0}
    assert j["totais"] == {"vBC": "1280.50", "vCBS": "2.00", "vIBS": "1.00", "vIS": "0.00"}
    assert j["avisos"] == ["6"]
    item = corpos[0]["itens"][0]
    assert item["ncm"] == "02013000"  # zeros à esquerda recompostos
    assert item["aliquotasNominais"] == {"cbs": 8.8, "ibsEstadual": 0.05, "ibsMunicipal": 0.05}
    assert corpos[0]["itens"][1]["nbs"] == "122011200" and "ncm" not in corpos[0]["itens"][1]
    assert [i["linha"] for i in j["itens"]] == [2, 3]


def test_divide_em_blocos_de_100(monkeypatch):
    corpos = motor(monkeypatch)
    r = enviar(xlsx([["NCM", "22030000", "x", "000", "000001", 10.0, None, None]] * 250))
    j = r.json()
    assert r.status_code == 200 and j["resumo"]["blocos"] == 3 and j["resumo"]["produtos"] == 250
    assert sorted(len(c["itens"]) for c in corpos) == [50, 100, 100]
    assert all([i["numero"] for i in c["itens"]] == list(range(1, len(c["itens"]) + 1)) for c in corpos)  # renumerado por bloco
    assert [i["linha"] for i in j["itens"]] == list(range(2, 252))  # ordem original preservada


def test_itens_com_erro_do_motor_ficam_fora_dos_totais(monkeypatch):
    motor(monkeypatch, estado=lambda it: "INCONSISTENCIA_ENTRADA" if it["numero"] == 2 else "CALCULO_SIMULADO")
    r = enviar(xlsx([["NCM", "22030000", "a", "000", "000001", 100, None, None], ["NCM", "02013000", "b", "200", "200035", 80, None, None]]))
    j = r.json()
    assert j["resumo"]["comErro"] == 1 and j["totais"]["vBC"] == "100.00"
    assert j["itens"][1]["erros"][0]["code"] == "REG-011"


@pytest.mark.parametrize("faltando", ["Tipo", "NCM / NBS", "CST", "cClassTrib", "Base de cálculo"])
def test_coluna_obrigatoria_ausente(monkeypatch, faltando):
    motor(monkeypatch)
    cab = [c for c in CAB if c != faltando]
    r = enviar(xlsx([], cabecalho=cab))
    assert r.status_code == 422 and faltando in r.json()["detail"]


def test_cabecalho_tolera_variacoes(monkeypatch):
    motor(monkeypatch)
    r = enviar(xlsx([["NCM", "22030000", "000", "000001", 10]], cabecalho=["tipo", "ncm/nbs", "CST", "CCLASSTRIB", "Base de Calculo"]))
    assert r.status_code == 200


def test_linhas_invalidas_listadas_sem_chamar_motor(monkeypatch):
    corpos = motor(monkeypatch)
    r = enviar(xlsx([["XYZ", "1", "d", "20", "1", -5, None, None], ["NCM", "22030000", "ok", "000", "000001", 10, None, None], ["NBS", "123", "d", "200", "200028", "abc", None, None]]))
    d = r.json()["detail"]
    assert r.status_code == 422 and len(d["linhas"]) == 2
    assert d["linhas"][0].startswith("Linha 2:") and "Tipo deve ser NCM ou NBS" in d["linhas"][0]
    assert d["linhas"][1].startswith("Linha 4:") and "NBS deve ter 9 dígitos" in d["linhas"][1] and corpos == []


def test_arquivo_que_nao_e_xlsx():
    r = enviar(b"isto nao e um xlsx")
    assert r.status_code == 422 and ".xlsx" in r.json()["detail"]


def test_planilha_sem_produtos(monkeypatch):
    motor(monkeypatch)
    assert enviar(xlsx([])).status_code == 422


def test_linhas_em_branco_sao_ignoradas(monkeypatch):
    motor(monkeypatch)
    r = enviar(xlsx([["NCM", "22030000", "a", "000", "000001", 10], [None] * 6, ["NCM", "22030000", "b", "000", "000001", 20]]))
    assert r.status_code == 200 and r.json()["resumo"]["produtos"] == 2


def test_aliquotas_em_branco_nao_sao_enviadas(monkeypatch):
    corpos = motor(monkeypatch)
    campos = {**CAMPOS, "cbs": "", "ibsEstadual": "", "ibsMunicipal": ""}
    assert enviar(xlsx([["NCM", "22030000", "a", "000", "000001", 10]]), campos).status_code == 200
    assert "aliquotasNominais" not in corpos[0]["itens"][0]


def test_modelo_baixavel_e_reimportavel(monkeypatch):
    motor(monkeypatch)
    r = client.get("/api/modelo.xlsx")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    ws = load_workbook(io.BytesIO(r.content)).active
    assert [c.value for c in ws[1]][:2] == ["Tipo", "NCM / NBS"]
    assert enviar(r.content).status_code == 200  # o modelo passa na própria validação
