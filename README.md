<img width="1400" height="350" alt="teste" src="https://github.com/user-attachments/assets/5c528402-a843-44d6-a976-3216dd77097f" />

---

NeveTracker é uma aplicação local para descoberta, coleta, análise e comparação de preços de peças, SKUs e produtos em múltiplas fontes. O projeto combina frontend React/Vite, backend Python/FastAPI, filas persistentes, banco SQLite local e dashboards de análise orientados pela LLM da **NeveAI**.

O foco atual do sistema é comparar ofertas por **lojistas reais**, mantendo histórico, logística e tabelas comparativas por marketplace e seller.

---

## Destaques

- Pesquisa local por produto, SKU ou material.
- Comparação por lojistas/ofertas, incluindo sellers derivados de marketplace quando disponíveis.
- Lista principal em formato ranking, com marca, produto, quantidade de lojas, preço sugerido e menor preço.
- Hover em "Lojas" com top 10 menores preços e lojistas daquele item.
- Filtros por marcas, lojas, data, faixa de preço, frete, lead time e parcelamento.
- Taxonomia opcional por subfamília, categoria, cluster e mercado Cativo/Competitivo.
- Filtros por lojas/fontes: Mercado Livre, Magazine Luiza, Amazon Brasil, Shopee, Leroy Merlin, Dufrio, Friolar, Refrigeração Mota, MG Parts, Gold Service e ComClick.
- Catálogo em cards com imagem, preço e link de acesso ao produto.
- Dashboard por item com gráfico de preços e tabela comparativa por fonte.
- Exportação do Analytics em Excel, com abas de resumo, lojistas e ofertas.
- Seleção conjunta dos SKUs existentes no banco, preservando a visão agregada no Analytics e no Excel.
- Importação de XLSX, XLSM ou CSV com até 5.000 SKUs e metadados de catálogo.
- Fila persistente para extrações grandes, com lotes automáticos de 30 SKUs, retomada após reinício, progresso e cancelamento.
- Condições à vista/a prazo, diferença de pagamento, frete e prazo quando publicados pela fonte.
- CEP logístico configurável e atualização individual dos SKUs do banco local.
- Agendamento manual por dias, janela de horário e repetição configurável de 0 a 12 horas.
- Analytics determinístico com indicadores, distribuição de preços e comparação por lojista.
- Banco SQLite local, sem Docker ou serviços pagos obrigatórios.
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
- Scrapy
- Playwright (fallback seletivo)

## Como Funciona

1. O usuário pesquisa um produto, nome ou SKU.
2. O backend consulta as lojas diretas em paralelo, agrega Buscapé/Zoom e usa Bing Shopping e descoberta de páginas via DuckDuckGo como fallbacks sem credenciais.
3. As ofertas são validadas, normalizadas, deduplicadas e agrupadas por similaridade de item, SKU e marca.
4. A comparação prioriza lojistas/ofertas, não apenas o nome do marketplace.
5. Quando uma fonte sofre bloqueio temporário, a última oferta local daquela loja permanece disponível com sua data original.
6. O frontend renderiza ranking, cards e dashboards.
7. O frontend organiza os resultados em Ofertas, Catálogo e Analytics, com exportação consolidada para Excel.

### Modos de extração

- `Rápida`: preserva o pipeline de busca por SKU, comparadores, cache e enriquecimento.
- `Precisa`: resolve EAN/MPN/modelo, reutiliza URLs da tabela `product_sources`, coleta a página do produto via API/JSON/Scrapy e redescobre lojas sem coleta bem-sucedida. O scraping de busca de Amazon, Mercado Livre e Magalu avança uma página por termo a cada execução; as páginas de vendedores da Amazon também são percorridas com cursor persistido. Fontes encontradas ficam salvas e entram em lotes rotativos de coleta, inclusive após reiniciar o backend.

No modo preciso, a ordem é API oficial, endpoint JSON da loja, Scrapy HTTP, Playwright seletivo para logística e integração externa como último fallback. Bing Shopping, DuckDuckGo, Buscapé e Zoom participam da descoberta; o preço final é persistido somente depois da coleta da página original quando ela está acessível. Ofertas de vendedores diferentes da mesma loja ou da mesma página permanecem separadas na comparação.

Cada lote preciso coleta até 40 URLs, com até 12 por loja; as demais continuam ativas e são priorizadas nos lotes seguintes pela data da última tentativa. Um job na fila continua o SKU em novos lotes enquanto houver fontes por tentar ou páginas de scraping avançando. Os cursores de páginas concluídas reiniciam a busca depois de 24 horas ou quando a redescoberta é solicitada.

