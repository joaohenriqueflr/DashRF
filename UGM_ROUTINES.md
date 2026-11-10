# UGM Routines

A estrutura usa cinco tabelas para separar configuração, permissões, execuções e conversa. O horário pertence à atividade; a conclusão pertence à execução. Não há uma tabela intermediária de etapas ou um segundo registro da mesma conclusão.

## Estrutura SQLite

| Tabela | Conteúdo |
|---|---|
| `UGM_ROUTINES` | Nome, descrição, grupo, layout, ativação e ordem das rotinas. |
| `UGM_ROUTINE_ACTIVITIES` | Definição completa da atividade: prompt, workflow, fontes, filtros de ativos, horário, recorrência, ação conclusiva, ativação e ordem. |
| `UGM_ROUTINE_ACTIVITY_TOOLS` | Uma permissão por ferramenta e atividade, com política de revisão, ativação e opções específicas. |
| `UGM_ROUTINE_EXECUTIONS` | Uma tentativa de ação: usuário, data, entrada, resultado, status e indicação de que conclui a atividade. |
| `UGM_ROUTINE_CHAT` | Uma interação legível por mensagem do usuário, com resposta e vínculo opcional à execução. |

`AI_PROMPTS` continua sendo o repositório compartilhado de prompts versionados. `AI_REQUEST_LOG` continua registrando todas as chamadas ao SDK. As tabelas operacionais de cotações e devedores também permanecem separadas: seus dados têm finalidade própria e são usados por outras partes da aplicação.

Não existem mais `UGM_ROUTINE_SCHEDULE`, `UGM_ROUTINE_ACTIVITY_COMPLETIONS`, `UGM_ROUTINE_TOOLS` ou `ID_STEP`. O registro da ferramenta na atividade já é sua permissão; não há outro cadastro de permissões no nível da rotina.

## Atividades e horários

Cada atividade representa uma ação e possui no máximo um horário, na própria coluna `DEADLINE_TIME`. `RECURRENCE` aceita `DAILY`, `WEEKDAYS` e `MANUAL`. `NULL` representa ausência de horário. A recorrência repete a ocorrência em outro dia; ações semelhantes em horários diferentes são atividades diferentes.

A NTN-B começa com:

| ID_ACTIVITY | Atividade | DEADLINE_TIME | PROMPT_KEY | Ferramenta |
|---|---|---|---|---|
| NTNB_RUN_1030 | Run NTN-B — 1ª importação | 10:30 | NTNB_RUN | save_quotes |
| NTNB_RUN_1230 | Run NTN-B — 2ª importação | 12:30 | NTNB_RUN | save_quotes |
| NTNB_RUN_1530 | Run NTN-B — 3ª importação | 15:30 | NTNB_RUN | save_quotes |

As três atividades compartilham a mesma referência de prompt e o mesmo contrato Python da ferramenta. Cada uma possui sua permissão e seu estado diário. O usuário escolhe a atividade; não há avanço automático para a próxima atividade pendente. Os horários são prazos em `America/Sao_Paulo`, sem execução autônoma por relógio.

## Colunas de configuração

As configurações comuns ficam visíveis em `UGM_ROUTINE_ACTIVITIES`:

- `PROMPT_FEATURE`, `PROMPT_KEY`: referência ao prompt ativo em `AI_PROMPTS`.
- `WORKFLOW`: `quote_import` para a importação restrita ou `assistant` para chat com ferramentas.
- `QUOTE_MODE`: `RATE` ou `DI_SPREAD`.
- `SOURCES_JSON`, `INSTRUMENT_TYPES_JSON`, `EXCLUDED_INSTRUMENT_TYPES_JSON`: listas curtas de nomes, sem um objeto genérico envolvendo as configurações.
- `DEADLINE_TIME`, `RECURRENCE`, `COMPLETION_ACTION`: quando a atividade está prevista e qual ação a conclui.
- `OPTIONS_JSON`: somente extensões que não possuem uma coluna própria.

Não há herança implícita de configuração da rotina. O grupo fica em `UGM_ROUTINES.NM_GROUP`; prompt e configuração operacional ficam na atividade. Alterar uma coluna da atividade altera aquela atividade.

As ferramentas são identificadas por `NM_TOOL`. Seus handlers, descrições e schemas de entrada pertencem ao registro Python em `services/routine_tools.py`; schemas extensos e cópias de `HANDLER_KEY` não ficam no SQLite. O backend valida o contrato fixo e as permissões da atividade tanto na preparação quanto na confirmação. Uma escrita sempre exige o botão de confirmação, inclusive se a política da ferramenta tiver sido configurada como `AUTO`.

