"""API Python (FastAPI) que faz a ponte entre o front e os motores oficiais da Receita.

Roda como Vercel Function (Python). Os motores (Java + SQLite) ficam em outro serviço
(ver engine/ e render.yaml) e são apontados por variáveis de ambiente:

    ENGINE_REGULAR_URL  ex.: https://calculadora-motor-regular.onrender.com
    ENGINE_SIMPLES_URL  ex.: https://calculadora-motor-simples.onrender.com
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

MAX_ITENS = 990  # limite de itens de uma NF-e
TIMEOUT = httpx.Timeout(150.0, connect=20.0)  # motor free pode levar 2-3 min para acordar (maxDuration=300 no vercel.json)
RETRY_STATUS = {502, 503, 504}

# Caminhos dos motores (prefixo /api é o context-path do Spring dos dois projetos).
REGULAR_CALCULO = "/api/calculadora/observabilidade/regime-geral"
REGULAR_ERROS = "/api/calculadora/observabilidade/erros"
SIMPLES_CALCULO = "/api/calculadora/observabilidade/simples-nacional/nao-assistido"
SIMPLES_ERROS = "/api/calculadora/observabilidade/simples-nacional/erros"
REGULAR_DADOS = "/api/calculadora/dados-abertos"
REGULAR_VERSAO = "/api/calculadora/dados-abertos/versao"
SIMPLES_VERSAO = "/api/versao"

# Consultas de dados abertos liberadas (lista fechada: o front não vira proxy aberto).
DADOS_ABERTOS = {
    "ncm",
    "nbs",
    "fundamentacoes-legais",
    "aliquota-uniao",
    "aliquota-uf",
    "aliquota-municipio",
    "situacoes-tributarias/cbs-ibs",
    "classificacoes-tributarias/cbs-ibs",
}

app = FastAPI(title="Calculadora de Tributos - Reforma Tributária", docs_url="/api/docs", openapi_url="/api/openapi.json")


# ---------------------------------------------------------------- modelos
class ItemRegular(BaseModel):
    model_config = ConfigDict(extra="allow")  # campos da Receita passam sem alteração

    numero: int = Field(ge=1)
    cst: str = Field(pattern=r"^\d{3}$")
    cClassTrib: str = Field(pattern=r"^\d{6}$")
    ncm: str | None = Field(default=None, pattern=r"^\d{8}$")
    nbs: str | None = Field(default=None, pattern=r"^\d{9}$")


class OperacaoRegular(BaseModel):
    model_config = ConfigDict(extra="allow")

    dhFatoGerador: str
    municipio: int
    uf: str = Field(pattern=r"^[A-Za-z]{2}$")
    itens: list[ItemRegular] = Field(min_length=1, max_length=MAX_ITENS)


class ItemSimples(BaseModel):
    model_config = ConfigDict(extra="allow")

    numero: int = Field(ge=1)
    cClassTrib: str = Field(pattern=r"^\d{6}$")
    baseCalculo: str


class OperacaoSimples(BaseModel):
    model_config = ConfigDict(extra="allow")

    data: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    tpDoc: str
    itens: list[ItemSimples] = Field(min_length=1, max_length=MAX_ITENS)


# ---------------------------------------------------------------- acesso aos motores
def _engine_url(name: str) -> str:
    var = f"ENGINE_{name.upper()}_URL"
    url = os.environ.get(var, "").rstrip("/")
    if not url:
        raise HTTPException(status_code=503, detail=f"Variável de ambiente {var} não configurada.")
    return url


def _client() -> httpx.Client:
    """Fábrica separada para os testes poderem substituir o transporte."""
    return httpx.Client(timeout=TIMEOUT)


def _call(engine: str, method: str, path: str, **kwargs: Any) -> Any:
    url = _engine_url(engine) + path
    last: Exception | None = None
    with _client() as client:
        for attempt in range(2):  # 2ª tentativa cobre o "acordar" do motor
            try:
                resp = client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                last = exc
                continue
            if resp.status_code in RETRY_STATUS and attempt == 0:
                continue
            break
        else:
            raise HTTPException(status_code=504, detail="Motor de cálculo indisponível (pode estar iniciando). Tente de novo em instantes.") from last

    try:
        body = resp.json()
    except ValueError:
        body = {"detail": resp.text[:500]}
    if resp.status_code >= 400:
        # 4xx do motor são erros de entrada (ex.: 422): devolvemos como vieram.
        status = resp.status_code if resp.status_code < 500 else 502
        raise HTTPException(status_code=status, detail=body)
    warn = resp.headers.get("x-warning-dados-simulados")
    return {"resultado": body, "aviso": warn} if warn else {"resultado": body}


# ---------------------------------------------------------------- rotas
@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "motores": {
            "regular": bool(os.environ.get("ENGINE_REGULAR_URL")),
            "simples": bool(os.environ.get("ENGINE_SIMPLES_URL")),
        },
    }


def _ping(engine: str, path: str) -> bool:
    """True se o motor respondeu rápido; a própria chamada já faz o Render acordá-lo."""
    try:
        url = _engine_url(engine) + path
        with httpx.Client(timeout=httpx.Timeout(8.0, connect=5.0)) as client:
            return client.get(url).status_code < 400
    except (httpx.HTTPError, HTTPException):
        return False


@app.get("/api/aquecer")
def aquecer() -> dict[str, bool]:
    """Acorda os motores (plano free dorme). O front chama isso ao abrir a página."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as pool:
        reg = pool.submit(_ping, "regular", REGULAR_VERSAO)
        sim = pool.submit(_ping, "simples", SIMPLES_VERSAO)
        return {"regular": reg.result(), "simples": sim.result()}


