"""Testes da validação prévia da planilha (motor simulado)."""

import io
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from api import index  # noqa: E402

client = TestClient(index.app)
CAB = ["Tipo", "NCM / NBS", "Descrição", "CST", "cClassTrib", "Base de cálculo"]
D = "/api/calculadora/dados-abertos"


def xlsx(linhas):
    wb = Workbook()
    ws = wb.active
    ws.append(CAB)
    for l in linhas:
        ws.append(l)
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def motor(request: httpx.Request) -> httpx.Response:
    p = request.url.path
    q = dict(request.url.params)
    if p == f"{D}/ncm/vigentes":
        return httpx.Response(200, json=[{"codigo": "84713012", "descricao": "Notebook"}, {"codigo": "84713019", "descricao": "Outros computadores portáteis"}, {"codigo": "22030000", "descricao": "Cerveja de malte"}])
    if p == f"{D}/nbs/lista":
        return httpx.Response(200, json=[{"codigo": "115021000", "descricao": "Serviço"}])
    if p == f"{D}/situacoes-tributarias/cbs-ibs":
        return httpx.Response(200, json=[
            {"codigo": "000", "descricao": "Tributação integral", "classificacoesTributarias": [{"codigo": "000001", "descricao": "Situações tributadas integralmente"}]},
            {"codigo": "200", "descricao": "Alíquota reduzida", "classificacoesTributarias": [{"codigo": "200003", "descricao": "Reduzida"}, {"codigo": "200034", "descricao": "Hortifrúti"}]},
        ])
    if p == f"{D}/classificacoes-tributarias/ncm-aplicavel":
        return httpx.Response(200, json={"valido": q["ncm"] != "84713019"})
    if p == f"{D}/ncm":
        return httpx.Response(200, json={"tributadoPeloImpostoSeletivo": q["ncm"] == "22030000"})
    return httpx.Response(404)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ENGINE_REGULAR_URL", "http://regular.test")
    monkeypatch.setenv("ENGINE_SIMPLES_URL", "http://simples.test")
    monkeypatch.setattr(index, "_client", lambda: httpx.Client(transport=httpx.MockTransport(motor)))


def validar(linhas, regime="regular"):
    return client.post("/api/validar", data={"regime": regime, "dhFatoGerador": "2027-03-10T10:00:00-03:00"},
                       files={"arquivo": ("p.xlsx", xlsx(linhas), "application/octet-stream")})


def por_linha(r):
    return {i["linha"]: i for i in r.json()["itens"]}


def test_planilha_correta_nao_tem_apontamentos():
    r = validar([["NCM", "84713012", "Notebook", "000", "000001", 100], ["NBS", "115021000", "", "200", "200003", 10]])
    assert r.status_code == 200
    assert r.json()["resumo"] == {"linhas": 2, "ok": 2, "comAviso": 0, "comErro": 0, "vinculosConferidos": 1, "vinculosNaoConferidos": 0}
    assert r.json()["itens"] == []


def test_ncm_fora_de_vigencia_sugere_codigos_parecidos():
    r = validar([["NCM", "84713099", "", "000", "000001", 100]])
    p = por_linha(r)[2]
    assert p["status"] == "erro"
    assert [s["valor"] for s in p["problemas"][0]["sugestoes"]] == ["84713012", "84713019"]


def test_ncm_desconhecido_sugere_pela_descricao():
    r = validar([["NCM", "99999999", "cerveja de malte artesanal", "000", "000001", 100]])
    assert por_linha(r)[2]["problemas"][0]["sugestoes"][0]["valor"] == "22030000"


def test_cclasstrib_de_outro_cst_aponta_o_cst_certo():
    r = validar([["NCM", "84713012", "", "000", "200003", 100]])
    pr = por_linha(r)[2]["problemas"][0]
    assert pr["campo"] == "cClassTrib" and pr["sugestoes"][0]["valor"] == "200"


def test_cclasstrib_inexistente_lista_as_do_cst():
    r = validar([["NCM", "84713012", "", "200", "999999", 100]])
    assert [s["valor"] for s in por_linha(r)[2]["problemas"][0]["sugestoes"]] == ["200003", "200034"]


def test_vinculo_nao_aplicavel_e_ncm_com_imposto_seletivo_viram_avisos():
    r = validar([["NCM", "84713019", "", "200", "200003", 100], ["NCM", "22030000", "", "000", "000001", 100]])
    p = por_linha(r)
    assert p[2]["status"] == "aviso" and "não é aplicável" in p[2]["problemas"][0]["mensagem"]
    assert p[3]["status"] == "aviso" and "Imposto Seletivo" in p[3]["problemas"][0]["mensagem"]


def test_linhas_malformadas_nao_derrubam_a_validacao():
    r = validar([["NCM", "ABC", "", "000", "000001", 100], ["NCM", "84713012", "", "000", "000001", 100]])
    assert r.status_code == 200
    assert por_linha(r)[2]["problemas"][0]["campo"] == "formato"
    assert r.json()["resumo"]["comErro"] == 1 and r.json()["resumo"]["ok"] == 1


def test_simples_confere_cclasstrib_e_ncm():
    wb = Workbook(); ws = wb.active
    ws.append(["Tipo", "NCM / NBS", "cAtivSN", "cClassTrib", "Base de cálculo"])
    ws.append(["NCM", "84713012", "01", "999999", 100])
    b = io.BytesIO(); wb.save(b)
    r = client.post("/api/validar", data={"regime": "simples", "dhFatoGerador": "2027-03-10"}, files={"arquivo": ("p.xlsx", b.getvalue(), "application/octet-stream")})
    assert r.status_code == 200 and por_linha(r)[2]["problemas"][0]["campo"] == "cClassTrib"
