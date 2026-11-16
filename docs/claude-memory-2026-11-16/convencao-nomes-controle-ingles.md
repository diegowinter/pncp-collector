---
name: convencao-nomes-controle-ingles
description: Convenção do Diego para os repos PNCP - nomes de coluna/variável de controle em inglês, campos do PNCP em snake_case do nome original, VALORES de classificação e status em português
metadata:
  type: feedback
---

- **Nomes de coluna e variáveis nossos** em inglês: `collected_at`, `detail_status`, `triage_status`, `compra_key`.
- **Campos do PNCP**: snake_case do nome original da API (`numeroControlePNCP` -> `numero_controle_pncp`), nunca traduzidos.
- **Valores de classificação** em português: grupos da triagem (`armamento`, `vestuario_operacional`, `viaturas`, `ti_comunicacao`, `drone_bicicleta`, `servicos`), estados (`potencial`, `fora_por_ora`, `indefinido`), clareza (`claro`, `generico`, `remete_ao_edital`).
- **Outros valores de controle** (status): em inglês só se forem novos e não houver precedente; os existentes são em português (`pendente`...) e novos seguem o que já existe.

**Why:** Diego definiu em 2026-11-13. Em inglês nos nomes para separar o que é nosso do que é do PNCP; valores de classificação em português porque o classificador recebe critérios em português e os rótulos são lidos por gente. (O rename `service_selecao` no pesquisa-de-precos-pncp foi aplicação errada dessa regra a um valor.)
**How to apply:** coluna nova = inglês; valor de enum/label = português se classificação ou se já houver precedente em português. Ver [[fase3-triagem-objeto-jev]].