O painel `Extrações > Definições` configura globalmente o modo, a concorrência por domínio, o timeout, a descoberta externa e o fallback Playwright. `Extrações > Execução` aplica essas definições; `Agendamento` automatiza atualizações e `Dados` gerencia os SKUs persistidos.

### Revisão de precisão e diagnóstico

A coleta precisa lê JSON-LD, microdata e RDFa, resolve referências entre produto e oferta e valida identificadores antes de salvar. Produtos recomendados, ofertas agregadas, quantidades incompatíveis, preços condicionados e moedas diferentes de BRL não completam automaticamente a oferta pesquisada.

O fallback de navegador pode recuperar a página do produto e cotar CEP pelos formulários públicos das lojas. Loja Integrada e WooCommerce utilizam seus cálculos de frete em uma sessão anônima; Shopify seleciona a variante correspondente. Atualizações renovam preço e logística, enquanto dados reutilizados mantêm sua data. O modo preciso não chama provedores de descoberta pagos.

Cada lojista permanece associado à sua loja principal, inclusive quando aparece em vários marketplaces. Os lotes continuam enquanto houver trabalho acionável; páginas em pausa e buscas antigas não bloqueiam a retomada. Os controles de navegador para produtos e logística ficam em `Extrações > Definições`.

A [revisão completa](docs/scraping-review.md) registra os métodos pesquisados, as correções, os testes e as limitações observadas. Uma auditoria de amostras sem modificar ofertas no banco pode ser executada com:

```powershell
cd backend
.\.venv\Scripts\python.exe diagnose_scraping.py --browser --shipping --cep 01001-000
```

O relatório padrão é `.logs/scraping-audit.json`. Uma amostra por SKU e loja não representa todos os anúncios disponíveis.

A [auditoria do W11199342](docs/W11199342-coverage.md) detalha vendedores que
faltavam, verificações nas 11 lojas e bloqueios ainda presentes. A descoberta
complementar agora amplia os resultados mesmo quando já existe uma oferta;
buscas públicas por domínio também têm paginação e cursores retomáveis.

A [revisão do Mercado Livre](docs/mercado-livre-review.md) registra a integração
de Patchright e ChompJS, os resultados gravados para três SKUs e a ação
**Integrações > Mercado Livre > Validar navegador** para verificações exigidas pelo site.

Ofertas confirmadas como **Sem estoque** também são armazenadas e exibidas,
inclusive sem preço publicado. Preço ausente permanece vazio, sem virar zero;
essas ofertas ficam fora do menor preço e da média, mas aparecem em Database,
nos resultados e na exportação para Excel. O histórico de preços é preservado.

## Lojas e Fontes

O projeto trabalha com as seguintes fontes:

- Mercado Livre
- Shopee
- Leroy Merlin
- Amazon Brasil
- Magazine Luiza
- Dufrio/Duofrio
- Friolar
- Refrigeração Mota
- MG Parts
- Gold Service
- ComClick
- Bing Shopping (fallback sem chave)
- DuckDuckGo para descoberta de páginas de produto (fallback sem chave)
- Bright Data SERP API (opcional, com chave e controle de consumo)
- Apify Google Shopping Actor (opcional, com token e controle de consumo)
- DataForSEO Merchant API (opcional, pay-as-you-go)

As fontes diretas e os fallbacks sem chave podem bloquear, limitar ou omitir informações. Bright Data, Apify e DataForSEO são integrações opcionais e podem ter custo após suas franquias; o backend aplica travas locais antes de cada consumo. Quando não é possível obter o nome real do lojista, o sistema usa o melhor identificador disponível, como ID de anúncio, seller ID ou origem da oferta.

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

### Arquivos locais

O Git mantém apenas código, configuração de exemplo e documentação. Bancos SQLite, arquivos `.env`, ambientes virtuais, Node portátil, dependências, builds, logs, caches, perfis de navegador e capturas de teste ficam somente na máquina local por meio do `.gitignore`.

Use `backend/.env.example` como referência de configuração. Nunca versione `backend/.env`, credenciais de provedores ou bancos gerados em execução.

## Preparar no Windows

Execute na pasta raiz:

```bat
instalar.bat
```

O script:

