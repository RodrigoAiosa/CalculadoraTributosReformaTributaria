# Calculadora de Tributos — Reforma Tributária (CBS/IBS)

Interface web + API em Python (Vercel) sobre os **motores oficiais da Receita Federal/Serpro**
(organização [ReceitaFederal](https://github.com/orgs/ReceitaFederal/repositories) no GitHub, licença MIT):

- [`calculadora-rtc-regime-regular`](https://github.com/ReceitaFederal/calculadora-rtc-regime-regular) — CBS, IBS e Imposto Seletivo (regime geral)
- [`calculadora-rtc-simples-nacional`](https://github.com/ReceitaFederal/calculadora-rtc-simples-nacional) — CBS e IBS no Simples Nacional

Os motores são Java 25 + Spring Boot + SQLite e **não rodam em Vercel Functions**. Por isso o sistema tem duas partes:

```
navegador ──► Vercel (public/ + api/index.py, FastAPI) ──HTTP──► motores oficiais (engine/, Docker no Render)
```

A API Python valida a entrada, repassa ao motor (com 1 retry para o motor "acordando"), traduz erros
e limita as consultas de dados abertos a uma lista fechada. **Nenhuma regra tributária é reimplementada aqui.**

## Estrutura

| Pasta/arquivo | O que é |
|---|---|
| `engine/` | Dockerfile que clona os repositórios da Receita, gera o banco com Flyway, compila e sobe o motor escolhido por `ENGINE=regular\|simples` |
| `render.yaml` | Blueprint do Render com os dois serviços |
| `docker-compose.yml` | Sobe os dois motores localmente |
| `api/index.py` | API FastAPI (Vercel Function) |
| `public/index.html` | Interface (abas Regime regular e Simples Nacional) |
| `tests/` | Testes da API com os motores simulados |

## Rodar localmente

```bash
# 1) motores oficiais (primeiro build demora: baixa dependências e gera o banco)
docker compose up --build          # regular :8080, simples :8082

# 2) API + front
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
export ENGINE_REGULAR_URL=http://localhost:8080 ENGINE_SIMPLES_URL=http://localhost:8082
uvicorn api.index:app --reload --port 3000   # a API; o front é public/index.html (use `vercel dev` para ver tudo junto)

pytest                              # testes (não precisam dos motores)
```

## Deploy

1. **Motores no Render:** *New → Blueprint*, aponte para este repositório (usa `render.yaml`). Anote as duas URLs
   (`https://calculadora-motor-regular.onrender.com` e `...-simples...`). Verifique no Swagger
   (`<url>/api/swagger-ui.html`) se subiu.
2. **Front/API na Vercel:** importe o repositório e defina as variáveis de ambiente
   `ENGINE_REGULAR_URL` e `ENGINE_SIMPLES_URL` (sem barra final e sem `/api`). Deploy.
3. Abra a URL da Vercel e use *Carregar exemplo* → *Calcular*.

## Importação de produtos (.xlsx)

Na aba *Regime regular* o usuário sobe uma planilha em vez de digitar os itens (a digitação manual fica recolhida, para poucos itens).

- **Colunas obrigatórias:** `Tipo` (NCM ou NBS), `NCM / NBS`, `CST`, `cClassTrib`, `Base de cálculo`. Opcionais: `Descrição`, `Quantidade`, `Unidade`.
- Limites: 5.000 produtos e 10 MB. A API divide em blocos de 100 itens (uma NF-e aceita até 990) e soma os totais.
- Linhas inválidas são listadas com o número da linha e **nada é enviado ao motor**. Zeros à esquerda do NCM perdidos pelo Excel são recompostos.
- **Produtos sujeitos a Imposto Seletivo** exigem dados de IS na operação e voltam com erro `REG-013`; ainda não há colunas de IS na planilha.
- `GET /api/modelo.xlsx` baixa o modelo; `public/exemplos/produtos-1000.xlsx` é um exemplo com 1.000 produtos
  (gerado por `tools/gerar_exemplo.py` a partir da base oficial e validado no motor: 1.000/1.000 calculados em 21/11/2026).

## Pontos de atenção

- **Plano free do Render** (512 MB, dorme por inatividade): a 1ª chamada pode levar ~1 min. Para uso real, use plano pago.
- **Os motores ficam públicos** (não têm autenticação — o Spring dos projetos libera todas as requisições). Qualquer um que
  descubra a URL pode chamá-los diretamente. Se isso importar, coloque um proxy/WAF na frente ou restrinja por IP.
- **Alíquotas:** o motor do regime regular exige `aliquotasNominais` a partir de 2027 e as proíbe antes; para 2026 há
  alíquotas de referência cadastradas. Resultados com alíquotas informadas saem como `CALCULO_SIMULADO`.
- **Atualizações da Receita:** o `Dockerfile` clona `main` por padrão. Para builds reproduzíveis, fixe tags com
  `REGULAR_REF`/`SIMPLES_REF` (build args) e refaça o deploy quando a Receita publicar versões novas.
- Os valores calculados são **simulações** e não substituem a apuração oficial.

## Importação de planilha — Simples Nacional

Na aba **Simples Nacional**, envie um `.xlsx` (`POST /api/simples-nacional/importar`). Colunas obrigatórias:
**Tipo** (NCM ou NBS), **NCM / NBS**, **cAtivSN**, **cClassTrib** e **Base de cálculo**; opcionais: Descrição e Valor IS.
Data, documento (tpDoc), CRT e alíquotas efetivas (JSON) vêm do formulário e valem para todos os itens, que são
enviados ao motor em blocos de 100. Só documentos com CRT (55, 65, 57, 67, 63, 93, 62): a NFS-e (91) aceita 1 item por nota.

- Modelo: `/api/modelo-simples.xlsx`. Exemplo: `/exemplos/simples-1000.xlsx` (1.000 produtos de NF-e, atividades 01/02,
  Anexo I; validado no motor real: 1000/1000 `CALCULADO`). Gerador: `tools/gerar_exemplo_simples.py`.
- Cada anexo usado pelas atividades da planilha precisa constar nas alíquotas efetivas; a atividade 09 exige `fatorR`.
- O motor devolve `NAO_CALCULADO` (sem erro) para atividade não admitida no tpDoc; confira a coluna Estado.
- Pares recusados pelo motor no exemplo: cClassTrib 200044 não vale para tpDoc 55 (VSN-022) e alguns NCM
  sem vínculo com 200003/200004/200034 (VSN-047) — foram retirados.

## Leitura de XML de NF-e/NFC-e

Aba **XML de NF-e** (`POST /api/xml/importar`): envie vários `.xml` ou um `.zip` (até 200 notas, 40 MB). Para cada nota a
ferramenta lê data (`dhEmi`), município (`cMunFG`), UF do emitente, CRT, e por item NCM, `CST`, `cClassTrib`, base e alíquotas
(`pCBS`, `pIBSUF`, `pIBSMun`), calcula no motor (CRT 3 → regime regular; CRT 1/2/4 → Simples, que exige as alíquotas efetivas
e usa o cAtivSN informado) e **compara com os valores destacados** (`vCBS`, `vIBS`). Situações por item: `CONFERE`,
`DIVERGENTE` (acima da tolerância, padrão R$ 0,01), `SEM_DESTAQUE`, `NAO_CALCULADO` (erro do motor) e `PULADO` (sem NCM de
8 dígitos ou sem grupo IBSCBS). XML com DOCTYPE/ENTITY é recusado, e um XML ruim não derruba o lote.

- Aceita modelos 55 e 65. Não lê CT-e, NFS-e nem eventos. A base usada é o `vBC` do grupo IBSCBS, ou vProd+vFrete+vSeg+vOutro−vDesc.
- Exemplo: `/exemplos/nfe-exemplo.zip` (3 notas de homologação: confere, com divergência plantada e Simples).
  Gerador: `tools/gerar_xml_exemplo.py`; os valores destacados vêm de `tools/valores-exemplo-xml.json` (calculados pelo motor).

## Andamento em tempo real e desempenho

As três importações aceitam o campo `progresso=1`: em vez de um JSON no fim, o servidor transmite linhas NDJSON
(`{"e":"inicio","total":N}`, `{"e":"prog","feito":K}`, `{"e":"fim","dados":{...}}` ou `{"e":"erro","status":...}`).
A tela usa isso para mostrar o percentual do fluxo inteiro (envio 10% + leitura 4% + cálculo 84% + finalização 2%) e o
tempo restante. Sem `progresso`, o comportamento é o de antes (JSON único). Erros de validação continuam HTTP 4xx normais.

- O cálculo vai ao motor em blocos de 50 itens, 3 chamadas simultâneas (`CHUNK`, `WORKERS` em `api/index.py`).
- Medido ao vivo: ~20 ms por item e **sem ganho com chamadas paralelas** (o motor no plano gratuito do Render tem pouca CPU),
  então 1.000 itens levam ~20 s. Para acelerar de verdade, o caminho é um plano pago do Render para os motores (mais CPU,
  e sem dormir): o tempo cai em proporção à CPU e a espera de 1–3 min do primeiro acesso desaparece.
