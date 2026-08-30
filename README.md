# NeveTracker

NeveTracker é uma aplicação local para análise e comparação de preços de peças, SKUs e produtos em múltiplas fontes. O projeto combina um frontend React/Vite com um backend Python/FastAPI, banco SQLite local e dashboards de análise orientados pela IA **NeveAI**.

O foco atual do sistema é comparar ofertas por **lojistas reais** sempre que a fonte permite, em vez de tratar apenas marketainel de preços por item e tabelas comparativas por marketplace/fonte.

## Destaques

- Pesquisa local por produto, SKU ou material.
- Comparação por lojistas/ofertas, incluindo sellers derivados de marketplace quando disponíveis.
- Lista principal em formato ranking, com marca, produto, quantidade de lojas, preço sugerido e menor preço.
- Hover em "Lojas" com top 10 menores preços e lojistas daquele item.
- Filtros por marcas, lojas, data, faixa de preço, frete, lead time e parcelamento.
- Taxonomia opcional por subfamília, categoria, cluster e mercado Cativo/Competitivo.
- Filtros por lojas/fontes: Mercado Livre, Magazine Luiza, Amazon Brasil, Shopee, Leroy Merlin, Dufrio, Friolar, Refrigeração Mota, MG Parts, Gold Service e ComClick.
- Visualização "Design" em cards com imagem, preço e link de acesso ao produto.
- Dashboard por item com gráfico de preços e tabela comparativa por fonte.
- Exportação do Analytics em Excel, com abas de resumo, lojistas e ofertas.
- Pesquisa conjunta de até 30 SKUs, preservando a visão agregada no Analytics e no Excel.
- Importação de XLSX, XLSM ou CSV com até 5.000 SKUs e metadados de catálogo.
- Fila persistente para extrações grandes, com lotes automáticos de 30 SKUs, retomada após reinício, progresso e cancelamento.
- Condições à vista/a prazo, diferença de pagamento, frete e prazo quando publicados pela fonte.
- CEP logístico configurável e atualização individual dos SKUs do banco local.
- Agendamento manual por dias, janela de horário e repetição configurável de 0 a 12 horas.
- Análises de dados e dashboards gerados através da IA **NeveAI**.
- Banco SQLite local, sem depender de Docker, serviços pagos ou programas externos.
- Scripts `.bat` para instalar e iniciar o projeto no Windows.

## Estrutura

```text
NeveTracker/
├── backend/              # FastAPI, SQLAlchemy, scrapers e SQLite local
├── components/           # Componentes React da interface
├── models/               # Tipos TypeScript compartilhados no frontend
├── public/               # Arquivos públicos do Vite
├── src/                  # Entrada da aplicação React
├── static/               # Assets locais, incluindo logo.png
├── instalar.bat          # Instala/repara Python, Node e dependências
├── iniciar.bat           # Inicia backend e frontend localmente
├── package.json          # Frontend React/Vite na raiz
└── README.md
```

## Tecnologias

Frontend:

- React
- Vite
- TypeScript
- Tailwind CSS
- Lucide React
- Recharts

Backend:

- Python
- FastAPI
- SQLAlchemy
- SQLite
- Requests
- BeautifulSoup

## Como Funciona

1. O usuário pesquisa um produto, nome ou SKU.
2. O backend consulta as lojas diretas em paralelo, agrega Buscapé/Zoom e usa Bing Shopping e descoberta de páginas via DuckDuckGo como fallbacks sem credenciais.
3. As ofertas são validadas, normalizadas, deduplicadas e agrupadas por similaridade de item, SKU e marca.
4. A comparação prioriza lojistas/ofertas, não apenas o nome do marketplace.
5. Quando uma fonte sofre bloqueio temporário, a última oferta local daquela loja permanece disponível com sua data original.
6. O frontend renderiza ranking, cards e dashboards.
7. A camada **NeveAI** organiza a análise visual, os indicadores e os dashboards comparativos exibidos na interface.

## Lojas e Fontes

O projeto trabalha com as seguintes fontes:

- Mercado Livre
- Shopee
- Leroy Merlin
- Amazon Brasil
- Magazine Luiza
- Dufrio
- Friolar
- Refrigeração Mota
- MG Parts
- Gold Service
- ComClick
- Bing Shopping (fallback sem chave)
- DuckDuckGo para descoberta de páginas de produto (fallback sem chave)
- Bright Data SERP API (opcional, com chave e controle de consumo)
- Apify Google Shopping Actor (opcional, com token e controle de consumo)
- Serper Google Shopping (opcional, com chave e cota gratuita)
- DataForSEO Merchant API (opcional, pay-as-you-go)
- SearchAPI Google Shopping + Amazon Brasil (opcional, com chave)

