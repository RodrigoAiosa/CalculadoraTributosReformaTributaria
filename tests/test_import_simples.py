"""Testes da importação de .xlsx do Simples Nacional (motor simulado)."""

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
ALIQ = json.dumps({"anexoI": {"pCBSSN": "1.1612", "pIBSSN": "0.0129"}})
CAMPOS = {"data": "2027-03-15", "tpDoc": "55", "CRT": "1", "aliquotasEfetivas": ALIQ}
CAB = ["Tipo", "NCM / NBS", "Descrição", "cAtivSN", "cClassTrib", "Base de cálculo", "Valor IS"]


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
    return client.post("/api/simples-nacional/importar", data=campos, files={"arquivo": ("p.xlsx", conteudo, "application/octet-stream")})


def motor(monkeypatch, estado=lambda it: "CALCULADO"):
    corpos = []

    def handler(req: httpx.Request) -> httpx.Response:
        corpo = json.loads(req.content)
        corpos.append(corpo)
        objs = []
        for it in corpo["itens"]:
            est = estado(it)
            o = {"nObj": it["numero"], "estadoItem": est}
            if est == "CALCULADO":
                base = float(it["baseCalculo"])
                o["tribCalc"] = {"vBC": it["baseCalculo"], "vCBSSN": f"{base * 0.011612:.2f}", "vIBSSN": f"{base * 0.000129:.2f}",
                                 "IS": {"valor": it["valorImpostoSeletivo"]}, "totalItem": f"{base * 0.011741:.2f}"}
            else:
                o["erros"] = [{"code": "VSN-010", "title": "Erro", "detail": "x"}]
            objs.append(o)
        return httpx.Response(200, json={"objetos": objs, "resolucaoAliquotas": {"itens": [{"numero": 1, "avisos": ["aviso x"]}]}})

    monkeypatch.setattr(index, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    return corpos


def test_importa_formata_campos_como_texto_e_totaliza(monkeypatch):
    corpos = motor(monkeypatch)
    r = enviar(xlsx([["NCM", 22030000, "Cerveja", 1, 1, 10000, None], ["NCM", "02013000", "Carne", "02", "000001", "2500,5", 0]]))
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["resumo"] == {"produtos": 2, "blocos": 1, "porEstado": {"CALCULADO": 2}, "comErro": 0}
    assert j["totais"]["vBC"] == "12500.50" and j["totais"]["vCBS"] == "145.16"
    c = corpos[0]
    assert (c["tpDoc"], c["CRT"], c["versao"], c["data"]) == ("55", "1", "3.0.0", "2027-03-15") and len(c["id"]) == 32
    assert c["aliquotasEfetivas"] == json.loads(ALIQ)
    i1, i2 = c["itens"]
    assert i1 == {"numero": 1, "cAtivSN": "01", "cClassTrib": "000001", "baseCalculo": "10000.00", "valorImpostoSeletivo": "0.00", "ncm": "22030000"}
    assert i2["ncm"] == "02013000" and i2["baseCalculo"] == "2500.50"
    assert j["avisos"] == ["aviso x"]


def test_blocos_de_50(monkeypatch):
    corpos = motor(monkeypatch)
    r = enviar(xlsx([["NCM", "22030000", "x", "01", "000001", 10.0, None]] * 230))
    assert r.json()["resumo"]["blocos"] == 5 and sorted(len(c["itens"]) for c in corpos) == [30, 50, 50, 50, 50]


def test_item_com_erro_fica_fora_dos_totais(monkeypatch):
    motor(monkeypatch, estado=lambda it: "NAO_CALCULADO" if it["numero"] == 2 else "CALCULADO")
    j = enviar(xlsx([["NCM", "22030000", "a", "01", "000001", 100, None], ["NCM", "22030000", "b", "09", "000001", 50, None]])).json()
    assert j["resumo"]["comErro"] == 1 and j["totais"]["vBC"] == "100.00"


@pytest.mark.parametrize("faltando", ["Tipo", "NCM / NBS", "cAtivSN", "cClassTrib", "Base de cálculo"])
def test_coluna_obrigatoria_ausente(monkeypatch, faltando):
    motor(monkeypatch)
    r = enviar(xlsx([], cabecalho=[c for c in CAB if c != faltando]))
    assert r.status_code == 422 and faltando in r.json()["detail"]


def test_valor_is_e_descricao_sao_opcionais(monkeypatch):
    motor(monkeypatch)
    r = enviar(xlsx([["NCM", "22030000", "01", "000001", 100]], cabecalho=["Tipo", "NCM / NBS", "cAtivSN", "cClassTrib", "Base de cálculo"]))
    assert r.status_code == 200


def test_linhas_invalidas(monkeypatch):
    corpos = motor(monkeypatch)
    r = enviar(xlsx([["NCM", "22030000", "ok", "01", "000001", 10, None], ["NCM", "22030000", "x", "ABC", "000001", 10, None], ["NCM", "22030000", "y", "01", "000001", 10, -1]]))
    d = r.json()["detail"]
    assert r.status_code == 422 and len(d["linhas"]) == 2 and corpos == []
    assert "cAtivSN" in d["linhas"][0] and d["linhas"][0].startswith("Linha 3:")
    assert "Valor IS" in d["linhas"][1]


def test_nfse_nao_e_aceita_na_importacao(monkeypatch):
    motor(monkeypatch)
    r = enviar(xlsx([["NCM", "22030000", "a", "01", "000001", 10, None]]), {**CAMPOS, "tpDoc": "91"})
    assert r.status_code == 422 and "NFS-e" in r.json()["detail"]


@pytest.mark.parametrize("aliq", ["", "isto nao e json", "[]", "{}"])
def test_aliquotas_invalidas(monkeypatch, aliq):
    corpos = motor(monkeypatch)
    r = enviar(xlsx([["NCM", "22030000", "a", "01", "000001", 10, None]]), {**CAMPOS, "aliquotasEfetivas": aliq})
    assert r.status_code == 422 and corpos == []


def test_modelo_do_simples_reimportavel(monkeypatch):
    motor(monkeypatch)
    r = client.get("/api/modelo-simples.xlsx")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert [c.value for c in load_workbook(io.BytesIO(r.content)).active[1]][3] == "cAtivSN"
    assert enviar(r.content).status_code == 200