- Detecta Python 3.11 e o instala automaticamente quando necessário.
- Tenta primeiro o WinGet e usa o instalador oficial assinado do Python como fallback.
- Reutiliza o Node.js portátil existente ou baixa automaticamente uma versão LTS compatível em `.tools/node/`.
- Confere o SHA-256 do Node portátil com o arquivo oficial publicado em `nodejs.org` antes da extração.
- Usa um Node/npm global compatível como alternativa quando o download portátil não estiver disponível.
- Cria `backend\.venv` se necessário.
- Atualiza o `pip` no ambiente virtual.
- Instala dependências Python do backend.
- Executa `pip check` e compila os módulos Python para detectar instalações incompletas.
- Instala o Chromium gerenciado pelo Playwright e abre uma página de teste para validar o navegador.
- Executa `npm ci` com fallback para `npm install`.
- Executa lint e build do frontend e confirma a geração de `dist/index.html`.
- Valida o backend e as migrações do banco por uma requisição HTTP local com `TestClient`.
- Cria arquivos `.env` locais sem sobrescrever configurações válidas.
- Registra diagnóstico em `.logs/install.log`.
- Usa caminhos absolutos com tratamento de espaços no nome da pasta.
- Registra hashes e artefatos instalados; o `iniciar.bat` chama o reparo automaticamente se algo desaparecer ou mudar.

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

Em uma instalação nova, o acesso inicial é baseado em um perfil de desenvolvedor. Essa conta pode criar usuários, redefinir senhas e conceder permissões. Troque a senha inicial antes do uso real. As senhas são derivadas e armazenadas no SQLite; as sessões possuem token próprio e expiração.

### Perfis de acesso

- `Desenvolvedor`: reservado ao usuário principal; controla integrações, definições avançadas de extração, banco de dados e permissões.
- `Governante`: acessa a operação e a administração, mas não acessa Integrações nem as áreas avançadas Definições e Dados; novos usuários criados por esse perfil são Visualizadores.
- `Visualizador`: acessa as consultas e análises, sem acesso aos modais Integrações, Extrações e Administração.

## Variáveis de Ambiente

Frontend:

```text
VITE_API_URL=http://127.0.0.1:8000
```

Backend:

```text
DATABASE_URL=sqlite:///precos.db
DEFAULT_SHIPPING_ZIP_CODE=
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
DATAFORSEO_ENABLED=false
DATAFORSEO_LOGIN=
DATAFORSEO_PASSWORD=
DATAFORSEO_USAGE_MODE=fallback
DATAFORSEO_MAX_RESULTS=20
MERCADO_LIVRE_API_ENABLED=false
```

Se `DATABASE_URL` não for informado, o backend usa automaticamente `backend/precos.db`.
O token do Mercado Livre é opcional; quando informado, habilita a API oficial autenticada antes dos fallbacks gratuitos.
Os fallbacks de Bing Shopping e DuckDuckGo não exigem configuração ou chave. Eles passam pelas mesmas validações de SKU e não substituem Mercado Livre, lojas diretas, Buscapé ou Zoom.
As variáveis da fila são opcionais. Os padrões limitam a duas pesquisas de SKU simultâneas, aguardam 0,75 segundo entre SKUs e fazem uma pausa de 5 segundos a cada lote de 30.

O perfil Desenvolvedor pode configurar os conectores em `Usuário > Integrações`. As credenciais são salvas apenas em `backend/.env`, ficam fora do Git e nunca retornam completas para o frontend. No modo `Completar lacunas`, a fonte de descoberta só é chamada quando falta alguma loja-alvo ou quando as fontes gratuitas retornam menos ofertas que o limite de fallback. No modo `Toda extração`, ela é chamada em cada atualização com APIs. O limite de resultados é aplicado por SKU e por provedor.

O token do Mercado Livre alimenta somente a API oficial dessa loja. APIs oficiais de seller da Amazon, Magalu e Shopee não são usadas para inteligência competitiva porque restringem a consulta ao catálogo autorizado da própria conta.

### Proteção obrigatória das cotas

Toda chamada paga em potencial passa primeiro por uma reserva atômica no arquivo local `backend/provider_quota.db`. Se não houver saldo permitido, o backend bloqueia a chamada antes de acessar o provedor. O contador é compartilhado entre filas e processos, persiste entre reinicializações e não é estornado em falhas ou timeouts, pois uma tentativa pode ser faturada mesmo sem retornar dados.

| Provedor | Trava local | Renovação |
| --- | ---: | --- |
| Bright Data | 5.000 requisições | Mensal |
| Apify | 1.400 resultados (até US$ 4,90 no Actor padrão) | Mensal |
| DataForSEO | 1.000 itens reservados (até US$ 1 de teste) | Vitalícia |
| Mercado Livre | Sem tarifa de uso publicada | Não se aplica |

O saldo aparece em `Usuário > Integrações`. As cotas vitalícias não podem ser reiniciadas pela interface. Para contas usadas fora do NeveTracker, confirme o consumo anterior no painel do provedor antes de ativar a chave: a trava controla o que o NeveTracker consome, mas não enxerga chamadas feitas por outros sistemas com a mesma credencial.

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
GET|PUT /products/crawler-control
GET /products/sources
POST /products/sources/{sku}/rediscover
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
- A execução local não exige permissões de administrador. No modo servidor, o Windows pode pedir autorização para liberar o firewall da rede local.
- Não usa serviços pagos obrigatórios.
- Scraping e consultas gratuitas podem sofrer bloqueios temporários por parte das lojas.
- Frete e prazo dependem do CEP e da informação publicada pelo lojista; ausência é retornada como dado não informado.
- O cache local permanece identificado e serve como contingência, sem ser apresentado como coleta nova.
- Dados salvos no SQLite podem ser reaproveitados para reduzir novas coletas.