As fontes são gratuitas e podem bloquear, limitar ou omitir informações em alguns momentos. Quando não é possível obter o nome real do lojista, o sistema usa o melhor identificador gratuito disponível, como ID de anúncio, seller ID ou origem da oferta.

## Banco de Dados

Por padrão, o backend usa SQLite local em:

```text
backend/precos.db
```

As tabelas são criadas automaticamente ao iniciar o backend. O sistema também mantém uma lista de SKUs conhecidos e pode pré-carregar dados para acelerar consultas futuras.

Para pré-carregar SKUs conhecidos:

```bat
cd backend
.venv\Scripts\python.exe preload_skus.py
```

Para forçar nova coleta:

```bat
cd backend
.venv\Scripts\python.exe preload_skus.py --force
```

## Preparar no Windows

Execute na pasta raiz:

```bat
instalar.bat
```

O script:

- Detecta Python 3.11 e o instala automaticamente quando necessário.
- Tenta primeiro o WinGet e usa o instalador oficial assinado do Python como fallback.
- Reutiliza o Node.js portátil existente ou baixa automaticamente uma versão LTS compatível em `.tools/node/`.
- Usa um Node/npm global compatível como alternativa quando o download portátil não estiver disponível.
- Cria `backend\.venv` se necessário.
- Atualiza o `pip` no ambiente virtual.
- Instala dependências Python do backend.
- Executa `npm ci` com fallback para `npm install`.
- Valida o build do frontend e a importação completa do backend.
- Cria arquivos `.env` locais sem sobrescrever configurações válidas.
- Registra diagnóstico em `.logs/install.log`.
- Usa caminhos absolutos com tratamento de espaços no nome da pasta.

`.tools/` é ignorado pelo Git; o Node portátil fica apenas na máquina local.

## Iniciar Localmente

Execute:

```bat
iniciar.bat
```

Endereços locais:

- Frontend: `http://127.0.0.1:5173`
- Backend: `http://127.0.0.1:8000`

O script repara o ambiente automaticamente se necessário, escolhe portas alternativas quando há conflito, inicia frontend e backend, valida os dois serviços, abre o navegador e mantém o console aberto. Os logs ficam em `.logs/`.

O acesso inicial é `DEUSM2` com senha `1234`. Essa conta é o perfil principal e pode criar usuários, redefinir senhas e conceder acesso administrativo. As senhas são derivadas e armazenadas no SQLite; sessões possuem token próprio e expiração.

## Variáveis de Ambiente

Frontend:

```text
VITE_API_URL=http://127.0.0.1:8000
```

Backend:

```text
DATABASE_URL=sqlite:///precos.db
MERCADO_LIVRE_ACCESS_TOKEN=
NEVETRACKER_MAX_PARALLEL_SKUS=2
NEVETRACKER_QUEUE_SKU_PAUSE_SECONDS=0.75
NEVETRACKER_QUEUE_BATCH_PAUSE_SECONDS=5
NEVETRACKER_EXTERNAL_FALLBACK_MIN_RESULTS=8
BRIGHT_DATA_ENABLED=false
BRIGHT_DATA_API_KEY=
BRIGHT_DATA_SERP_ZONE=
BRIGHT_DATA_USAGE_MODE=fallback
BRIGHT_DATA_MAX_RESULTS=20
APIFY_ENABLED=false
APIFY_API_TOKEN=
APIFY_ACTOR_ID=damilo/google-shopping-apify
APIFY_USAGE_MODE=fallback
APIFY_MAX_RESULTS=20
SERPER_ENABLED=false
SERPER_API_KEY=
SERPER_USAGE_MODE=fallback
SERPER_MAX_RESULTS=20
DATAFORSEO_ENABLED=false
DATAFORSEO_LOGIN=
DATAFORSEO_PASSWORD=
DATAFORSEO_USAGE_MODE=fallback
DATAFORSEO_MAX_RESULTS=20
SEARCHAPI_ENABLED=false
SEARCHAPI_API_KEY=
SEARCHAPI_USAGE_MODE=fallback
SEARCHAPI_MAX_RESULTS=20
MERCADO_LIVRE_API_ENABLED=false
```

