"""Gera XMLs de NF-e de exemplo (ambiente de homologação, sem valor fiscal) para testar /api/xml/importar.

Uso:
    python tools/gerar_xml_exemplo.py <saida.zip> [--valores valores.json]

Os pares NCM/CST/cClassTrib vêm das planilhas de exemplo, já validadas no motor oficial. `--valores` é um JSON
{"arquivo.xml": {"1": ["vCBS", "vIBS"]}} com o que o motor calculou: entra como valor destacado na nota.
O arquivo 02 sai com um item propositalmente divergente e um item sem grupo IBSCBS, para mostrar a conferência.
"""

import argparse
import json
import zipfile
from decimal import Decimal
from xml.sax.saxutils import escape

NS = "http://www.portalfiscal.inf.br/nfe"
ALIQ = {"pCBS": "0.9", "pIBSUF": "0.05", "pIBSMun": "0.05"}  # as mesmas alíquotas da tela

# (NCM, descrição, CST, cClassTrib, valor)  — pares validados no motor
REGULAR_OK = [
    ("84191100", "Aquecedor de agua a gas, instantaneo", "000", "000001", "1000.00"),
    ("85176229", "Aparelho de telecomunicacao", "000", "000001", "4500.00"),
    ("10063021", "Arroz parboilizado", "200", "200003", "100.00"),
    ("95067000", "Patins de rodas", "000", "000001", "350.50"),
    ("51122010", "Tecido de la", "000", "000001", "2890.90"),
    ("85012000", "Motor universal", "000", "000001", "180.00"),
]
REGULAR_DIV = [
    ("85176229", "Aparelho de telecomunicacao", "000", "000001", "2200.00"),
    ("95067000", "Patins de rodas", "000", "000001", "700.00"),
    ("84191100", "Aquecedor de agua a gas, instantaneo", "000", "000001", "500.00"),
    ("85012000", "Motor universal (sem grupo IBSCBS na nota)", None, None, "900.00"),
]
SIMPLES = [
    ("95067000", "Patins de rodas", "000", "000001", "10000.00"),
    ("51122010", "Tecido de la", "000", "000001", "2500.00"),
    ("85012000", "Motor universal", "000", "000001", "800.00"),
]


def nfe(num, mod, crt, itens, destaque, divergir=None):
    """destaque(i) -> (vCBS, vIBS) ou None; divergir = {nItem: (vCBS, vIBS)} força valores errados."""
    dets = []
    for i, (ncm, desc, cst, ccls, v) in enumerate(itens, start=1):
        ib = ""
        if cst:
            vals = (divergir or {}).get(i) or destaque(i)
            g = f"<vBC>{v}</vBC>"
            if crt == "3":
                g += f"<gIBSUF><pIBSUF>{ALIQ['pIBSUF']}</pIBSUF></gIBSUF><gIBSMun><pIBSMun>{ALIQ['pIBSMun']}</pIBSMun></gIBSMun>"
            if vals:
                g += f"<vIBS>{vals[1]}</vIBS>"
            g += "<gCBS>" + (f"<pCBS>{ALIQ['pCBS']}</pCBS>" if crt == "3" else "") + (f"<vCBS>{vals[0]}</vCBS>" if vals else "") + "</gCBS>"
            ib = f"<IBSCBS><CST>{cst}</CST><cClassTrib>{ccls}</cClassTrib><gIBSCBS>{g}</gIBSCBS></IBSCBS>"
        dets.append(
            f'<det nItem="{i}"><prod><cProd>{i:03d}</cProd><cEAN>SEM GTIN</cEAN><xProd>{escape(desc)}</xProd><NCM>{ncm}</NCM>'
            f"<CFOP>5102</CFOP><uCom>UN</uCom><qCom>1.0000</qCom><vUnCom>{v}</vUnCom><vProd>{v}</vProd><indTot>1</indTot></prod>"
            f"<imposto>{ib}</imposto></det>"
        )
    chave = f"{35:02d}2703{11222333000181:014d}{mod}001{num:09d}1{num:08d}0"
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<NFe xmlns="{NS}"><infNFe Id="NFe{chave}" versao="4.00">'
        f"<ide><cUF>35</cUF><mod>{mod}</mod><serie>1</serie><nNF>{num}</nNF><dhEmi>2027-03-10T10:00:00-03:00</dhEmi><tpAmb>2</tpAmb><cMunFG>3550308</cMunFG></ide>"
        f"<emit><CNPJ>11222333000181</CNPJ><xNome>EMPRESA DE EXEMPLO LTDA - HOMOLOGACAO SEM VALOR FISCAL</xNome>"
        f"<enderEmit><xMun>Sao Paulo</xMun><UF>SP</UF></enderEmit><CRT>{crt}</CRT></emit>{''.join(dets)}</infNFe></NFe>\n"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("saida"); ap.add_argument("--valores")
    a = ap.parse_args()
    val = json.load(open(a.valores)) if a.valores else {}

    def d(nome):
        return lambda i: tuple(val[nome][str(i)]) if nome in val and str(i) in val[nome] else None

    n1, n2, n3 = "01-nfe-regular-confere.xml", "02-nfe-regular-divergente.xml", "03-nfe-simples-nacional.xml"
    with zipfile.ZipFile(a.saida, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(n1, nfe(1001, "55", "3", REGULAR_OK, d(n1)))
        z.writestr(n2, nfe(1002, "55", "3", REGULAR_DIV, d(n2), divergir={2: ("99.99", "0.01")}))
        z.writestr(n3, nfe(1003, "55", "1", SIMPLES, d(n3)))
    print("ok", a.saida)


if __name__ == "__main__":
    main()