@app.post("/api/regime-geral/calcular")
def calcular_regular(op: OperacaoRegular) -> Any:
    """Cálculo de CBS/IBS/IS (regime geral) com erros por item (observabilidade)."""
    body = op.model_dump(exclude_none=True)
    body.setdefault("id", uuid.uuid4().hex)  # o motor exige id e versao
    body.setdefault("versao", "1.0.0")
    return _call("regular", "POST", REGULAR_CALCULO, json=body)


@app.post("/api/simples-nacional/calcular")
def calcular_simples(op: OperacaoSimples) -> Any:
    """Cálculo de CBS/IBS no Simples Nacional (não assistido) com erros por item."""
    body = op.model_dump(exclude_none=True)
    body.setdefault("id", uuid.uuid4().hex)
    body.setdefault("versao", "3.0.0")
    return _call("simples", "POST", SIMPLES_CALCULO, json=body)


@app.get("/api/erros/{regime}/{codigo}")
def descricao_erro(regime: str, codigo: str) -> Any:
    """Descrição de um código de erro (REG-xxx no regular, VSN-xxx no Simples)."""
    if not re.fullmatch(r"(REG|VSN)-\d{3}", codigo):
        raise HTTPException(status_code=422, detail="Código inválido. Use REG-000 ou VSN-000.")
    if regime == "regime-geral":
        return _call("regular", "GET", REGULAR_ERROS, params={"codigo": codigo})
    if regime == "simples-nacional":
        return _call("simples", "GET", SIMPLES_ERROS, params={"codigo": codigo})
    raise HTTPException(status_code=404, detail="Regime desconhecido.")


@app.get("/api/dados-abertos/{recurso:path}")
def dados_abertos(
    recurso: str,
    data: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ncm: str | None = Query(default=None, pattern=r"^\d{1,8}$"),
    nbs: str | None = Query(default=None, pattern=r"^\d{1,9}$"),
    codigoUf: str | None = Query(default=None, pattern=r"^\d{2}$"),
) -> Any:
    """Consultas de apoio (NCM, NBS, cClassTrib, alíquotas) ao motor do regime geral."""
    if recurso not in DADOS_ABERTOS:
        raise HTTPException(status_code=404, detail="Recurso não disponível.")
    params = {k: v for k, v in {"data": data, "ncm": ncm, "nbs": nbs, "codigoUf": codigoUf}.items() if v}
    return _call("regular", "GET", f"{REGULAR_DADOS}/{recurso}", params=params)
