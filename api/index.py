"""API Python (FastAPI) que faz a ponte entre o front e os motores oficiais da Receita.

Roda como Vercel Function (Python). Os motores (Java + SQLite) ficam em outro serviço
(ver engine/ e render.yaml) e são apontados por variáveis de ambiente:

    ENGINE_REGULAR_URL  ex.: https://calculadora-motor-regular.onrender.com
    ENGINE_SIMPLES_URL  ex.: https://calculadora-motor-simples.onrender.com
"""

from __future__ import annotations

import io
import os
import re
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
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


# ---------------------------------------------------------------- importação de planilha (.xlsx)
CHUNK = 100                      # itens por chamada ao motor (uma NF-e aceita até 990)
MAX_LINHAS = 5000
MAX_BYTES = 10 * 1024 * 1024
ESTADOS_CALCULADOS = {"CALCULADO", "CALCULO_SIMULADO"}

COLUNAS = {  # cabeçalho normalizado -> campo interno
    "tipo": "tipo",
    "ncmnbs": "codigo",
    "cst": "cst",
    "cclasstrib": "cclasstrib",
    "basedecalculo": "base",
    "basecalculo": "base",
    "quantidade": "quantidade",
    "qtd": "quantidade",
    "unidade": "unidade",
    "descricao": "descricao",
}
OBRIGATORIAS = {"tipo": "Tipo", "codigo": "NCM / NBS", "cst": "CST", "cclasstrib": "cClassTrib", "base": "Base de cálculo"}


def _norm(texto: Any) -> str:
    s = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s)


