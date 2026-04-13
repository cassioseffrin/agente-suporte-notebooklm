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





  Da forma como implementamos agora (injetando no array sessions), essa "Injeção de Verdade" tem escopo local. Ela vai corrigir o rumo apenas daquela conversa (thread_id) específica enquanto ela durar.

Se um novo usuário (ou o mesmo usuário em um novo chat amanhã) fizer exatamente a mesma pergunta:

O backend vai começar com um histórico limpo.
Vai consultar o NotebookLM.
O NotebookLM vai ler o manual (onde a informação ainda está duvidosa ou errada).
A IA vai errar a resposta de novo.
Como resolvemos isso de forma global em sistemas empresariais?
Temos duas estratégias principais para tornar a intervenção do Auditor permanente para todas as conversas futuras:

1. Correção na Fonte (Re-Indexação RAG) O fluxo ideal é usar essa falha identificada pelo auditor para corrigir o documento original que gerou o erro (um PDF de manual de contabilidade, por exemplo) e enviar de volta ao NotebookLM.

Pró: Resolve o problema na raiz e o modelo continua com o contexto enxuto.
Contra: Nem sempre é rápido editar um manual oficial e subir de novo pra API do Google só pra ajustar um errinho num cenário isolado.
2. Memory Layer / Base de Conhecimento Dinâmica (O Mais Usado) Nós podemos criar uma tabela no PostgreSQL chamada agent_global_rules (ou Correções Globais). Quando o auditor envia uma mensagem na tela, podemos colocar um checkbox no painel do Dashboard: [x] Salvar como Regra Global de Negócio.

Se marcado, o backend salva essa correção no banco e injeta ela diretamente no System Prompt do Agente de todas as consultas de todos os usuários dali em diante:

[REGRAS DE NEGÓCIO ATUALIZADAS (APLICADAS A TODOS)]
1. A simples transformação do orçamento em venda retira o produto do estoque.
2. O sistema não permite faturar sem informar a modalidade de frete.
Dessa forma, sempre que o NotebookLM buscar um trecho do manual antigo, o próprio sistema injeta a nova regra sobrepondo a desatualizada, garantindo que a IA nunca mais erre aquilo, mesmo que o PDF original não seja modificado de imediato.

Faz sentido? Gostaria de evoluir e salvar essa injeção de verdade local também numa base global para corrigir futuras conversas?

