"""Testes da API com os motores simulados (httpx.MockTransport) — não precisam de Java."""

import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from api import index  # noqa: E402

client = TestClient(index.app)

REGULAR = {
    "dhFatoGerador": "2027-03-10T10:00:00-03:00",
    "municipio": 4314902,
    "uf": "RS",
    "itens": [
        {
            "numero": 1,
            "ncm": "10063021",
            "cst": "200",
            "cClassTrib": "200003",
            "baseCalculo": 100.0,
            "aliquotasNominais": {"cbs": 8.8, "ibsEstadual": 0.05, "ibsMunicipal": 0.05},
        }
    ],
}
SIMPLES = {
    "data": "2027-03-15",
    "tpDoc": "55",
    "CRT": "1",
    "aliquotasEfetivas": {"anexoI": {"pCBSSN": "1.1612", "pIBSSN": "0.0129"}},
    "itens": [{"numero": 1, "cAtivSN": "01", "cClassTrib": "000001", "ncm": "22030000", "baseCalculo": "10000.00"}],
}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ENGINE_REGULAR_URL", "http://regular.test/")
    monkeypatch.setenv("ENGINE_SIMPLES_URL", "http://simples.test")


def use_engine(monkeypatch, handler):
    calls = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    monkeypatch.setattr(index, "_client", lambda: httpx.Client(transport=httpx.MockTransport(wrapped)))
    return calls


def test_regular_repassa_payload_e_extras(monkeypatch):
    calls = use_engine(monkeypatch, lambda r: httpx.Response(200, json={"objetos": [{"nObj": 1, "estadoItem": "CALCULO_SIMULADO"}]}, headers={"x-warning-dados-simulados": "aliquotas informadas"}))
    resp = client.post("/api/regime-geral/calcular", json=REGULAR)
    assert resp.status_code == 200
    assert resp.json()["resultado"]["objetos"][0]["estadoItem"] == "CALCULO_SIMULADO"
    assert resp.json()["aviso"] == "aliquotas informadas"
    sent = calls[0]
    assert str(sent.url) == "http://regular.test/api/calculadora/observabilidade/regime-geral"
    import json

    body = json.loads(sent.content)
    assert body["itens"][0]["aliquotasNominais"]["cbs"] == 8.8  # campo extra preservado
    assert "nbs" not in body["itens"][0]  # None não é enviado
    assert body["versao"] == "1.0.0" and len(body["id"]) == 32  # preenchidos pela API


def test_simples_vai_para_nao_assistido(monkeypatch):
    calls = use_engine(monkeypatch, lambda r: httpx.Response(200, json={"objetos": []}))
    resp = client.post("/api/simples-nacional/calcular", json=SIMPLES)
    assert resp.status_code == 200
    assert str(calls[0].url) == "http://simples.test/api/calculadora/observabilidade/simples-nacional/nao-assistido"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(itens=[]),
        lambda p: p["itens"][0].update(cClassTrib="12"),
        lambda p: p.update(uf="RSX"),
        lambda p: p["itens"][0].update(ncm="123"),
    ],
)
def test_regular_valida_entrada_sem_chamar_motor(monkeypatch, mutate):
    import copy

    calls = use_engine(monkeypatch, lambda r: httpx.Response(200, json={}))
    payload = copy.deepcopy(REGULAR)
    mutate(payload)
    assert client.post("/api/regime-geral/calcular", json=payload).status_code == 422
    assert calls == []


def test_erro_4xx_do_motor_e_devolvido(monkeypatch):
    use_engine(monkeypatch, lambda r: httpx.Response(422, json={"code": "REG-011", "title": "NCM não vinculada"}))
    resp = client.post("/api/regime-geral/calcular", json=REGULAR)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "REG-011"


def test_5xx_do_motor_vira_502(monkeypatch):
    use_engine(monkeypatch, lambda r: httpx.Response(500, json={"x": 1}))
    assert client.post("/api/regime-geral/calcular", json=REGULAR).status_code == 502


def test_retry_quando_motor_esta_acordando(monkeypatch):
    estado = {"n": 0}

    def handler(r):
        estado["n"] += 1
        return httpx.Response(503) if estado["n"] == 1 else httpx.Response(200, json={"objetos": []})

    use_engine(monkeypatch, handler)
    assert client.post("/api/regime-geral/calcular", json=REGULAR).status_code == 200
    assert estado["n"] == 2


def test_motor_fora_do_ar_vira_504(monkeypatch):
    def handler(r):
        raise httpx.ConnectError("down")

    use_engine(monkeypatch, handler)
    assert client.post("/api/regime-geral/calcular", json=REGULAR).status_code == 504


def test_sem_variavel_de_ambiente(monkeypatch):
    monkeypatch.delenv("ENGINE_REGULAR_URL")
    resp = client.post("/api/regime-geral/calcular", json=REGULAR)
    assert resp.status_code == 503 and "ENGINE_REGULAR_URL" in resp.json()["detail"]


def test_descricao_de_erro(monkeypatch):
    calls = use_engine(monkeypatch, lambda r: httpx.Response(200, json={"codigo": "VSN-001"}))
    assert client.get("/api/erros/simples-nacional/VSN-001").status_code == 200
    assert calls[0].url.path.endswith("/simples-nacional/erros") and calls[0].url.params["codigo"] == "VSN-001"
    assert client.get("/api/erros/regime-geral/..%2Fx").status_code in (404, 422)
    assert client.get("/api/erros/regime-geral/XXX-1").status_code == 422
    assert client.get("/api/erros/outro/REG-001").status_code == 404


def test_dados_abertos_lista_fechada(monkeypatch):
    calls = use_engine(monkeypatch, lambda r: httpx.Response(200, json=[]))
    assert client.get("/api/dados-abertos/ncm", params={"ncm": "22030000", "data": "2027-01-01"}).status_code == 200
    assert calls[0].url.path == "/api/calculadora/dados-abertos/ncm"
    assert client.get("/api/dados-abertos/xml/generate").status_code == 404
    assert client.get("/api/dados-abertos/ncm", params={"data": "x"}).status_code == 422


def test_health(monkeypatch):
    assert client.get("/api/health").json() == {"ok": True, "motores": {"regular": True, "simples": True}}


def test_aquecer_informa_quem_esta_pronto(monkeypatch):
    def handler(r):
        return httpx.Response(200, json={}) if "regular.test" in str(r.url) else httpx.Response(503)

    real_client = httpx.Client
    monkeypatch.setattr(index.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    assert client.get("/api/aquecer").json() == {"regular": True, "simples": False}