def _texto(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _codigo(v: Any, tamanho: int) -> str:
    """Excel guarda 02013000 como número 2013000: recompõe os zeros à esquerda."""
    t = _texto(v).replace(".", "").replace("-", "").replace(" ", "")
    return t.zfill(tamanho) if tamanho and t.isdigit() else t


def _decimal(v: Any) -> Decimal | None:
    try:
        return Decimal(_texto(v).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def ler_planilha(conteudo: bytes) -> list[dict[str, Any]]:
    """Lê o .xlsx e valida. Levanta HTTP 422 com a lista de problemas (colunas ou linhas)."""
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True)
    except Exception as exc:  # arquivo não é xlsx válido
        raise HTTPException(status_code=422, detail="Arquivo inválido: envie uma planilha .xlsx.") from exc

    linhas = wb.worksheets[0].iter_rows(values_only=True)
    mapa: dict[str, int] = {}
    numero = 0
    for numero, row in enumerate(linhas, start=1):  # cabeçalho: primeira linha com "Tipo" e "NCM / NBS"
        achados = {COLUNAS[_norm(c)]: i for i, c in enumerate(row) if _norm(c) in COLUNAS}
        if "tipo" in achados and "codigo" in achados:
            mapa = achados
            break
        if numero >= 10:
            break
    faltam = [nome for chave, nome in OBRIGATORIAS.items() if chave not in mapa]
    if not mapa or faltam:
        raise HTTPException(
            status_code=422,
            detail="Colunas obrigatórias ausentes: " + ", ".join(faltam or ["Tipo", "NCM / NBS"]) + ". Baixe o modelo para ver o formato.",
        )

    itens: list[dict[str, Any]] = []
    problemas: list[str] = []
    for row in linhas:
        numero += 1
        if all(c is None or _texto(c) == "" for c in row):
            continue  # linha em branco
        get = lambda campo: row[mapa[campo]] if campo in mapa and mapa[campo] < len(row) else None  # noqa: E731
        tipo = _texto(get("tipo")).upper()
        erros = []
        if tipo not in {"NCM", "NBS"}:
            erros.append("Tipo deve ser NCM ou NBS")
        codigo = _codigo(get("codigo"), 8 if tipo == "NCM" else 0)  # só o NCM perde zeros à esquerda no Excel
        if tipo == "NCM" and not re.fullmatch(r"\d{8}", codigo):
            erros.append("NCM deve ter 8 dígitos")
        if tipo == "NBS" and not re.fullmatch(r"\d{9}", codigo):
            erros.append("NBS deve ter 9 dígitos")
        cst = _codigo(get("cst"), 3)
        if not re.fullmatch(r"\d{3}", cst):
            erros.append("CST deve ter 3 dígitos")
        ccls = _codigo(get("cclasstrib"), 6)
        if not re.fullmatch(r"\d{6}", ccls):
            erros.append("cClassTrib deve ter 6 dígitos")
        base = _decimal(get("base"))
        if base is None or base <= 0:
            erros.append("Base de cálculo deve ser um número maior que zero")
        qtd = _decimal(get("quantidade")) if _texto(get("quantidade")) else None
        if _texto(get("quantidade")) and (qtd is None or qtd <= 0):
            erros.append("Quantidade inválida")
        if erros:
            problemas.append(f"Linha {numero}: " + "; ".join(erros))
            continue
        itens.append({"linha": numero, "tipo": tipo, "codigo": codigo, "cst": cst, "cClassTrib": ccls,
                      "base": base, "quantidade": qtd, "unidade": _texto(get("unidade")) or None})
        if len(itens) > MAX_LINHAS:
            raise HTTPException(status_code=422, detail=f"A planilha excede o limite de {MAX_LINHAS} produtos.")

    if problemas:
        extra = f" (+{len(problemas) - 30} outras)" if len(problemas) > 30 else ""
        raise HTTPException(status_code=422, detail={"mensagem": "Há linhas inválidas na planilha." + extra, "linhas": problemas[:30]})
    if not itens:
        raise HTTPException(status_code=422, detail="A planilha não tem produtos abaixo do cabeçalho.")
    return itens


def _dec(v: Any) -> Decimal:
    d = _decimal(v)
    return d if d is not None else Decimal("0")


def _bloco(ctx: dict[str, Any], bloco: list[dict[str, Any]]) -> dict[str, Any]:
    itens = []
    for i, it in enumerate(bloco, start=1):
        d: dict[str, Any] = {"numero": i, "cst": it["cst"], "cClassTrib": it["cClassTrib"], "baseCalculo": float(it["base"])}
        d[it["tipo"].lower()] = it["codigo"]
        if it["quantidade"] is not None:
            d["quantidade"] = float(it["quantidade"])
        if it["unidade"]:
            d["unidade"] = it["unidade"]
        if ctx["aliquotas"]:
            d["aliquotasNominais"] = ctx["aliquotas"]
        itens.append(d)
    body = {"id": uuid.uuid4().hex, "versao": "1.0.0", "dhFatoGerador": ctx["dh"], "municipio": ctx["municipio"], "uf": ctx["uf"], "itens": itens}
    return _call("regular", "POST", REGULAR_CALCULO, json=body)


def _resumir(bloco: list[dict[str, Any]], resp: dict[str, Any]) -> list[dict[str, Any]]:
    objetos = {o.get("nObj"): o for o in (resp.get("resultado") or {}).get("objetos", [])}
    saida = []
    for i, it in enumerate(bloco, start=1):
        o = objetos.get(i, {})
        trib = o.get("tribCalc") or {}
        g = (trib.get("IBSCBS") or {}).get("gIBSCBS") or {}
        is_ = trib.get("IS") or {}
        saida.append({
            "linha": it["linha"], "tipo": it["tipo"], "codigo": it["codigo"], "cst": it["cst"], "cClassTrib": it["cClassTrib"],
            "estado": o.get("estadoItem", "SEM_RESPOSTA"),
            "vBC": g.get("vBC"), "vCBS": (g.get("gCBS") or {}).get("vCBS"), "vIBS": g.get("vIBS"),
            "vIS": is_.get("vIS") or (is_.get("gIS") or {}).get("vIS"),
            "erros": [{"code": e.get("code"), "title": e.get("title"), "detail": e.get("detail")} for e in o.get("erros", [])],
        })
    return saida


def _opcional(v: str | None) -> float | None:
    if v is None or v.strip() == "":
        return None
    try:
        return float(v.replace(",", "."))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Alíquota inválida: {v}") from exc


@app.post("/api/regime-geral/importar")
def importar(
    arquivo: UploadFile = File(...),
    dhFatoGerador: str = Form(...),
    municipio: int = Form(...),
    uf: str = Form(..., pattern=r"^[A-Za-z]{2}$"),
    cbs: str | None = Form(None),
    ibsEstadual: str | None = Form(None),
    ibsMunicipal: str | None = Form(None),
) -> Any:
    """Calcula CBS/IBS/IS para os produtos de uma planilha .xlsx (em blocos de 100 itens)."""
    conteudo = arquivo.file.read(MAX_BYTES + 1)
    if len(conteudo) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="Arquivo maior que 10 MB.")
    itens = ler_planilha(conteudo)

    aliquotas = {k: v for k, v in {"cbs": _opcional(cbs), "ibsEstadual": _opcional(ibsEstadual), "ibsMunicipal": _opcional(ibsMunicipal)}.items() if v is not None}
    ctx = {"dh": dhFatoGerador, "municipio": municipio, "uf": uf.upper(), "aliquotas": aliquotas}
    blocos = [itens[i : i + CHUNK] for i in range(0, len(itens), CHUNK)]

    with ThreadPoolExecutor(max_workers=4) as pool:
        respostas = list(pool.map(lambda b: _bloco(ctx, b), blocos))

    resultado = [r for b, resp in zip(blocos, respostas) for r in _resumir(b, resp)]
    por_estado: dict[str, int] = {}
    totais = {"vBC": Decimal("0"), "vCBS": Decimal("0"), "vIBS": Decimal("0"), "vIS": Decimal("0")}
    for r in resultado:
        por_estado[r["estado"]] = por_estado.get(r["estado"], 0) + 1
        if r["estado"] in ESTADOS_CALCULADOS:
            for k in totais:
                totais[k] += _dec(r[k])
    return {
        "resumo": {"produtos": len(resultado), "blocos": len(blocos), "porEstado": por_estado, "comErro": sum(1 for r in resultado if r["erros"] or r["estado"] not in ESTADOS_CALCULADOS)},
        "totais": {k: f"{v:.2f}" for k, v in totais.items()},
        "avisos": sorted({r["aviso"] for r in respostas if r.get("aviso")}),
        "itens": resultado,
    }


@app.get("/api/modelo.xlsx")
def modelo() -> StreamingResponse:
    """Planilha modelo com as colunas esperadas pela importação."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Produtos"
    ws.append(["Tipo", "NCM / NBS", "Descrição", "CST", "cClassTrib", "Base de cálculo", "Quantidade", "Unidade"])
    ws.append(["NCM", "10063021", "Arroz", "200", "200003", 100.00, 20, "KG"])
    ws.append(["NBS", "122011200", "Serviço de educação", "200", "200028", 1200.00, None, None])
    for col, largura in zip("ABCDEFGH", (8, 14, 36, 7, 12, 16, 12, 9)):
        ws.column_dimensions[col].width = largura
    from openpyxl.styles import Font

    for c in ws[1]:
        c.font = Font(bold=True)
    for linha in ws.iter_rows(min_row=2, max_col=5):  # códigos como texto, preservando zeros à esquerda
        for c in linha[1:2] + linha[3:5]:
            c.number_format = "@"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": 'attachment; filename="modelo-produtos.xlsx"'})
