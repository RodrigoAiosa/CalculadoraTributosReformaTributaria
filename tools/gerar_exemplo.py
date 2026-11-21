"""Gera uma planilha de exemplo com N produtos a partir da base SQLite oficial da Receita.

Uso:
    python tools/gerar_exemplo.py <calculadora-pro.db> <saida.xlsx> [--n 1000] [--excluir falhas.txt]

A base vem do Flyway do repositório calculadora-rtc-regime-regular. Só entram pares
(NCM/NBS, CST, cClassTrib) que a própria base declara como aplicáveis, mais a classificação
genérica 000/000001 (tributação integral). `--excluir` lista (um por linha) 'TIPO|codigo|cst|cClassTrib'
a descartar, para refazer a planilha depois de validar no motor. Também deixa de fora NCM com
Imposto Seletivo e aplica as exceções de NCM aplicável da base.
"""

import argparse
import random
import sqlite3
from decimal import Decimal

from openpyxl import Workbook
from openpyxl.styles import Font

DATA = "2027-03-10"  # mesma data usada nos exemplos da tela


def carregar(con):
    ncm_vig = {r[0]: r[1] for r in con.execute(
        "select NCM_CD, NCM_DESCRICAO from NCM where length(NCM_CD)=8 and NCM_INICIO_VIGENCIA<=? and (NCM_FIM_VIGENCIA is null or NCM_FIM_VIGENCIA>=?)", (DATA, DATA))}
    nbs_vig = {r[0]: r[1] for r in con.execute(
        "select NBS_CD, NBS_DESCRICAO from NBS where length(NBS_CD)=9 and NBS_INICIO_VIGENCIA<=? and (NBS_FIM_VIGENCIA is null or NBS_FIM_VIGENCIA>=?)", (DATA, DATA))}
    # classificação vigente: CST + cClassTrib (versão 'Padrão')
    cls = {r[0]: (r[1], r[2]) for r in con.execute(
        "select c.CLTR_ID, s.SITR_CD, c.CLTR_CD from CLASSIFICACAO_TRIBUTARIA c join SITUACAO_TRIBUTARIA s on s.SITR_ID=c.CLTR_SITR_ID "
        "where c.CLTR_TIPO_ALIQUOTA='Padrão' and c.CLTR_INICIO_VIGENCIA<=? and (c.CLTR_FIM_VIGENCIA is null or c.CLTR_FIM_VIGENCIA>=?)", (DATA, DATA))}
    # NCM sujeitos ao Imposto Seletivo exigem dados de IS na operação (a planilha não os traz): ficam de fora.
    pref_is = {r[0] for r in con.execute("select AAVP_NCM_CD from ALIQUOTA_AD_VALOREM_PRODUTO union select AARP_NCM_CD from ALIQUOTA_AD_REM_PRODUTO")}
    ncm_is = lambda n: any(n.startswith(p) or p.startswith(n) for p in pref_is)  # noqa: E731
    for n in [n for n in ncm_vig if ncm_is(n)]:
        del ncm_vig[n]
    # exceções: NCM que a base tira de uma regra de NCM aplicável
    exc = {}
    for ncma_id, cd in con.execute("select ENCM_NCMA_ID, ENCM_NCM_CD from EXCECAO_NCM_APLICAVEL where ENCM_INICIO_VIGENCIA<=? and (ENCM_FIM_VIGENCIA is null or ENCM_FIM_VIGENCIA>=?)", (DATA, DATA)):
        exc.setdefault(ncma_id, []).append(cd)
    esp_ncm, esp_nbs = {}, {}
    for ncma_id, cd, cid in con.execute("select NCMA_ID, NCMA_NCM_CD, NCMA_CLTR_ID from NCM_APLICAVEL where NCMA_INICIO_VIGENCIA<=? and (NCMA_FIM_VIGENCIA is null or NCMA_FIM_VIGENCIA>=?)", (DATA, DATA)):
        if cid in cls:
            esp_ncm.setdefault(cls[cid], set()).update(
                n for n in ncm_vig if n.startswith(cd) and not any(n.startswith(e) for e in exc.get(ncma_id, [])))
    for cd, cid in con.execute("select NBSA_NBS_CD, NBSA_CLTR_ID from NBS_APLICAVEL where NBSA_INICIO_VIGENCIA<=? and (NBSA_FIM_VIGENCIA is null or NBSA_FIM_VIGENCIA>=?)", (DATA, DATA)):
        if cid in cls:
            esp_nbs.setdefault(cls[cid], set()).update(n for n in nbs_vig if n.startswith(cd))
    return ncm_vig, nbs_vig, esp_ncm, esp_nbs


