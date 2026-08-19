# NevePrice

NevePrice é uma aplicação local para análise e comparação de preços de peças, SKUs e produtos em múltiplas fontes gratuitas. O projeto combina um frontend React/Vite com um backend Python/FastAPI, banco SQLite local e dashboards de análise orientados pela IA **NeveAI**.

O foco atual do sistema é comparar ofertas por **lojistas reais** sempre que a fonte permite, em vez de tratar apenas marketplaces como uma loja única. A interface apresenta ranking de produtos, visualização em cards, filtros por marca/loja, painel de preços por item e tabelas comparativas por marketplace/fonte.

## Destaques

- Pesquisa local por produto, SKU ou material.
- Comparação por lojistas/ofertas, incluindo sellers derivados de marketplace quando disponíveis.
- Lista principal em formato ranking, com marca, produto, quantidade de lojas, preço sugerido e menor preço.
- Hover em "Lojas" com top 10 menores preços e lojistas daquele item.
- Filtros por marcas: Consul, Brastemp e Whirlpool.
- Filtros por lojas/fontes: Mercado Livre, Magazine Luiza, Amazon Brasil, Shopee, Leroy Merlin, Dufrio, Friolar, Refrigeração Mota, MG Parts, Gold Service e ComClick.
- Visualização "Design" em cards com imagem, preço e link de acesso ao produto.
- Dashboard por item com gráfico de preços e tabela comparativa por fonte.
- Exportação do Analytics em Excel, com abas de resumo, lojistas e ofertas.
- Pesquisa conjunta de até 30 SKUs, preservando a visão agregada no Analytics e no Excel.
- Condições à vista/a prazo, diferença de pagamento, frete e prazo quando publicados pela fonte.
- CEP logístico configurável e atualização individual dos SKUs do banco local.
- Agendamento manual por dias, janela de horário e repetição configurável de 0 a 12 horas.
- Análises de dados e dashboards gerados através da IA **NeveAI**.
- Banco SQLite local, sem depender de Docker, serviços pagos ou programas externos.
- Scripts `.bat` para preparar e iniciar o projeto no Windows.

## Estrutura

```text
NevePrice/
├── backend/              # FastAPI, SQLAlchemy, scrapers e SQLite local
├── components/           # Componentes React da interface
├── models/               # Tipos TypeScript compartilhados no frontend
├── public/               # Arquivos públicos do Vite
├── src/                  # Entrada da aplicação React
├── static/               # Assets locais, incluindo logo.png
├── preparar.bat          # Prepara ambiente Python/Node
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
2. O backend consulta as lojas diretas em paralelo e agrega Buscapé/Zoom uma única vez por pesquisa.
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
preparar.bat
```

O script:

- Verifica Python.
- Usa Node/npm do sistema se houver versão compatível.
- Baixa Node.js portátil em `.tools/node/` quando Node não existir ou for antigo.
- Aceita `python` ou `py`, escolhendo automaticamente o disponível.
- Cria `backend\.venv` se necessário.
- Atualiza o `pip` no ambiente virtual.
- Instala dependências Python do backend.
- Executa `npm install` na raiz do frontend.
- Cria arquivos `.env` locais sem sobrescrever configurações válidas.
- Usa caminhos relativos, funcionando mesmo com espaços no nome da pasta.

`.tools/` é ignorado pelo Git; o Node portátil fica apenas na máquina local.

## Iniciar Localmente

Execute:

```bat
iniciar.bat
```

Endereços locais:

- Frontend: `http://127.0.0.1:5173`
- Backend: `http://127.0.0.1:8000`

O script inicia frontend e backend, aguarda o backend ficar disponível e abre o navegador automaticamente.

## Variáveis de Ambiente

Frontend:

```text
VITE_API_URL=http://127.0.0.1:8000
```

Backend:

```text
DATABASE_URL=sqlite:///precos.db
MERCADO_LIVRE_ACCESS_TOKEN=
```

Se `DATABASE_URL` não for informado, o backend usa automaticamente `backend/precos.db`.
O token do Mercado Livre é opcional; quando informado, habilita a API oficial autenticada antes dos fallbacks gratuitos.

## Endpoints Principais

```text
GET /              # Status da API
GET /products/search?q=SKU_OU_TERMO
POST /products/search/batch
POST /products/batch-search
GET /products/skus/status
GET|PUT /products/schedule
POST /products/export/analytics
```

A busca retorna:

- `results`: ofertas encontradas.
- `comparison`: linhas comparativas agrupadas.
- `stores`: disponibilidade por fonte.
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
