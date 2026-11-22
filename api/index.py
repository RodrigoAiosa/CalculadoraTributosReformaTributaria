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
import json
import queue
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
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
CHUNK = 50                       # itens por chamada ao motor (uma NF-e aceita até 990); blocos menores = andamento mais fluido
WORKERS = 3                      # chamadas simultâneas ao motor
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
    "cativsn": "cativsn",
    "valoris": "valoris",
    "valorimpostoseletivo": "valoris",
}
OBRIGATORIAS_POR = {
    "regular": {"tipo": "Tipo", "codigo": "NCM / NBS", "cst": "CST", "cclasstrib": "cClassTrib", "base": "Base de cálculo"},
    "simples": {"tipo": "Tipo", "codigo": "NCM / NBS", "cativsn": "cAtivSN", "cclasstrib": "cClassTrib", "base": "Base de cálculo"},
}
TPDOC_COM_CRT = {"55", "65", "57", "67", "63", "93", "62"}  # a NFS-e (91) aceita 1 item por nota: fora da importação


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


def ler_planilha(conteudo: bytes, regime: str = "regular", invalidas: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Lê o .xlsx e valida. Levanta HTTP 422 com a lista de problemas (colunas ou linhas).

    Com `invalidas` (lista), as linhas malformadas não interrompem a leitura: vão para a lista, com os dados crus."""
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
    faltam = [nome for chave, nome in OBRIGATORIAS_POR[regime].items() if chave not in mapa]
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
        cst, ativ, valor_is = "", "", Decimal("0")
        if regime == "regular":
            cst = _codigo(get("cst"), 3)
            if not re.fullmatch(r"\d{3}", cst):
                erros.append("CST deve ter 3 dígitos")
        else:
            ativ = _codigo(get("cativsn"), 2)
            if not re.fullmatch(r"\d{2}", ativ):
                erros.append("cAtivSN deve ter 1 ou 2 dígitos (ex.: 01)")
            if _texto(get("valoris")):
                valor_is = _decimal(get("valoris"))
                if valor_is is None or valor_is < 0:
                    erros.append("Valor IS inválido")
                    valor_is = Decimal("0")
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
            if invalidas is not None:
                invalidas.append({"linha": numero, "tipo": tipo, "codigo": codigo, "cst": cst, "cativ": ativ, "cClassTrib": ccls,
                                  "descricao": _texto(get("descricao")), "erros": erros})
            else:
                problemas.append(f"Linha {numero}: " + "; ".join(erros))
            continue
        itens.append({"linha": numero, "tipo": tipo, "codigo": codigo, "descricao": _texto(get("descricao")), "cst": cst, "cativ": ativ, "valoris": valor_is, "cClassTrib": ccls,
                      "base": base, "quantidade": qtd, "unidade": _texto(get("unidade")) or None})
        if len(itens) > MAX_LINHAS:
            raise HTTPException(status_code=422, detail=f"A planilha excede o limite de {MAX_LINHAS} produtos.")

    if problemas:
        extra = f" (+{len(problemas) - 30} outras)" if len(problemas) > 30 else ""
        raise HTTPException(status_code=422, detail={"mensagem": "Há linhas inválidas na planilha." + extra, "linhas": problemas[:30]})
    if not itens and not invalidas:
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


# --- andamento em tempo real: o servidor devolve linhas JSON (NDJSON) enquanto calcula -------------------------
def _rodar_blocos(fn: Any, blocos: list[list[dict[str, Any]]], notificar: Any) -> list[dict[str, Any]]:
    """Chama o motor para cada bloco (em paralelo) e avisa a cada bloco concluído, com o total de itens já feitos."""
    respostas: list[Any] = [None] * len(blocos)
    feito = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futuros = {pool.submit(fn, b): i for i, b in enumerate(blocos)}
        for f in as_completed(futuros):
            i = futuros[f]
            respostas[i] = f.result()
            feito += len(blocos[i])
            notificar({"e": "prog", "feito": feito})
    return respostas


def _responder(trabalho: Any, progresso: str) -> Any:
    """Sem `progresso`: devolve o JSON de uma vez. Com `progresso`: transmite eventos NDJSON e termina com {"e":"fim","dados":...}."""
    if not progresso:
        return trabalho(lambda ev: None)
    fila: queue.Queue = queue.Queue()

    def alvo() -> None:
        try:
            fila.put({"e": "fim", "dados": trabalho(fila.put)})
        except HTTPException as exc:
            fila.put({"e": "erro", "status": exc.status_code, "detail": exc.detail})
        except Exception as exc:  # noqa: BLE001
            fila.put({"e": "erro", "status": 500, "detail": f"Erro inesperado: {exc}"})
        fila.put(None)

    threading.Thread(target=alvo, daemon=True).start()

    def linhas() -> Any:
        while True:
            try:
                ev = fila.get(timeout=10)
            except queue.Empty:
                yield b"\n"  # mantém a conexão viva enquanto o motor acorda
                continue
            if ev is None:
                return
            yield (json.dumps(ev, default=str) + "\n").encode()

    return StreamingResponse(linhas(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@app.post("/api/regime-geral/importar")
def importar(
    arquivo: UploadFile = File(...),
    dhFatoGerador: str = Form(...),
    municipio: int = Form(...),
    uf: str = Form(..., pattern=r"^[A-Za-z]{2}$"),
    cbs: str | None = Form(None),
    ibsEstadual: str | None = Form(None),
    ibsMunicipal: str | None = Form(None),
    progresso: str = Form(""),
) -> Any:
    """Calcula CBS/IBS/IS para os produtos de uma planilha .xlsx (em blocos de 50 itens). Com `progresso`, transmite o andamento."""
    conteudo = arquivo.file.read(MAX_BYTES + 1)
    if len(conteudo) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="Arquivo maior que 10 MB.")
    itens = ler_planilha(conteudo)

    aliquotas = {k: v for k, v in {"cbs": _opcional(cbs), "ibsEstadual": _opcional(ibsEstadual), "ibsMunicipal": _opcional(ibsMunicipal)}.items() if v is not None}
    ctx = {"dh": dhFatoGerador, "municipio": municipio, "uf": uf.upper(), "aliquotas": aliquotas}
    blocos = [itens[i : i + CHUNK] for i in range(0, len(itens), CHUNK)]

    def trabalho(notificar: Any) -> dict[str, Any]:
        notificar({"e": "inicio", "total": len(itens)})
        respostas = _rodar_blocos(lambda b: _bloco(ctx, b), blocos, notificar)
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

    return _responder(trabalho, progresso)


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


SIMPLES_ESTADOS_OK = {"CALCULADO"}


def _bloco_simples(ctx: dict[str, Any], bloco: list[dict[str, Any]]) -> dict[str, Any]:
    itens = []
    for i, it in enumerate(bloco, start=1):
        d: dict[str, Any] = {"numero": i, "cAtivSN": it["cativ"], "cClassTrib": it["cClassTrib"],
                             "baseCalculo": f"{it['base']:.2f}", "valorImpostoSeletivo": f"{it['valoris']:.2f}"}
        d[it["tipo"].lower()] = it["codigo"]
        itens.append(d)
    body = {"id": uuid.uuid4().hex, "versao": "3.0.0", "data": ctx["data"], "tpDoc": ctx["tpDoc"], "CRT": ctx["CRT"],
            "aliquotasEfetivas": ctx["aliquotas"], "itens": itens}
    return _call("simples", "POST", SIMPLES_CALCULO, json=body)


def _resumir_simples(bloco: list[dict[str, Any]], resp: dict[str, Any]) -> list[dict[str, Any]]:
    res = resp.get("resultado") or {}
    objetos = {o.get("nObj"): o for o in res.get("objetos", [])}
    avisos = {i.get("numero"): i.get("avisos") or [] for i in (res.get("resolucaoAliquotas") or {}).get("itens", [])}
    saida = []
    for i, it in enumerate(bloco, start=1):
        o = objetos.get(i, {})
        t = o.get("tribCalc") or {}
        saida.append({
            "linha": it["linha"], "tipo": it["tipo"], "codigo": it["codigo"], "cativ": it["cativ"], "cClassTrib": it["cClassTrib"],
            "estado": o.get("estadoItem", "SEM_RESPOSTA"),
            "vBC": t.get("vBC"), "vCBS": t.get("vCBSSN"), "vIBS": t.get("vIBSSN"), "vIS": (t.get("IS") or {}).get("valor"), "total": t.get("totalItem"),
            "erros": [{"code": e.get("code"), "title": e.get("title"), "detail": e.get("detail")} for e in o.get("erros", [])],
            "avisos": [a if isinstance(a, str) else str(a) for a in avisos.get(i, [])],
        })
    return saida


@app.post("/api/simples-nacional/importar")
def importar_simples(
    arquivo: UploadFile = File(...),
    data: str = Form(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    tpDoc: str = Form(...),
    CRT: str = Form(...),
    aliquotasEfetivas: str = Form(...),
    progresso: str = Form(""),
) -> Any:
    """Calcula CBS/IBS no Simples Nacional para os produtos de uma planilha .xlsx (documentos com CRT)."""
    if tpDoc not in TPDOC_COM_CRT:
        raise HTTPException(status_code=422, detail="A importação aceita documentos com CRT (55, 65, 57, 67, 63, 93, 62). A NFS-e (91) admite só 1 item por nota: use a digitação manual.")
    try:
        aliq = json.loads(aliquotasEfetivas)
        if not isinstance(aliq, dict) or not aliq:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Alíquotas efetivas: informe um JSON com o(s) anexo(s) usado(s), ex.: {\"anexoI\": {\"pCBSSN\": \"1.1612\", \"pIBSSN\": \"0.0129\"}}") from exc

    conteudo = arquivo.file.read(MAX_BYTES + 1)
    if len(conteudo) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="Arquivo maior que 10 MB.")
    itens = ler_planilha(conteudo, regime="simples")

    ctx = {"data": data, "tpDoc": tpDoc, "CRT": CRT, "aliquotas": aliq}
    blocos = [itens[i : i + CHUNK] for i in range(0, len(itens), CHUNK)]
    def trabalho(notificar: Any) -> dict[str, Any]:
        notificar({"e": "inicio", "total": len(itens)})
        respostas = _rodar_blocos(lambda b: _bloco_simples(ctx, b), blocos, notificar)
        resultado = [r for b, resp in zip(blocos, respostas) for r in _resumir_simples(b, resp)]
        por_estado: dict[str, int] = {}
        totais = {"vBC": Decimal("0"), "vCBS": Decimal("0"), "vIBS": Decimal("0"), "vIS": Decimal("0"), "total": Decimal("0")}
        for r in resultado:
            por_estado[r["estado"]] = por_estado.get(r["estado"], 0) + 1
            if r["estado"] in SIMPLES_ESTADOS_OK:
                for k in totais:
                    totais[k] += _dec(r[k])
        avisos = sorted({a for r in resultado for a in r["avisos"]})
        return {
            "resumo": {"produtos": len(resultado), "blocos": len(blocos), "porEstado": por_estado,
                       "comErro": sum(1 for r in resultado if r["erros"] or r["estado"] not in SIMPLES_ESTADOS_OK)},
            "totais": {k: f"{v:.2f}" for k, v in totais.items()},
            "avisos": avisos[:20],
            "itens": resultado,
        }

    return _responder(trabalho, progresso)


@app.get("/api/modelo-simples.xlsx")
def modelo_simples() -> StreamingResponse:
    """Planilha modelo da importação do Simples Nacional."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Produtos"
    ws.append(["Tipo", "NCM / NBS", "Descrição", "cAtivSN", "cClassTrib", "Base de cálculo", "Valor IS"])
    ws.append(["NCM", "22030000", "Cerveja", "01", "000001", 10000.00, 0.00])
    ws.append(["NCM", "10063021", "Arroz", "01", "200003", 100.00, 0.00])
    for col, largura in zip("ABCDEFG", (8, 14, 36, 9, 12, 16, 10)):
        ws.column_dimensions[col].width = largura
    for c in ws[1]:
        c.font = Font(bold=True)
    for linha in ws.iter_rows(min_row=2, max_col=5):
        for c in (linha[1], linha[3], linha[4]):
            c.number_format = "@"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": 'attachment; filename="modelo-produtos-simples.xlsx"'})


# ---------------------------------------------------------------------------
# Leitura de XML de NF-e/NFC-e (modelos 55 e 65): calcula pelo motor e confere com o que a nota destacou
# ---------------------------------------------------------------------------
import zipfile  # noqa: E402
from xml.etree import ElementTree as ET  # noqa: E402

MAX_NOTAS = 200
MAX_XML_BYTES = 2 * 1024 * 1024          # por XML
MAX_TOTAL_BYTES = 40 * 1024 * 1024       # soma de tudo, já descompactado
CRT_REGULAR = {"3"}
CRT_SIMPLES = {"1", "2", "4"}


def _ln(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _filho(el: ET.Element | None, *caminho: str) -> ET.Element | None:
    """Desce pelos filhos diretos, ignorando namespace."""
    for nome in caminho:
        if el is None:
            return None
        el = next((c for c in el if _ln(c.tag) == nome), None)
    return el


def _txt(el: ET.Element | None, *caminho: str) -> str:
    n = _filho(el, *caminho)
    return (n.text or "").strip() if n is not None and n.text else ""


def _dx(v: str) -> Decimal | None:
    try:
        return Decimal(v) if v else None
    except InvalidOperation:
        return None


def ler_nfe(conteudo: bytes) -> dict[str, Any]:
    """Extrai de um XML de NF-e/NFC-e o cabeçalho e os itens. Levanta ValueError com mensagem legível."""
    if len(conteudo) > MAX_XML_BYTES:
        raise ValueError("XML maior que 2 MB")
    if re.search(rb"<!(DOCTYPE|ENTITY)", conteudo, re.I):
        raise ValueError("XML com DOCTYPE/ENTITY não é aceito (segurança)")
    try:
        raiz = ET.fromstring(conteudo)
    except ET.ParseError as exc:
        raise ValueError(f"XML inválido: {exc}") from exc
    inf = next((e for e in raiz.iter() if _ln(e.tag) == "infNFe"), None)
    if inf is None:
        raise ValueError("Não é uma NF-e/NFC-e (elemento infNFe não encontrado)")
    ide, emit = _filho(inf, "ide"), _filho(inf, "emit")
    mod = _txt(ide, "mod")
    if mod not in ("55", "65"):
        raise ValueError(f"Modelo {mod or '?'} não suportado (aceita 55 e 65)")
    dh = _txt(ide, "dhEmi")
    if not re.match(r"^\d{4}-\d{2}-\d{2}", dh):
        raise ValueError("dhEmi ausente ou inválida")
    uf, mun = _txt(emit, "enderEmit", "UF"), _txt(ide, "cMunFG")
    if not re.fullmatch(r"[A-Z]{2}", uf) or not re.fullmatch(r"\d{7}", mun):
        raise ValueError("UF do emitente ou cMunFG ausente/inválido")
    itens = []
    for det in (c for c in inf if _ln(c.tag) == "det"):
        prod, ib = _filho(det, "prod"), _filho(det, "imposto", "IBSCBS")
        g = _filho(ib, "gIBSCBS")
        extra = sum((_dx(_txt(prod, k)) or Decimal("0")) * s for k, s in (("vProd", 1), ("vFrete", 1), ("vSeg", 1), ("vOutro", 1), ("vDesc", -1)))
        vibs = _dx(_txt(g, "vIBS"))
        if vibs is None and g is not None:
            partes = [_dx(_txt(g, "gIBSUF", "vIBSUF")), _dx(_txt(g, "gIBSMun", "vIBSMun"))]
            vibs = sum((p for p in partes if p is not None), Decimal("0")) if any(p is not None for p in partes) else None
        itens.append({
            "nItem": det.get("nItem") or str(len(itens) + 1),
            "ncm": _txt(prod, "NCM"), "xProd": _txt(prod, "xProd"),
            "qCom": _dx(_txt(prod, "qCom")), "uCom": _txt(prod, "uCom"),
            "base": extra, "cst": _txt(ib, "CST"), "cClassTrib": _txt(ib, "cClassTrib"),
            "xmlBC": _dx(_txt(g, "vBC")), "xmlCBS": _dx(_txt(g, "gCBS", "vCBS")), "xmlIBS": vibs,
            "xmlIS": _dx(_txt(_filho(det, "imposto", "IS"), "vIS")),
            "pCBS": _dx(_txt(g, "gCBS", "pCBS")), "pIBSUF": _dx(_txt(g, "gIBSUF", "pIBSUF")) or _dx(_txt(g, "gIBSUF", "pIBS")),
            "pIBSMun": _dx(_txt(g, "gIBSMun", "pIBSMun")) or _dx(_txt(g, "gIBSMun", "pIBS")),
        })
    if not itens:
        raise ValueError("Nota sem itens (det)")
    return {
        "chave": (inf.get("Id") or "").replace("NFe", ""), "mod": mod, "nNF": _txt(ide, "nNF"), "dhEmi": dh,
        "uf": uf, "municipio": int(mun), "crt": _txt(emit, "CRT"), "emitente": _txt(emit, "xNome"), "itens": itens,
    }


def _status(it: dict[str, Any], calc: dict[str, Any], ok_estados: set[str], tol: Decimal) -> tuple[str, dict[str, Any]]:
    dif = {}
    if calc["estado"] not in ok_estados:
        return "NAO_CALCULADO", dif
    pares = [("vCBS", it["xmlCBS"], _dec(calc["vCBS"])), ("vIBS", it["xmlIBS"], _dec(calc["vIBS"]))]
    if all(x is None for _, x, _c in pares):
        return "SEM_DESTAQUE", dif
    diverge = False
    for nome, xml, c in pares:
        if xml is not None:
            d = c - xml
            dif[nome] = f"{d:.2f}"
            diverge = diverge or abs(d) > tol
    return ("DIVERGENTE" if diverge else "CONFERE"), dif


def _processar_nota(nota: dict[str, Any], cativ: str, aliq_sn: dict[str, Any] | None, tol: Decimal, ao_bloco: Any = None) -> dict[str, Any]:
    itens = nota["itens"]
    saida: dict[int, dict[str, Any]] = {}
    pular: dict[int, str] = {}
    regime = "regular" if nota["crt"] in CRT_REGULAR else "simples" if nota["crt"] in CRT_SIMPLES else None
    aviso = None
    ano = int(nota["dhEmi"][:4])
    for i, it in enumerate(itens):
        if regime is None:
            pular[i] = f"CRT {nota['crt'] or '?'} não suportado"
        elif not re.fullmatch(r"\d{8}", it["ncm"]):
            pular[i] = "NCM ausente ou diferente de 8 dígitos (o motor calcula só itens com NCM válido)"
        elif not it["cClassTrib"] or (regime == "regular" and not it["cst"]):
            pular[i] = "A nota não traz CST/cClassTrib do IBS/CBS neste item"
    if regime == "simples" and not aliq_sn:
        pular = {i: "Informe as alíquotas efetivas do Simples para calcular notas de optantes" for i in range(len(itens))}
    ok = [i for i in range(len(itens)) if i not in pular]
    if ao_bloco and pular:
        ao_bloco(len(pular))  # itens que não vão ao motor já contam como concluídos

    respostas: list[dict[str, Any]] = []
    if ok and regime == "regular":
        grupos: dict[tuple, list[int]] = {}
        for i in ok:
            it = itens[i]
            chave = (it["pCBS"], it["pIBSUF"], it["pIBSMun"]) if ano >= 2027 else ()
            grupos.setdefault(chave, []).append(i)
        for chave, idx in grupos.items():
            aliq = {}
            if chave and None not in chave:
                aliq = {"cbs": float(chave[0]), "ibsEstadual": float(chave[1]), "ibsMunicipal": float(chave[2])}
            ctx = {"dh": nota["dhEmi"], "municipio": nota["municipio"], "uf": nota["uf"], "aliquotas": aliq}
            for k in range(0, len(idx), CHUNK):
                parte = idx[k : k + CHUNK]
                bloco = [{"linha": i, "tipo": "NCM", "codigo": itens[i]["ncm"], "cst": itens[i]["cst"].zfill(3), "cClassTrib": itens[i]["cClassTrib"].zfill(6),
                          "base": itens[i]["xmlBC"] if itens[i]["xmlBC"] is not None else itens[i]["base"],
                          "quantidade": itens[i]["qCom"], "unidade": itens[i]["uCom"] or None} for i in parte]
                resp = _bloco(ctx, bloco)
                respostas.append(resp)
                for r in _resumir(bloco, resp):
                    saida[r["linha"]] = r
                if ao_bloco:
                    ao_bloco(len(bloco))
        ok_estados = ESTADOS_CALCULADOS
    elif ok and regime == "simples":
        ctx = {"data": nota["dhEmi"][:10], "tpDoc": nota["mod"], "CRT": nota["crt"], "aliquotas": aliq_sn}
        for k in range(0, len(ok), CHUNK):
            parte = ok[k : k + CHUNK]
            bloco = [{"linha": i, "tipo": "NCM", "codigo": itens[i]["ncm"], "cativ": cativ, "cClassTrib": itens[i]["cClassTrib"].zfill(6),
                      "base": itens[i]["base"], "valoris": itens[i]["xmlIS"] or Decimal("0")} for i in parte]
            resp = _bloco_simples(ctx, bloco)
            respostas.append(resp)
            for r in _resumir_simples(bloco, resp):
                saida[r["linha"]] = r
            if ao_bloco:
                ao_bloco(len(bloco))
        ok_estados = SIMPLES_ESTADOS_OK
    else:
        ok_estados = ESTADOS_CALCULADOS

    linhas, tot = [], {"calcCBS": Decimal("0"), "calcIBS": Decimal("0"), "xmlCBS": Decimal("0"), "xmlIBS": Decimal("0")}
    contagem: dict[str, int] = {}
    for i, it in enumerate(itens):
        item = {"nItem": it["nItem"], "ncm": it["ncm"], "produto": it["xProd"][:60], "cst": it["cst"], "cClassTrib": it["cClassTrib"],
                "xml": {"vBC": _s(it["xmlBC"]), "vCBS": _s(it["xmlCBS"]), "vIBS": _s(it["xmlIBS"])}}
        if i in pular:
            item.update(status="PULADO", motivo=pular[i], calc=None, dif={}, erros=[])
        else:
            c = saida.get(i, {"estado": "SEM_RESPOSTA", "erros": [], "vBC": None, "vCBS": None, "vIBS": None})
            st, dif = _status(it, c, ok_estados, tol)
            item.update(status=st, estado=c["estado"], motivo=None, dif=dif, erros=c["erros"],
                        calc={"vBC": c["vBC"], "vCBS": c["vCBS"], "vIBS": c["vIBS"]})
            if st != "NAO_CALCULADO":
                tot["calcCBS"] += _dec(c["vCBS"]); tot["calcIBS"] += _dec(c["vIBS"])
                tot["xmlCBS"] += it["xmlCBS"] or Decimal("0"); tot["xmlIBS"] += it["xmlIBS"] or Decimal("0")
        contagem[item["status"]] = contagem.get(item["status"], 0) + 1
        linhas.append(item)
    aviso = sorted({r["aviso"] for r in respostas if r.get("aviso")})
    return {"regime": regime, "contagem": contagem, "totais": {k: f"{v:.2f}" for k, v in tot.items()}, "avisos": aviso, "itens": linhas}


def _s(v: Decimal | None) -> str | None:
    return None if v is None else f"{v:.2f}"


def _coletar_xmls(arquivos: list[UploadFile]) -> list[tuple[str, bytes]]:
    xmls: list[tuple[str, bytes]] = []
    total = 0
    for up in arquivos:
        nome = up.filename or "arquivo"
        dados = up.file.read(MAX_TOTAL_BYTES + 1)
        total += len(dados)
        if total > MAX_TOTAL_BYTES:
            raise HTTPException(status_code=413, detail="Envio maior que 40 MB.")
        if nome.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(dados)) as z:
                    membros = [m for m in z.infolist() if m.filename.lower().endswith(".xml") and not m.is_dir()]
                    if len(membros) > MAX_NOTAS or sum(m.file_size for m in membros) > MAX_TOTAL_BYTES:
                        raise HTTPException(status_code=413, detail=f"ZIP com mais de {MAX_NOTAS} XMLs ou maior que 40 MB descompactado.")
                    xmls += [(m.filename, z.read(m)) for m in membros]
            except zipfile.BadZipFile as exc:
                raise HTTPException(status_code=422, detail=f"{nome}: ZIP inválido") from exc
        else:
            xmls.append((nome, dados))
    if not xmls:
        raise HTTPException(status_code=422, detail="Envie ao menos um arquivo .xml (ou um .zip com XMLs).")
    if len(xmls) > MAX_NOTAS:
        raise HTTPException(status_code=413, detail=f"No máximo {MAX_NOTAS} notas por envio.")
    return xmls


@app.post("/api/xml/importar")
def importar_xml(
    arquivos: list[UploadFile] = File(...),
    cAtivSN: str = Form("01", pattern=r"^\d{1,2}$"),
    aliquotasEfetivas: str = Form(""),
    tolerancia: str = Form("0.01"),
    progresso: str = Form(""),
) -> Any:
    """Lê XMLs de NF-e/NFC-e, calcula CBS/IBS no motor oficial e compara com o que a nota destacou."""
    tol = _decimal(tolerancia)
    if tol is None or tol < 0:
        raise HTTPException(status_code=422, detail="Tolerância inválida.")
    aliq_sn = None
    if aliquotasEfetivas.strip():
        try:
            aliq_sn = json.loads(aliquotasEfetivas)
            if not isinstance(aliq_sn, dict) or not aliq_sn:
                raise ValueError
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Alíquotas efetivas: informe um JSON, ex.: {\"anexoI\": {\"pCBSSN\": \"1.1612\", \"pIBSSN\": \"0.0129\"}}") from exc

    xmls = _coletar_xmls(arquivos)

    lidas: list[tuple[str, dict[str, Any] | str]] = []
    for nome, dados in xmls:
        try:
            lidas.append((nome, ler_nfe(dados)))
        except ValueError as exc:
            lidas.append((nome, str(exc)))
    total_itens = sum(len(n["itens"]) for _, n in lidas if isinstance(n, dict))

    def trabalho(notificar: Any) -> dict[str, Any]:
        notificar({"e": "inicio", "total": total_itens})
        feito = [0]
        trava = threading.Lock()

        def avancou(qtd: int) -> None:
            with trava:
                feito[0] += qtd
                notificar({"e": "prog", "feito": feito[0]})

        def trabalhar(par: tuple[str, dict[str, Any] | str]) -> dict[str, Any]:
            nome, nota = par
            if isinstance(nota, str):
                return {"arquivo": nome, "erro": nota}
            r = _processar_nota(nota, cAtivSN.zfill(2), aliq_sn, tol, avancou)
            return {"arquivo": nome, **{k: nota[k] for k in ("chave", "mod", "nNF", "dhEmi", "emitente", "crt")}, **r}

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            notas = list(pool.map(trabalhar, lidas))

        geral: dict[str, int] = {}
        tot = {"calcCBS": Decimal("0"), "calcIBS": Decimal("0"), "xmlCBS": Decimal("0"), "xmlIBS": Decimal("0")}
        for n in notas:
            for k, v in n.get("contagem", {}).items():
                geral[k] = geral.get(k, 0) + v
            for k in tot:
                tot[k] += _dec(n.get("totais", {}).get(k))
        return {
            "resumo": {"notas": len(notas), "comErroLeitura": sum(1 for n in notas if "erro" in n), "itens": sum(geral.values()), "porStatus": geral},
            "totais": {k: f"{v:.2f}" for k, v in tot.items()},
            "avisos": sorted({a for n in notas for a in n.get("avisos", [])}),
            "notas": notas,
        }

    return _responder(trabalho, progresso)


# ---------------------------------------------------------------- validação prévia da planilha
MAX_VINCULOS = 300   # pares (NCM/NBS, cClassTrib) conferidos no motor por planilha
MAX_SUGESTOES = 200  # linhas com sugestão por descrição (limita o custo da busca)
CLASSE_GENERICA = "000001"


def _chave(d: Any, *nomes: str) -> str:
    for n in nomes:
        if isinstance(d, dict) and d.get(n) not in (None, ""):
            return str(d[n])
    return ""


def _lista_dados(caminho: str, data: str) -> list[Any]:
    r = _call("regular", "GET", f"{REGULAR_DADOS}/{caminho}", params={"data": data})["resultado"]
    return r if isinstance(r, list) else []


def _carregar_referencias(data: str) -> dict[str, Any]:
    """Códigos vigentes na data, direto do motor (fonte oficial): NCM, NBS e CST → cClassTrib."""
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_ncm = ex.submit(_lista_dados, "ncm/vigentes", data)
        f_nbs = ex.submit(_lista_dados, "nbs/lista", data)
        f_cst = ex.submit(_lista_dados, "situacoes-tributarias/cbs-ibs", data)
        ncm, nbs, csts = f_ncm.result(), f_nbs.result(), f_cst.result()
    cst_classes: dict[str, dict[str, str]] = {}
    for c in csts:
        filhos = next((v for v in c.values() if isinstance(v, list) and v and isinstance(v[0], dict)), []) if isinstance(c, dict) else []
        cst_classes[_chave(c, "codigo")] = {_chave(f, "codigo"): _chave(f, "descricao") for f in filhos}
    return {
        "NCM": {_chave(n, "codigo", "ncm"): _chave(n, "descricao") for n in ncm if _chave(n, "codigo", "ncm")},
        "NBS": {_chave(n, "codigo", "nbs"): _chave(n, "descricao") for n in nbs if _chave(n, "codigo", "nbs")},
        "cst": cst_classes,
    }


def _tokens(texto: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{4,}", unicodedata.normalize("NFKD", texto.lower()).encode("ascii", "ignore").decode())}


def sugerir_codigos(tipo: str, codigo: str, descricao: str, base: dict[str, str], buscar_por_descricao: bool = True) -> list[dict[str, str]]:
    """Códigos vigentes parecidos: mesmo prefixo (do mais longo ao mais curto) e, se houver descrição, palavras em comum."""
    achados: dict[str, str] = {}
    if codigo.isdigit():
        for n in range(min(len(codigo) - 1, 7), 3, -1):
            pref = codigo[:n]
            achados = {c: d for c, d in sorted(base.items()) if c.startswith(pref)}
            if achados:
                break
    sugestoes = [{"valor": c, "descricao": d} for c, d in list(achados.items())[:5]]
    if buscar_por_descricao and not sugestoes and descricao:
        alvo = _tokens(descricao)
        if alvo:
            pontos = sorted(((len(alvo & _tokens(d)), c) for c, d in base.items()), reverse=True)[:5]
            sugestoes = [{"valor": c, "descricao": base[c]} for p, c in pontos if p > 0]
    return sugestoes


def _consulta_segura(caminho: str, params: dict[str, str]) -> dict[str, Any] | None:
    try:
        r = _call("regular", "GET", f"{REGULAR_DADOS}/{caminho}", params=params)["resultado"]
        return r if isinstance(r, dict) else None
    except HTTPException:
        return None


def validar_itens(itens: list[dict[str, Any]], invalidas: list[dict[str, Any]], regime: str, data: str) -> dict[str, Any]:
    ref = _carregar_referencias(data)
    todas_classes = {c: cst for cst, cs in ref["cst"].items() for c in cs}
    problemas: dict[int, dict[str, Any]] = {}

    def anotar(l: int, gravidade: str, campo: str, msg: str, sugestoes: list[dict[str, str]] | None = None) -> None:
        p = problemas.setdefault(l, {"linha": l, "status": "ok", "problemas": []})
        if gravidade == "erro":
            p["status"] = "erro"
        elif p["status"] == "ok":
            p["status"] = "aviso"
        p["problemas"].append({"gravidade": gravidade, "campo": campo, "mensagem": msg, **({"sugestoes": sugestoes} if sugestoes else {})})

    for inv in invalidas:
        for e in inv["erros"]:
            anotar(inv["linha"], "erro", "formato", e)
    n_desc = 0
    for it in itens:
        l, tipo, cod = it["linha"], it["tipo"], it["codigo"]
        if cod not in ref[tipo]:
            busca = n_desc < MAX_SUGESTOES
            n_desc += 1
            anotar(l, "erro", tipo, f"{tipo} {cod} não está vigente em {data[8:]}/{data[5:7]}/{data[:4]}.",
                   sugerir_codigos(tipo, cod, it.get("descricao", ""), ref[tipo], busca))
        ccls = it["cClassTrib"]
        if regime == "regular":
            classes = ref["cst"].get(it["cst"])
            if classes is None:
                anotar(l, "erro", "CST", f"CST {it['cst']} não existe.", [{"valor": c, "descricao": ""} for c in sorted(ref["cst"])[:8]])
            elif ccls not in classes:
                dono = todas_classes.get(ccls)
                if dono:
                    anotar(l, "erro", "cClassTrib", f"O cClassTrib {ccls} pertence ao CST {dono}, não ao {it['cst']}.", [{"valor": dono, "descricao": "CST correto para este cClassTrib"}])
                else:
                    anotar(l, "erro", "cClassTrib", f"cClassTrib {ccls} não existe para o CST {it['cst']}.", [{"valor": c, "descricao": d} for c, d in sorted(classes.items())[:6]])
        elif ccls not in todas_classes:
            anotar(l, "erro", "cClassTrib", f"cClassTrib {ccls} não existe na data informada.")

    # conferências item a item no motor (só para códigos que passaram nas etapas acima)
    validos = [it for it in itens if it["linha"] not in problemas or problemas[it["linha"]]["status"] != "erro"]
    pares: dict[tuple[str, str, str], list[int]] = {}
    for it in validos:
        if it["cClassTrib"] != CLASSE_GENERICA:
            pares.setdefault((it["tipo"], it["codigo"], it["cClassTrib"]), []).append(it["linha"])
    ncms = {it["codigo"]: [] for it in validos if it["tipo"] == "NCM"}
    for it in validos:
        if it["tipo"] == "NCM":
            ncms[it["codigo"]].append(it["linha"])
    pares_lista = list(pares)[:MAX_VINCULOS]

    def vinculo(par: tuple[str, str, str]) -> tuple[tuple[str, str, str], bool | None]:
        tipo, cod, ccls = par
        r = _consulta_segura("classificacoes-tributarias/" + ("ncm-aplicavel" if tipo == "NCM" else "nbs-aplicavel"),
                             {"cClassTrib": ccls, "ncm" if tipo == "NCM" else "nbs": cod, "dataOcorrenciaFatoGerador": data})
        return par, (None if r is None else r.get("valido"))

    def seletivo(cod: str) -> tuple[str, bool]:
        r = _consulta_segura("ncm", {"ncm": cod, "data": data})
        return cod, bool(r and r.get("tributadoPeloImpostoSeletivo"))

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        res_v = list(ex.map(vinculo, pares_lista))
        res_s = list(ex.map(seletivo, list(ncms)[:MAX_VINCULOS])) if regime == "regular" else []
    for (tipo, cod, ccls), ok in res_v:
        if ok is False:
            for l in pares[(tipo, cod, ccls)]:
                anotar(l, "aviso", "cClassTrib", f"O cClassTrib {ccls} não é aplicável ao {tipo} {cod} segundo o motor; o cálculo deve rejeitar o item.")
    for cod, sim in res_s:
        if sim:
            for l in ncms[cod]:
                anotar(l, "aviso", "NCM", f"O NCM {cod} está sujeito ao Imposto Seletivo; o cálculo exigirá os dados do IS.")

    lista = sorted(problemas.values(), key=lambda p: p["linha"])
    total = len(itens) + len(invalidas)
    n_erro = sum(1 for p in lista if p["status"] == "erro")
    n_aviso = sum(1 for p in lista if p["status"] == "aviso")
    return {
        "resumo": {"linhas": total, "ok": total - n_erro - n_aviso, "comAviso": n_aviso, "comErro": n_erro,
                   "vinculosConferidos": len(pares_lista), "vinculosNaoConferidos": max(len(pares) - len(pares_lista), 0)},
        "itens": lista[:500],
        "truncado": len(lista) > 500,
    }


@app.post("/api/validar")
def validar(
    arquivo: UploadFile = File(...),
    regime: str = Form("regular", pattern=r"^(regular|simples)$"),
    dhFatoGerador: str = Form(..., pattern=r"^\d{4}-\d{2}-\d{2}"),
) -> Any:
    """Confere a planilha antes de calcular: formato, vigência de NCM/NBS, CST/cClassTrib e vínculos, com sugestões."""
    conteudo = arquivo.file.read(MAX_BYTES + 1)
    if len(conteudo) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="Arquivo maior que 10 MB.")
    invalidas: list[dict[str, Any]] = []
    itens = ler_planilha(conteudo, regime, invalidas)
    return validar_itens(itens, invalidas, regime, dhFatoGerador[:10])