def amostrar(rnd, esp, cota):
    """Distribui a cota entre as classificações (round-robin) para variar bem os cClassTrib."""
    pools = {k: sorted(v) for k, v in esp.items() if v}
    for v in pools.values():
        rnd.shuffle(v)
    chaves = sorted(pools)
    saida = []
    while len(saida) < cota and any(pools.values()):
        for k in chaves:
            if pools[k] and len(saida) < cota:
                saida.append((k, pools[k].pop()))
    return saida


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db"); ap.add_argument("saida")
    ap.add_argument("--n", type=int, default=1000); ap.add_argument("--excluir")
    ap.add_argument("--seed", type=int, default=2027)
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    excl = set(open(a.excluir).read().split()) if a.excluir else set()
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    ncm_vig, nbs_vig, esp_ncm, esp_nbs = carregar(con)

    n = a.n
    q = {"esp_ncm": int(n * 0.25), "esp_nbs": int(n * 0.10), "ger_nbs": int(n * 0.05)}
    q["ger_ncm"] = n - sum(q.values())
    folga = 1.4  # candidatos extras, para sobrar depois das exclusões
    linhas = []
    for (cst, ccls), cod in amostrar(rnd, esp_ncm, int(q["esp_ncm"] * folga)):
        linhas.append(("NCM", cod, ncm_vig[cod], cst, ccls))
    for (cst, ccls), cod in amostrar(rnd, esp_nbs, int(q["esp_nbs"] * folga)):
        linhas.append(("NBS", cod, nbs_vig[cod], cst, ccls))
    for cod in rnd.sample(sorted(nbs_vig), min(int(q["ger_nbs"] * folga), len(nbs_vig))):
        linhas.append(("NBS", cod, nbs_vig[cod], "000", "000001"))
    for cod in rnd.sample(sorted(ncm_vig), min(int(q["ger_ncm"] * folga), len(ncm_vig))):
        linhas.append(("NCM", cod, ncm_vig[cod], "000", "000001"))

    linhas = [l for l in linhas if "|".join([l[0], l[1], l[3], l[4]]) not in excl]
    # mantém a proporção pedida: corta o excedente de cada grupo, depois embaralha
    grupos = {"esp_ncm": [], "esp_nbs": [], "ger_nbs": [], "ger_ncm": []}
    for l in linhas:
        g = ("ger_" if l[4] == "000001" else "esp_") + l[0].lower()
        grupos[g].append(l)
    final = [l for g, cota in q.items() for l in grupos[g][:cota]]
    if len(final) < n:  # faltou: completa com o que sobrou
        extra = [l for g in grupos.values() for l in g if l not in final]
        final += extra[: n - len(final)]
    rnd.shuffle(final)

    wb = Workbook(); ws = wb.active; ws.title = "Produtos"
    ws.append(["Tipo", "NCM / NBS", "Descrição", "CST", "cClassTrib", "Base de cálculo", "Quantidade", "Unidade"])
    for c in ws[1]:
        c.font = Font(bold=True)
    for tipo, cod, desc, cst, ccls in final:
        base = Decimal(rnd.randint(1000, 500000)) / 100
        if tipo == "NCM":
            ws.append([tipo, cod, (desc or "")[:120], cst, ccls, float(base), rnd.randint(1, 50), rnd.choice(["UN", "KG", "LT", "CX"])])
        else:
            ws.append([tipo, cod, (desc or "")[:120], cst, ccls, float(base), None, None])
    for row in ws.iter_rows(min_row=2, max_col=5):
        for c in (row[1], row[3], row[4]):
            c.number_format = "@"
    for col, w in zip("ABCDEFGH", (8, 14, 60, 7, 12, 16, 12, 9)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    wb.save(a.saida)
    por = {}
    for t, _, _, cst, cc in final:
        por[(t, cst, cc)] = por.get((t, cst, cc), 0) + 1
    print(f"{len(final)} produtos; {len(por)} combinações (tipo, CST, cClassTrib)")
    for k, v in sorted(por.items(), key=lambda kv: -kv[1])[:8]:
        print(" ", k, v)


if __name__ == "__main__":
    main()
