Usuário: "Quando transformo um orçamento em venda, o produto sai do estoque?"
IA (RAG errado): "Não, a simples transformação não reserva..."
Auditor envia correção: "A resposta é SIM, o produto é retirado do estoque"
  → Salva no banco (origem='auditor')
  → Envia pro chat do usuário via SSE
  → ⭐ NOVO: Injeta em sessions[thread_id] como:
    { role: "system", content: "[CORREÇÃO DO SUPORTE HUMANO]: A resposta é SIM..." }
Usuário pergunta outra coisa sobre o mesmo tema...
  → rewrite_query_with_context vê a correção no histórico (regra 6 nova)
  → build_messages extrai a correção e monta um bloco:
    "[CORREÇÕES DO SUPORTE HUMANO — VERDADE ABSOLUTA]
     [CORREÇÃO]: A resposta é SIM, o produto é retirado do estoque
     [FIM DAS CORREÇÕES]
     REGRA CRÍTICA: Priorize as correções do suporte humano sobre o contexto dos manuais."
  → OpenAI recebe o contexto do RAG + a correção do auditor com prioridade máxima
  → Resposta seguinte respeita a correção humana ✅