## Licença

Consulte [LICENSE.txt](LICENSE.txt).

## Servidor na rede local

Execute `servidor.bat`. O inicializador compila a interface, inicia ou reutiliza o backend local e mostra os endereços de acesso, como `http://192.168.1.10:8800`.

- Compartilhe com as pessoas da mesma rede o endereço exibido no console. Mantenha o computador ligado e o console aberto.
- Interface e API usam o mesmo endereço. O navegador dos outros computadores não precisa acessar `localhost` do servidor.
- Os usuários continuam usando as contas e permissões já existentes. O banco de dados é compartilhado pelo backend central.
- A interface compilada é servida diretamente, sem o servidor de desenvolvimento do Vite.
- A regra do firewall permite somente conexões das sub-redes locais. Sua configuração inicial pode solicitar autorização do Windows.
- Se a porta estiver ocupada, use `servidor.bat -Port 8801`.
- Para ambientes cujo firewall é administrado separadamente, use `servidor.bat -NoFirewall`; nesse caso, a liberação da porta depende da regra da rede.
- Logs: `.logs/server/`. `Ctrl+C` encerra os serviços iniciados por esse console; um backend que já existia não é encerrado.

`iniciar.bat` continua disponível para o modo local.

## Lead Time por loja e destino

Na sidebar, abra **Lead Time** e informe o SKU na barra de pesquisa superior. A tabela mostra as 15 lojas por 26 estados e Distrito Federal, com filtro por região. Caso selecione vários SKUs na navbar, escolha qual deles visualizar no seletor da aba.

Cada destino usa um CEP de referência da respectiva capital, exibido no cabeçalho; a cotação não representa todo o estado. Expanda a loja para conferir os lojistas, os tipos de prazo, o frete e a data da cotação. Prazos em dias úteis e corridos são identificados separadamente.

**Consultar prazos** inicia uma fila persistente para os destinos exibidos e as ofertas salvas do SKU. A consulta exige uma conta com permissão de extração. Há progresso e cancelamento; fechar a aba não perde a fila. **Atualizar** apenas recarrega os dados salvos. As cotações por CEP são armazenadas separadamente e não sobrescrevem a logística registrada no histórico de preços.

Resultados ausentes permanecem como não consultados ou não informados; falta de cotação não significa que a loja não entrega no estado. Cotações com mais de 24 horas são sinalizadas.

## Agendamento incremental

Em **Extração > Agendamento**, o toggle **Consultar apenas desatualizados** filtra os materiais antes de criar a fila. Fica desligado por padrão, preservando a extração de todos os SKUs. Quando ligado, considera as últimas observações de cada oferta: sem coleta confirmada nas últimas oito horas, cache, CEP divergente, falhas de fontes ou descoberta incompleta mantêm o SKU pendente. Preço inalterado e confirmação de falta de estoque contam como observações válidas. Alterações de cadastro não contam como coleta. Clique em **Salvar** para persistir a opção; o controle Ativo/Inativo continua independente.

## Mapa de fretes no Analytics

O gráfico **Frete por estado** acompanha o SKU da navbar e segue as lojas selecionadas no botão **Filtros** do Analytics e permite selecionar a UF para conferir valor, lojista, oferta, CEP e data. Para vários SKUs, escolha um no seletor do gráfico. Ausência de cotação permanece **Não cotado**; valores com mais de 24 horas são identificados. O valor corresponde ao CEP de referência da capital, não ao estado inteiro.

O mapa fica abaixo de **Preço mínimo por loja**. **Atualizar** relê as cotações salvas. **Consultar fretes** usa a fila persistente para consultar as 27 UFs nas lojas selecionadas, reaproveitando cotações completas recentes. Apresenta o menor frete pago observado; zero somente se confirmado e sem alternativa paga. Um frete confirmado pode aparecer mesmo quando o prazo não foi informado pela loja.

A geometria é incluída localmente a partir de [click_that_hood](https://github.com/codeforgermany/click_that_hood/blob/main/public/data/brazil-states.geojson), com licença em `src/data/brazilStates.LICENSE.txt`. Não depende de um serviço pago de mapas. Após atualizar o projeto, reinicie o backend para carregar a opção do agendamento e a rota do mapa.
