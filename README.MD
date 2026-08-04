 pkill -f uvicorn && git pull && systemctl start agente-suporte






Como evitar que perguntas repetidas gastem processamento?
Existem dois cenários em que perguntas repetidas chegam ao backend, e podemos tratar ambos sem chamar o NotebookLM:

Cenário 1: Chegou ENQUANTO o NotebookLM ainda está pensando
Como tratamos: A flag active_generations bloqueia a requisição e retorna o aviso de aguarde imediatamente. Zero chamadas ao NotebookLM.
Cenário 2: Chegou LOGO APÓS a resposta anterior ter finalizado (ex: retry de 60s do WebView2)
Quando a primeira resposta termina, ela fica salva no histórico da sessão (sessions[thread_id]).
Quando o WebView2 faz o retry de 60s com a mesma pergunta, o backend atualmente acha que é uma pergunta nova e roda o NotebookLM do zero novamente (gastando mais 60-90s de processamento).
Como podemos tratar (Deduplicação / Cache por Sessão): Antes de chamar o NotebookLM, o backend verifica se a user_message é exatamente igual à última pergunta feita na mesma thread. Se for igual, o backend pega a resposta que já foi gerada há pouco e a retorna instantaneamente (em 0.01 segundo), sem reprocessar nada!






Identifiquei exatamente a causa das duas bolhas de pergunta do usuário (IDs 7921 e 7922 no banco)!

O que estava acontecendo nos bastidores (Log + Banco de Dados):
Às 16:09:29: O usuário enviou a mensagem via /chat/stream.

O backend criou o registro 7921 no banco de dados (origem = 'usuario').
O backend marcou a thread como ativa (active_generations) e iniciou a busca no NotebookLM.
Às 16:10:31 (62 segundos depois):

O cliente WebView2 cortou a conexão SSE por causa do timeout padrão de 60s do browser.
O widget acionou o fallback via HTTP POST /chat.
A rota /chat interceptou o bloqueio (if thread_id in active_generations:).
O problema: Dentro desse bloco de interceptação, o backend chamava save_user_message_sync(...). Isso gravava um segundo INSERT no banco de dados (ID 7922) com a mesma pergunta do usuário!
Por haver 2 registros de usuário no banco, a tela de auditoria e o chat renderizavam os 2 balões vermelhos idênticos.
Solução Aplicada em 

main.py
Removi o comando save_user_message_sync de dentro dos blocos de interceptação de duplicatas (/chat e /chat/stream).

Agora, quando uma requisição duplicada/fallback chegar enquanto o NotebookLM ainda está processando:

O backend bloqueia a requisição e exibe no log [DUPLICATE] Requisição duplicada ignorada.
NENHUM registro duplicado é criado no banco de dados (ficará apenas a mensagem original 7921).
A interface e a auditoria exibirão apenas 1 balão do usuário e 1 resposta do agente.
As alterações já estão aplicadas no 

main.py
. Quando puder, efetue o deploy do backend para validar!