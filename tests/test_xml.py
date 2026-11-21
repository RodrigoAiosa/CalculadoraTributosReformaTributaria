"""Testes da leitura de XML de NF-e (motores simulados)."""

import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from api import index  # noqa: E402

client = TestClient(index.app)
ALIQ = json.dumps({"anexoI": {"pCBSSN": "1.1612", "pIBSSN": "0.0129"}})


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ENGINE_REGULAR_URL", "http://regular.test")
    monkeypatch.setenv("ENGINE_SIMPLES_URL", "http://simples.test")


@pytest.fixture(scope="module")
def exemplo(tmp_path_factory):
    out = tmp_path_factory.mktemp("x") / "ex.zip"
    subprocess.run([sys.executable, str(ROOT / "tools" / "gerar_xml_exemplo.py"), str(out)], check=True, capture_output=True)
    return out.read_bytes()


def motor(monkeypatch, pcbs=0.009, pibs=0.001):
    """Regular: CBS 0,9% e IBS 0,1% da base. Simples: 1,1612% e 0,0129%. Registra as chamadas."""
    chamadas = []

    def handler(req: httpx.Request) -> httpx.Response:
        corpo = json.loads(req.content)
        chamadas.append((req.url.host, corpo))
        objs = []
        for it in corpo["itens"]:
            if req.url.host == "regular.test":
                b = float(it["baseCalculo"])
                objs.append({"nObj": it["numero"], "estadoItem": "CALCULADO", "tribCalc": {"IBSCBS": {"gIBSCBS": {
                    "vBC": f"{b:.2f}", "vIBS": f"{b * pibs:.2f}", "gCBS": {"vCBS": f"{b * pcbs:.2f}"}}}}})
            else:
                b = float(it["baseCalculo"])
                objs.append({"nObj": it["numero"], "estadoItem": "CALCULADO", "tribCalc": {
                    "vBC": f"{b:.2f}", "vCBSSN": f"{b * 0.011612:.2f}", "vIBSSN": f"{b * 0.000129:.2f}", "IS": {"valor": "0.00"}, "totalItem": "0"}})
        return httpx.Response(200, json={"resultado": {"objetos": objs}, "objetos": objs})

    monkeypatch.setattr(index, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    return chamadas


def enviar(arquivos, **campos):
    return client.post("/api/xml/importar", data=campos, files=[("arquivos", (n, c, "application/xml")) for n, c in arquivos])


def zip_de(exemplo):
    z = zipfile.ZipFile(io.BytesIO(exemplo))
    return {n: z.read(n) for n in z.namelist()}


def test_regular_com_destaque_igual_confere(monkeypatch, exemplo):
    xmls = zip_de(exemplo)
    # injeta os valores que o motor simulado devolve como "destacados" na nota 1
    motor(monkeypatch)
    j = enviar([("n1.xml", xmls["01-nfe-regular-confere.xml"])]).json()
    n = j["notas"][0]
    assert n["regime"] == "regular" and len(n["itens"]) == 6
    assert n["contagem"] == {"SEM_DESTAQUE": 6}  # o gerador sem --valores não destaca nada
    assert n["itens"][0]["calc"]["vCBS"] == "9.00"


def test_envia_aliquotas_e_data_do_xml_ao_motor(monkeypatch, exemplo):
    ch = motor(monkeypatch)
    enviar([("n.xml", zip_de(exemplo)["01-nfe-regular-confere.xml"])])
    host, corpo = ch[0]
    assert host == "regular.test" and corpo["uf"] == "SP" and corpo["municipio"] == 3550308 and corpo["dhFatoGerador"].startswith("2027-03-10")
    assert corpo["itens"][0]["aliquotasNominais"] == {"cbs": 0.9, "ibsEstadual": 0.05, "ibsMunicipal": 0.05}
    assert corpo["itens"][0]["cst"] == "000" and corpo["itens"][0]["ncm"] == "22030000"


def test_confere_e_divergente(monkeypatch, tmp_path):
    motor(monkeypatch)
    ok = tmp_path / "v.json"
    # destaca exatamente o que o motor simulado calcula (0,9% e 0,1%) no 1º arquivo
    ok.write_text(json.dumps({"01-nfe-regular-confere.xml": {str(i): [f"{v * 0.009:.2f}", f"{v * 0.001:.2f}"] for i, v in enumerate([1000, 4500, 100, 350.5, 2890.9, 180], 1)}}))
    out = tmp_path / "e.zip"
    subprocess.run([sys.executable, str(ROOT / "tools" / "gerar_xml_exemplo.py"), str(out), "--valores", str(ok)], check=True, capture_output=True)
    xmls = zip_de(out.read_bytes())
    j = enviar([(n, c) for n, c in xmls.items() if n.startswith("01")]).json()
    assert j["notas"][0]["contagem"] == {"CONFERE": 6}


def test_divergencia_e_item_pulado(monkeypatch, exemplo):
    motor(monkeypatch)
    j = enviar([("n2.xml", zip_de(exemplo)["02-nfe-regular-divergente.xml"])]).json()
    n = j["notas"][0]
    assert n["itens"][1]["status"] == "DIVERGENTE" and n["itens"][1]["dif"]["vCBS"] == "-93.69"
    assert n["itens"][3]["status"] == "PULADO" and "CST/cClassTrib" in n["itens"][3]["motivo"]
    assert j["resumo"]["porStatus"]["PULADO"] == 1


def test_simples_exige_aliquotas_e_calcula(monkeypatch, exemplo):
    ch = motor(monkeypatch)
    xml = zip_de(exemplo)["03-nfe-simples-nacional.xml"]
    sem = enviar([("s.xml", xml)]).json()["notas"][0]
    assert sem["contagem"] == {"PULADO": 3} and ch == []
    j = enviar([("s.xml", xml)], aliquotasEfetivas=ALIQ).json()["notas"][0]
    assert j["regime"] == "simples" and j["contagem"] == {"SEM_DESTAQUE": 3}
    host, corpo = ch[0]
    assert host == "simples.test" and corpo["tpDoc"] == "55" and corpo["CRT"] == "1" and corpo["data"] == "2027-03-10"
    assert corpo["itens"][0]["cAtivSN"] == "01" and corpo["itens"][0]["baseCalculo"] == "10000.00"


def test_zip_com_varias_notas(monkeypatch, exemplo):
    motor(monkeypatch)
    j = enviar([("lote.zip", exemplo)], aliquotasEfetivas=ALIQ).json()
    assert j["resumo"]["notas"] == 3 and j["resumo"]["comErroLeitura"] == 0 and j["resumo"]["itens"] == 13


@pytest.mark.parametrize("conteudo,trecho", [
    (b"isto nao e xml", "XML inválido"),
    (b"<a/>", "infNFe"),
    (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x/>', "DOCTYPE"),
])
def test_xml_invalido_nao_derruba_o_lote(monkeypatch, exemplo, conteudo, trecho):
    motor(monkeypatch)
    j = enviar([("ruim.xml", conteudo), ("bom.xml", zip_de(exemplo)["01-nfe-regular-confere.xml"])]).json()
    assert trecho in j["notas"][0]["erro"] and j["notas"][1]["regime"] == "regular" and j["resumo"]["comErroLeitura"] == 1


def test_modelo_nao_suportado(monkeypatch, exemplo):
    motor(monkeypatch)
    xml = zip_de(exemplo)["01-nfe-regular-confere.xml"].replace(b"<mod>55</mod>", b"<mod>57</mod>")
    assert "Modelo 57" in enviar([("a.xml", xml)]).json()["notas"][0]["erro"]


def test_sem_arquivos_e_zip_invalido(monkeypatch):
    motor(monkeypatch)
    assert enviar([("a.zip", b"nao e zip")]).status_code == 422
    assert enviar([("a.zip", zipfile.ZipFile(io.BytesIO(), "w") and b"")]).status_code == 422
