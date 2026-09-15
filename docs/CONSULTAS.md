# Consultas de referência (fase 2)

Queries SQL sobre o que o detalhamento grava. Todas assumem a convenção do README (§ Banco):
colunas do PNCP em snake_case, controle nosso em inglês.

## Preço de referência por item

O vencedor (`is_winner`) com fallback para o valor estimado quando o item homologado não tem
resultado gravado. É a regra que o outro pipeline aplicava na coleta; aqui é aplicada na leitura,
para que mudar a regra não exija recoletar.

```sql
SELECT i.compra_key,
       i.numero_item,
       i.descricao,
       i.unidade_medida,
       i.quantidade,
       COALESCE(r.valor_unitario_homologado, i.valor_unitario_estimado) AS preco_unitario,
       CASE WHEN r.valor_unitario_homologado IS NOT NULL THEN 'homologado' ELSE 'estimado' END AS origem_preco,
       r.ni_fornecedor,
       r.nome_razao_social_fornecedor,
       r.data_resultado,
       c.url_pncp
FROM itens i
JOIN compras c ON c.compra_key = i.compra_key
LEFT JOIN itens_resultados r
       ON r.compra_key = i.compra_key
      AND r.numero_item = i.numero_item
      AND r.is_winner
WHERE i.situacao_compra_item_nome = 'Homologado';
```

## Itens de um documento

Ata ou contrato → compra → itens homologados. Todos os documentos da mesma compra compartilham
os mesmos itens (é por isso que a unidade da fase 2 é a compra).

```sql
-- por ata
SELECT a.numero_controle_pncp_ata, a.data_assinatura, i.*
FROM atas a
JOIN itens i ON i.compra_key = a.compra_key
WHERE a.numero_controle_pncp_ata = '07954480000179-1-025970/2025-000001'
  AND i.situacao_compra_item_nome = 'Homologado'
ORDER BY i.numero_item;

-- por contrato
SELECT k.numero_controle_pncp, k.data_assinatura, i.*
FROM contratos k
JOIN itens i ON i.compra_key = k.compra_key
WHERE k.numero_controle_pncp = '07954480000179-2-031999/2026'
  AND i.situacao_compra_item_nome = 'Homologado'
ORDER BY i.numero_item;
```

## Documentos que nunca fecharam

`detail_status` em `erro` ou `pendente`, com o número de tentativas e o motivo. Um `pendente`
com `compra_key` preenchido está esperando a compra (itens/resultados) fechar; um `pendente` com
`compra_key` nulo nunca passou pelo passo A.

```sql
SELECT 'ata' AS tipo, a.numero_controle_pncp_ata AS id, a.detail_status, a.detail_attempts,
       a.detail_error, a.compra_key, c.items_status, c.results_status, c.results_error
FROM atas a LEFT JOIN compras c ON c.compra_key = a.compra_key
WHERE a.detail_status IN ('erro', 'pendente')
UNION ALL
SELECT 'contrato', k.numero_controle_pncp, k.detail_status, k.detail_attempts,
       k.detail_error, k.compra_key, c.items_status, c.results_status, c.results_error
FROM contratos k LEFT JOIN compras c ON c.compra_key = k.compra_key
WHERE k.detail_status IN ('erro', 'pendente')
ORDER BY detail_attempts DESC, id;
```

## Compras com mais de um resultado por item

O que a decisão D6 (gravar todos os resultados, não só o vencedor) passou a permitir medir:
cadastro de reserva em SRP, ou item com mais de um fornecedor.

```sql
SELECT r.compra_key, r.numero_item, COUNT(*) AS resultados,
       COUNT(*) FILTER (WHERE r.is_winner) AS vencedores,
       MIN(r.valor_unitario_homologado) AS menor_valor,
       MAX(r.valor_unitario_homologado) AS maior_valor
FROM itens_resultados r
GROUP BY r.compra_key, r.numero_item
HAVING COUNT(*) > 1
ORDER BY resultados DESC;
```

## Arquivos de um documento

O que a lista de arquivos devolveu, com a marcação de tipo alvo. Nada é baixado; `url` é o
endereço público do PDF.

```sql
SELECT documento_tipo, documento_id, sequencial_documento, tipo_documento_nome, titulo,
       is_target_type, data_publicacao_pncp, url
FROM arquivos
WHERE documento_id = '07954480000179-2-031999/2026'
ORDER BY sequencial_documento;
```

## Distribuição de status (o mesmo que `pncp-collector status`)

```sql
SELECT 'ata' AS tipo, detail_status, COUNT(*) FROM atas GROUP BY 1, 2
UNION ALL
SELECT 'contrato', detail_status, COUNT(*) FROM contratos GROUP BY 1, 2
ORDER BY 1, 2;

SELECT items_status, results_status, COUNT(*) FROM compras GROUP BY 1, 2 ORDER BY 1, 2;
```