## Execução e conclusão

`FL_COMPLETES_ACTIVITY=1` indica que aquela tentativa executa a ação conclusiva. `STATUS='SUCCESS'` confirma seu sucesso. O índice parcial `idx_activity_daily_completion` permite apenas uma execução com essas duas condições por `(ID_ACTIVITY, DT_REF)`.

Assim, a conclusão, seu usuário, horário e resultado ficam no mesmo registro de execução. Consultas auxiliares podem ter sucesso sem concluir a atividade. A gravação operacional e a mudança para `SUCCESS` acontecem na mesma transação. A proteção de unicidade também existe no SQLite, além da verificação no serviço.

Falhas e cancelamentos permitem nova tentativa. Duas propostas concorrentes não podem concluir a mesma atividade duas vezes no mesmo dia. Reconfirmar a mesma execução concluída é idempotente. Ao concluir uma atividade, outra importação requer selecionar outra atividade; um dia adicional tem sua própria ocorrência.

## Consultas para depuração

Configuração completa da agenda NTN-B, sem joins:

```sql
SELECT ID_ACTIVITY, NM_ACTIVITY, DEADLINE_TIME, RECURRENCE,
       PROMPT_FEATURE, PROMPT_KEY, WORKFLOW, QUOTE_MODE, COMPLETION_ACTION
FROM UGM_ROUTINE_ACTIVITIES
WHERE ID_ROUTINE = 'NTNB_RUN' AND FL_ACTIVE = 1
ORDER BY DEADLINE_TIME;
```

Tentativas e conclusões do dia:

```sql
SELECT ID_EXECUTION, ID_ACTIVITY, DT_REF, USERNAME, NM_ACTION,
       STATUS, FL_COMPLETES_ACTIVITY, TS_REQUEST, TS_EXECUTION, ERROR_MESSAGE
FROM UGM_ROUTINE_EXECUTIONS
WHERE DT_REF = '2026-11-09'
ORDER BY ID_EXECUTION;
```

Ferramentas de uma atividade:

```sql
SELECT NM_TOOL, REVIEW_POLICY, FL_ACTIVE, CONFIG_JSON
FROM UGM_ROUTINE_ACTIVITY_TOOLS
WHERE ID_ACTIVITY = 'NTNB_RUN_1030';
```

## Configuração pelo Python

As mesmas funções atendem a atividades semelhantes, sem criar novos callbacks. As chaves comuns do argumento `config` são expandidas para as colunas correspondentes, em vez de serem armazenadas em um único objeto JSON.

```python
from services.routines import RoutineService

engine = RoutineService('data/fixed_income.db')
engine.ensure_schema()
engine.configure_activity(
    'DEBTORS', 'DEBTORS:CLOSING', 'Fechamento diário',
    prompt_key='DEBTORS_CLOSING',
    tools=['query_debtors', 'debtor_report'],
    description='Revisar os casos e preparar o fechamento.',
    deadline='17:00', recurrence='WEEKDAYS',
    completion_action='debtor_report',
    display_order=20,
)
engine.process_routine_message(
    'DEBTORS', 'usuario', 'Prepare o fechamento de hoje.',
    activity_id='DEBTORS:CLOSING',
)
```

O prompt deve estar publicado em `AI_PROMPTS` usando `PromptStore` ou a página de manutenção de IA. Nenhum modelo é fixado no código da rotina. `configure_activity` substitui a lista de ferramentas da atividade; ferramentas mantidas conservam sua política e opções. `configure_activity_schedule` edita diretamente `DEADLINE_TIME`, `RECURRENCE` e `COMPLETION_ACTION`, sem criar outra linha ou identificador de agenda.

## Interface e contratos operacionais

`/routines` mostra o Panorama do dia, com agenda compartilhada, filtros e atenção para atrasos, falhas e confirmações. Cada entrada abre `/routines/workspace?routine=<ID_ROUTINE>&activity=<ID_ACTIVITY>`. A leitura do panorama não opera ferramentas, não confirma propostas e não expõe conversas.

O workspace mantém painéis separados para rotinas e atividades à esquerda. A atividade deve ser selecionada antes de operar. Trocar de rotina limpa a seleção; trocar de atividade limpa anexos e texto ainda não enviado. A conversa é isolada por atividade, usuário e data operacional. Os estados compartilhados são atualizados a cada 15 segundos, sem remontar os cards em edição.