Se `DATABASE_URL` não for informado, o backend usa automaticamente `backend/precos.db`.
O token do Mercado Livre é opcional; quando informado, habilita a API oficial autenticada antes dos fallbacks gratuitos.
Os fallbacks de Bing Shopping e DuckDuckGo não exigem configuração ou chave. Eles passam pelas mesmas validações de SKU e não substituem Mercado Livre, lojas diretas, Buscapé ou Zoom.
As variáveis da fila são opcionais. Os padrões limitam a duas pesquisas de SKU simultâneas, aguardam 0,75 segundo entre SKUs e fazem uma pausa de 5 segundos a cada lote de 30.

Administradores podem configurar todos os conectores em `Usuario > Integracoes`. As credenciais sao salvas apenas em `backend/.env`, ficam fora do Git e nunca retornam completas para o frontend. No modo `Completar lacunas`, a fonte de descoberta so e chamada quando falta alguma loja-alvo ou quando as fontes gratuitas retornam menos ofertas que o limite de fallback. No modo `Toda extracao`, ela e chamada em cada atualizacao com APIs. O limite de resultados e aplicado por SKU e por provedor.

O token do Mercado Livre alimenta somente a API oficial dessa loja. A SearchAPI consulta Google Shopping, expande até dois produtos em ofertas diretas por lojista e também pesquisa a Amazon Brasil; por isso pode consumir até quatro consultas por SKU. APIs oficiais de seller da Amazon, Magalu e Shopee não são usadas para inteligência competitiva porque restringem a consulta ao catálogo autorizado da própria conta.

### Protecao obrigatoria das cotas gratuitas

Toda chamada paga em potencial passa primeiro por uma reserva atomica no arquivo local `backend/provider_quota.db`. Se nao houver saldo gratuito suficiente, o backend bloqueia a chamada antes de acessar o provedor. O contador e compartilhado entre filas e processos, persiste entre reinicializacoes e nao e estornado em falhas ou timeouts, pois uma tentativa pode ser faturada mesmo sem retornar dados.

| Provedor | Trava local | Renovacao |
| --- | ---: | --- |
| Bright Data | 5.000 requisicoes | Mensal |
| Apify | 1.400 resultados (ate US$ 4,90 no Actor padrao) | Mensal |
| Serper | 2.500 consultas | Vitalicia |
| DataForSEO | 1.000 itens reservados (ate US$ 1 de teste) | Vitalicia |
| SearchAPI | 100 consultas | Vitalicia |
| Mercado Livre | Sem tarifa de uso publicada | Nao se aplica |

O saldo aparece em `Usuario > Integracoes`. As cotas vitalicias nao podem ser reiniciadas pela interface. Para contas que ja tenham sido usadas fora do NeveTracker, confirme o consumo anterior no painel do provedor antes de ativar a chave: a trava controla integralmente o que o NeveTracker consome, mas nao enxerga chamadas feitas por outros sistemas com a mesma credencial.

## Endpoints Principais

```text
GET /              # Status da API
GET /products/search?q=SKU_OU_TERMO
POST /products/search/batch
POST /products/batch-search
POST /products/extraction-jobs
POST /products/extraction-jobs/database
GET /products/extraction-jobs/active
GET /products/extraction-jobs/{id}
POST /products/extraction-jobs/{id}/cancel
GET /products/skus/status
DELETE /products/skus/{sku}
GET /products/import/skus/template
POST /products/import/skus
GET|PUT /products/schedule
POST /products/export/analytics
POST /auth/login
POST /auth/logout
GET /auth/me
GET|POST /auth/users
PATCH|DELETE /auth/users/{id}
GET /integrations
PUT /integrations/{provider}
```

A busca retorna:

- `results`: ofertas encontradas.
- `comparison`: linhas comparativas agrupadas.
- `stores`: disponibilidade por fonte.
- `source_health`: cobertura e estado de cada fonte na consulta.
- `message`: resumo da busca.

## Observações Importantes

- O projeto é pensado para execução local e gratuita.
- Não usa Docker por padrão.
- Não exige permissões de administrador.
- Não usa serviços pagos obrigatórios.
- Scraping e consultas gratuitas podem sofrer bloqueios temporários por parte das lojas.
- Frete e prazo dependem do CEP e da informação publicada pelo lojista; ausência é retornada como dado não informado.
- O cache local permanece identificado e serve como contingência, sem ser apresentado como coleta nova.
- Dados salvos no SQLite podem ser reaproveitados para reduzir novas coletas.

## Licença

Consulte [LICENSE.txt](LICENSE.txt).
