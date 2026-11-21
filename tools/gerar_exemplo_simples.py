"""Gera a planilha de exemplo do Simples Nacional (NF-e, tpDoc 55) a partir da base oficial.

Uso:
    python tools/gerar_exemplo_simples.py <calculadora-pro.db> <saida.xlsx> [--n 1000] [--excluir falhas.txt]

Reaproveita a seleção de NCM/cClassTrib de gerar_exemplo.py (pares que a base declara aplicáveis, mais a
classificação genérica 000001) e acrescenta o cAtivSN. Atividades 01 e 02 (Anexo I, mercadorias) são as
admitidas na NF-e/NFC-e que usam um único anexo; o exemplo usa só o Anexo I, então as alíquotas efetivas são
{"anexoI": {...}}. `--excluir` lista 'TIPO|codigo|cAtivSN|cClassTrib' a descartar depois de validar no motor.
"""

import argparse
import random
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gerar_exemplo import amostrar, carregar  # noqa: E402

ATIVIDADES = ["01"] * 7 + ["02"] * 3  # 70% revenda/industrializadas, 30% demais bens materiais


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db"); ap.add_argument("saida")
    ap.add_argument("--n", type=int, default=1000); ap.add_argument("--excluir")
    ap.add_argument("--seed", type=int, default=2027)
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    excl = set(open(a.excluir).read().split()) if a.excluir else set()
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    ncm_vig, _, esp_ncm, _ = carregar(con)

    folga = 1.5
    cand = [("NCM", cod, ncm_vig[cod], cc) for (_, cc), cod in amostrar(rnd, esp_ncm, int(a.n * 0.35 * folga))]
    cand += [("NCM", cod, ncm_vig[cod], "000001") for cod in rnd.sample(sorted(ncm_vig), int(a.n * 0.65 * folga))]
    linhas = []
    for tipo, cod, desc, cc in cand:
        ativ = rnd.choice(ATIVIDADES)
        if "|".join([tipo, cod, ativ, cc]) not in excl:
            linhas.append((tipo, cod, desc, ativ, cc))
    final = linhas[: a.n]
    rnd.shuffle(final)

    wb = Workbook(); ws = wb.active; ws.title = "Produtos"
    ws.append(["Tipo", "NCM / NBS", "Descrição", "cAtivSN", "cClassTrib", "Base de cálculo", "Valor IS"])
    for c in ws[1]:
        c.font = Font(bold=True)
    for tipo, cod, desc, ativ, cc in final:
        ws.append([tipo, cod, (desc or "")[:120], ativ, cc, float(Decimal(rnd.randint(1000, 500000)) / 100), 0.0])
    for row in ws.iter_rows(min_row=2, max_col=5):
        for c in (row[1], row[3], row[4]):
            c.number_format = "@"
    for col, w in zip("ABCDEFG", (8, 14, 60, 9, 12, 16, 10)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    wb.save(a.saida)
    print(f"{len(final)} produtos")


if __name__ == "__main__":
    main()