NTN-B e NTN-F são chats restritos de importação. O usuário cola a tabela XP; o backend organiza e valida os dados e prepara um card para confirmação. Perguntas de capacidade são permitidas; perguntas gerais e consultas avulsas não são atendidas. Não há upload nem conclusão manual nesses dois modos. Mensagens como “pode aprovar” não substituem o botão do card.

NTN-B usa taxas decimais: 7,25% corresponde a `0.0725`. Ativos precisam existir na classificação; lado, unidade e vencimento ambíguos exigem esclarecimento. NTN-F recebe o spread original em pontos-base e depende de curva DI e convenção financeira configuradas em `configure_ntnf_market_data`. Sem essa integração, não grava taxas estimadas; taxas derivadas alteradas após a revisão exigem nova confirmação.

Crédito e Devedores usam o mesmo executor e contratos Python. Arquivos estruturados são aceitos apenas nas atividades que usam esse fluxo. `UGM_ROUTINES_DEVEDORES` mantém a chave `(DT_REF, ID_CLIENT)` e a convenção `FL_RESOLVED=0` para resolvido e `1` para devedor. Relatórios não enviam e-mails autonomamente.

O resumo de runs compara todas as atividades da rotina no dia. Cada execução confirmada de `save_quotes` gera um par de séries: Bid contínuo e Ask tracejado. Os vértices seguem os vencimentos da classificação; as taxas são convertidas de decimal para percentual na apresentação. Lacunas e observações repetidas são preservadas, sem interpolação ou média inventada. Propostas pendentes, falhas e cancelamentos não entram no gráfico.

## Conversa e auditoria de IA

`UGM_ROUTINE_CHAT` guarda apenas mensagens legíveis e seus vínculos. Confirmação e cancelamento atualizam a interação correspondente. Tokens, tool calls, reasoning e eventos técnicos não ficam nessa tabela.

Toda chamada ao SDK passa por `OpenAIService` e gera uma linha em `AI_REQUEST_LOG`, antes de acessar o SDK. A resposta, modelo, prompt, uso conhecido e status são registrados na auditoria central. `CHAT_MESSAGE_ID` relaciona chamadas à interação; uma interação pode exigir vários requests. Para analisar uso por atividade, relacione esse identificador com `UGM_ROUTINE_CHAT.ID_MESSAGE`.

Uso ausente continua `NULL`; zero reportado continua zero. Erros e respostas inválidas também são auditados. Processamento determinístico não gera requests fictícios. Se o registro inicial falhar, o SDK não é chamado. Os agregados de uso já migrados permanecem identificados como `LEGACY_AGGREGATE` e não representam chamadas individuais.

## Migração e validação

`services/routine_schema.py` contém o schema, a configuração inicial e um único cutover do formato anterior. O cutover incorpora a agenda à atividade, consolida permissões e marca as execuções conclusivas. Remove as três tabelas redundantes e os IDs de etapas; não mantém tabelas de histórico de agenda.

A reconstrução das tabelas ocorre em uma transação, com verificação de chaves estrangeiras antes do commit. Os textos das mensagens, os requests de IA e os dados operacionais existentes são conservados. Os IDs das mensagens e execuções também são conservados. A aplicação passa a usar somente o formato atual, sem fallback de etapas ou permissões herdadas.

Os testes usam SQLite temporário e SDK simulado. Cobrem a migração atômica, schema com cinco tabelas, permissões, validação dos ativos, revisão, rollback, concorrência, conclusão única, chat e auditoria central, agenda, interface e gráficos. O fixture do schema anterior contém somente DDL, sem dados do usuário.

```sh
python -m pip install -r requirements-ai.txt
python -m unittest discover -s tests -q
```

Validação desta simplificação: **241 testes passaram**. O cutover foi verificado em uma cópia e aplicado ao banco local; reinicializar o schema foi idempotente. O banco mantém cinco tabelas de gerenciamento de rotinas, sem triggers de compatibilidade. Chaves estrangeiras e integridade passaram. Os registros de chat, execuções, cotações, classificação, devedores, requests de IA e prompts foram preservados, removendo apenas os vínculos antigos de etapa. NTN-B continua com três atividades às 10h30, 12h30 e 15h30; o histórico verificado contém três runs, 78 cotações, 13 vértices e seis séries Bid/Ask. A integração confirmou menu, views, polling, manutenção de IA e cinco rotas/layouts HTTP 200.
