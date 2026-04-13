```mermaid
sequenceDiagram
    autonumber

    actor U as Usuário
    actor A as Auditor
    participant Chat as Cliente Arpa<br/>(Portal Representante)
    participant Audit as Auditor Suporte<br/>(Dashboard)
    participant Backend as Backend API<br/>(Python)
    participant DB as PostgreSQL
    participant NLM as NotebookLM CLI<br/>(subprocess)
    participant OAI as OpenAI API<br/>(gpt-4o-mini)

    rect rgba(0, 50, 100, 0.4)
        Note over U,OAI: FASE 1 — Abertura do Chat e Configuração Inicial
        U->>Chat: Clica no FAB (Assistente IA)
        Chat->>Chat: Verifica localStorage "ia_consent"

        alt Consentimento ainda não dado
            Chat-->>U: Exibe tela de Aviso de Privacidade e IA
            U->>Chat: Clica "Aceito"
            Chat->>Chat: Salva "ia_consent=true"
        end

        Chat->>Backend: GET /agents
        Backend->>DB: SELECT id, name, title FROM agent WHERE active = TRUE
        DB-->>Backend: Lista de agentes
        Backend-->>Chat: { agents: [...] }
        Chat-->>U: Exibe tela de seleção de Agente
        U->>Chat: Seleciona um Agente
    end

    rect rgba(0, 100, 50, 0.4)
        Note over U,OAI: FASE 2 — Criação da Thread e Conexão de Eventos
        Chat->>Backend: GET /createNewThread?agentName=...
        Backend->>Backend: Gera UUID (threadId) e inicializa sessão
        Backend->>DB: UPSERT user
        Backend->>DB: INSERT INTO thread e chat
        Backend-->>Chat: { threadId: "uuid-xxxx" }
        
        Chat->>Backend: GET /thread/{threadId}/user-events (Conecta no SSE)
        Backend->>Backend: Atualiza status de presença para Online
        Backend-->>Chat: event: connected
        Chat-->>U: Exibe mensagem de boas-vindas
    end

    rect rgba(100, 70, 0, 0.4)
        Note over U,OAI: FASE 3 — Envio de Mensagem e Resposta (SSE Streaming)
        U->>Chat: Digita mensagem
        Chat->>Chat: setIsTyping(true) / Start AbortController (timeout 240s)

        Chat->>Backend: POST /chat/stream { threadId, message, assistantName } (SSE)
        
        rect rgba(100, 50, 0, 0.3)
            Note over Backend,OAI: Etapa 3a — Query Rewriting (timeout 60s)
            Backend-->>Chat: event: status { stage: "rewriting" }
            Chat-->>U: Exibe "Reescrevendo consulta..."
            Backend->>OAI: chat.completions.create (escreve query autocontida)
            OAI-->>Backend: query reescrita
        end

        rect rgba(0, 50, 100, 0.3)
            Note over Backend,NLM: Etapa 3b — Busca RAG NotebookLM (timeout 240s)
            Backend-->>Chat: event: status { stage: "searching" }
            Chat-->>U: Exibe "Buscando nos manuais..."
            Backend->>NLM: notebooklm ask ...
            NLM-->>Backend: Contexto dos manuais
        end

        rect rgba(0, 100, 50, 0.3)
            Note over Backend,OAI: Etapa 3c — Geração Streaming (timeout 120s)
            Backend-->>Chat: event: status { stage: "generating" }
            Chat-->>U: Exibe "Gerando resposta com IA..."
            Backend->>OAI: chat.completions.create (stream=True)
            loop Tokens chegando
                OAI-->>Backend: chunk de texto
                Backend-->>Chat: event: token { text: "..." }
                Chat-->>U: Renderiza texto progressivamente
            end
        end

        alt OpenAI falhou mas NotebookLM respondeu
            Backend-->>Chat: event: fallback { content: "resposta NotebookLM" }
            Chat-->>U: Exibe resposta original dos manuais
        end

        rect rgba(70, 30, 100, 0.4)
            Note over Backend,DB: Etapa 3d — Persistência (assíncrona)
            Backend-->>Chat: event: status { stage: "saving" }
            Backend->>DB: Salva mensagens e cria Subject (assunto curto)
        end

        Backend-->>Chat: event: done { chat_id, content }
        Chat->>Chat: setIsTyping(false)
        Chat-->>U: Exibe resposta final com feedback
    end

    rect rgba(0, 80, 120, 0.4)
        Note over A,U: FASE 4 — Auditoria e Intervenção do Suporte (Injeção da Verdade)
        A->>Audit: Acessa conversa em andamento
        Audit->>Backend: GET /thread/{threadId}/presence (Conecta no SSE)
        Backend-->>Audit: event: presence { online: true }
        Audit-->>A: Mostra indicador verde "Online"
        
        A->>Audit: Digita e envia correção/mensagem para o usuário
        Audit->>Backend: POST /thread/{threadId}/auditor-message
        Backend->>DB: Persiste chat com origem='auditor'
        Backend-->>Chat: Pusheia mensagem por GET /user-events -- event: auditor_message
        Backend-->>Audit: Retorna status OK (e new_message pro SSE do Auditor)
        
        Chat-->>U: Renderiza a mensagem do auditor em dourado com ícone humano
        
        Backend->>Backend: Anexa à session em memória como {role: "system", "[CORREÇÃO DO SUPORTE HUMANO]: ..."}
        Note over Backend,OAI: Nas próximas perguntas do Usuário:
        Backend->>OAI: Query Rewrite é instruído a incorporar a correção humana
        Backend->>OAI: build_messages injeta bloco [VERDADE ABSOLUTA...]<br/>com prioridade sobre o contexto RAG
        OAI-->>Backend: Nova geração de IA respeita a correção do auditor!
    end

    rect rgba(100, 0, 0, 0.4)
        Note over U,Chat: TIMEOUT DO CLIENTE — AbortController (240s)
        Note over Chat: Se conexão falhar, tenta fallback POST /chat (não streaming).
    end
```
