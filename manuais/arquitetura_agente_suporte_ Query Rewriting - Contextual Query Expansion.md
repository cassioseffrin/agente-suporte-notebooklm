# Arquitetura - Agente de Suporte Smart (pós Query Rewriting)

## Fluxo Principal por Requisição

```mermaid
sequenceDiagram
    participant User as 📱 App Flutter
    participant API as ⚡ FastAPI Backend
    participant Mem as 🗃️ sessions[threadId]
    participant OAI1 as 🤖 OpenAI<br/>(Query Rewriter)
    participant NLM as 📚 NotebookLM CLI<br/>(RAG - Manuais)
    participant OAI2 as 🤖 OpenAI<br/>(Formatador)

    User->>API: POST /chat {threadId, message}
    API->>API: verify_api_key()

    API->>Mem: lê histórico[-10:] da thread

    alt há histórico na thread
        API->>OAI1: QUERY_REWRITE_PROMPT<br/>+ histórico + pergunta atual
        OAI1-->>API: pergunta autocontida e expandida
        Note over API,OAI1: ex: "pode detalhar isso?"<br/>→ "Como funciona o passo 2 do cadastro de cliente?"
    else primeira mensagem da thread
        API->>API: usa pergunta original (sem reescrita)
    end

    API->>NLM: notebooklm ask "query expandida" -n NOTEBOOK_ID
    NLM-->>API: contexto extraído dos manuais

    alt contexto encontrado
        API->>OAI2: SYSTEM_PROMPT + histórico<br/>+ [CONTEXTO DOS MANUAIS] + pergunta original
        OAI2-->>API: resposta formatada em Markdown
    else sem contexto
        API->>OAI2: SYSTEM_PROMPT + instrução de<br/>"não encontrado nos manuais"
        OAI2-->>API: "Não encontrei informações sobre..."
    end

    API->>Mem: append {user: msg_original} + {assistant: resposta}
    API-->>User: {content: [resposta], images: []}
```

---

## Isolamento de Sessões Multi-Usuário

```mermaid
graph TD
    NLM_NB["📚 NotebookLM Notebook<br/>(único, compartilhado, stateless)"]

    subgraph Backend["⚡ FastAPI Backend - sessions[]"]
        T1["🧵 Thread UUID-A<br/>hist: [Q1,R1,Q2,R2...]"]
        T2["🧵 Thread UUID-B<br/>hist: [Q1,R1...]"]
        T3["🧵 Thread UUID-C<br/>hist: []"]
    end

    U1["📱 Usuário A"] --> T1
    U2["📱 Usuário B"] --> T2
    U3["📱 Usuário C"] --> T3

    T1 -->|"query expandida (stateless)"| NLM_NB
    T2 -->|"query expandida (stateless)"| NLM_NB
    T3 -->|"query original (sem histórico)"| NLM_NB

    NLM_NB -->|"contexto isolado por chamada"| T1
    NLM_NB -->|"contexto isolado por chamada"| T2
    NLM_NB -->|"contexto isolado por chamada"| T3

    style NLM_NB fill:#1e3a8a,stroke:#60a5fa,stroke-width:2px,color:#ffffff
    style T1 fill:#064e3b,stroke:#34d399,color:#ffffff
    style T2 fill:#064e3b,stroke:#34d399,color:#ffffff
    style T3 fill:#064e3b,stroke:#34d399,color:#ffffff
```

---

## Responsabilidades por Componente

| Componente | Papel | Estado |
|---|---|---|
| **Flutter App** | Interface do usuário | stateless |
| **FastAPI Backend** | Orquestrador, autenticação, histórico por thread | **stateful** (RAM) |
| **sessions[threadId]** | Histórico isolado por usuário (até 10 msgs) | em memória |
| **OpenAI - Query Rewriter** | Expande perguntas vagas usando histórico | stateless |
| **NotebookLM CLI** | RAG - busca real nos manuais do sistema | **stateless** |
| **OpenAI - Formatador** | Ajusta gramática, aplica SYSTEM_PROMPT, formata Markdown | stateless |

---

## Por que dois papéis do OpenAI?

```mermaid
graph LR
    A["Pergunta vaga<br/>'pode detalhar isso?'"]
    B["OpenAI<br/>Query Rewriter<br/>temperature=0<br/>max_tokens=256"]
    C["Query autocontida<br/>'Como funciona o passo 2<br/>do cadastro de cliente?'"]
    D["NotebookLM<br/>busca nos manuais"]
    E["Contexto<br/>dos manuais"]
    F["OpenAI<br/>Formatador<br/>SYSTEM_PROMPT<br/>max_tokens=10240"]
    G["Resposta final<br/>em Markdown"]

    A --> B --> C --> D --> E --> F --> G

    style B fill:#78350f,stroke:#fbbf24,color:#ffffff
    style D fill:#4c1d95,stroke:#a78bfa,color:#ffffff
    style F fill:#78350f,stroke:#fbbf24,color:#ffffff
```

> [!NOTE]
> O **Query Rewriter** usa `temperature=0` para garantir resultados conservadores e determinísticos - ele nunca inventa contexto, apenas reorganiza o que já está no histórico da conversa.

> [!IMPORTANT]
> O NotebookLM **sempre recebe uma pergunta isolada e autocontida** - jamais recebe o histórico de conversa diretamente. Isso garante zero contaminação entre sessões de usuários diferentes que compartilham o mesmo notebook.
