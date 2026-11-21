# Casos de referência

Casos para conferir se a cadeia **tela → API Python → motor da Receita** continua calculando certo
(use o botão *Preencher dados de teste* de cada aba).

## Regime regular (do `docs/exemplos.md` do motor da Receita)

Data 2027-03-10, RS, município 4314902, alíquotas CBS 8,8 / IBS UF 0,05 / IBS Mun 0,05.

| Item | Entrada | Esperado |
|---|---|---|
| 1 | NCM 10063021, CST 200, cClassTrib 200003, base 100,00 | CBS 0,00 / IBS 0,00 (redução de 100%) |
| 2 | NBS 122011200, CST 200, cClassTrib 200028, base 1200,00 | CBS 42,24 / IBS 0,48 |
| Total | | base 1300,00 · CBS 42,24 · IBS 0,48 |

Estado dos itens: `CALCULO_SIMULADO` (alíquotas informadas manualmente).
Verificado no site publicado em 21/11/2026 (motor 1.7.2, banco V0073).

## Simples Nacional (NF-e, Anexo I)

Data 2027-03-15, tpDoc 55, CRT 1, `aliquotasEfetivas` = `{"anexoI": {"pCBSSN": "1.1612", "pIBSSN": "0.0129"}}`.

| Item | Entrada | Esperado |
|---|---|---|
| 1 | cAtivSN 01, cClassTrib 000001, NCM 22030000, base 10000.00, IS 0.00 | CBS 116,12 (1,1612%) · IBS 1,29 (0,0129%) · total 117,41 |

Estado: `CALCULADO`. Conferido à mão (10000 × 1,1612% = 116,12; 10000 × 0,0129% = 1,29) e contra a
resposta do motor (1.0.2, banco V0073) em 21/11/2026. As alíquotas são ilustrativas (valores de exemplo do Swagger do motor).
