# Coleta de 12/11/2026

Nova coleta dos 47 modelos únicos da solicitação, nas oito lojas indicadas: Mercado Livre, Shopee, Magazine Luiza, Amazon Brasil, Consul, Brastemp, Electrolux e Midea.

Foram usados exclusivamente **30130-003, 80530-908, 20211-110, 90010-170 e 01002-020**.

- **1.943 anúncios** com novas observações de hoje.
- **9.715 registros de anúncio × CEP**, salvos no banco.
- **6.205 cotações válidas com frete e prazo**.
- Pelo menos uma cotação disponível em **234 das 235 combinações de modelo × CEP**, considerando todas as lojas.
- Mercado Livre: **867 anúncios, 46 modelos e 2.447 cotações**.
- Shopee: **239 anúncios, 43 modelos e 275 cotações válidas de itens disponíveis**.

O arquivo `extracao-eletrodomesticos-2026-11-12.xlsx` contém 14 abas: parâmetros, Database, Ofertas, Catálogo, Analytics por SKU, Preço por loja, Comparar lojas, Lead Time detalhes, Frete por estado, Lead Time matriz, Histórico de preços, Evolução de preços, Cadastros e Cobertura da extração.

O arquivo foi validado para conter somente observações novas desta coleta. Valores de ontem não foram reclassificados como observações de hoje. O banco mantém o histórico anterior e os últimos valores conhecidos dos anúncios que não puderam ser confirmados novamente.

## Limitações verificadas

- **MD-RT611EVK46 / 20211-110:** não houve entrega confirmada. O anúncio disponível no Mercado Livre oferece entrega nos outros quatro CEPs; o catálogo oficial indica indisponibilidade de estoque. Não foi preenchido um frete fictício para o Rio de Janeiro.
- **Shopee:** a busca pela página autenticada retornou preços atuais para 43 modelos. A consulta de entrega confirmou os CEPs exatos nas respostas do próprio site. Depois das cotações salvas, o site voltou a recusar solicitações com `90309999`; a tentativa após a pausa também foi recusada. Assim, parte dos anúncios tem preço observado, mas frete e prazo ainda não confirmados.
- Não houve oferta confirmada na Shopee para **MAGISTER 4B INOX PTO / 300001610, MD-RT411FGF01, IB6 e MD-RT611EVK46**, inclusive nas buscas por referências alternativas. Os quatro modelos têm novas observações em outras lojas.
- **Amazon:** foram recuperados preços atuais de 35 anúncios, em 24 modelos. A tentativa de configurar cada um dos cinco CEPs pelo seletor público não confirmou a aplicação do destino no anúncio. Frete e prazo não foram atribuídos sem essa confirmação.
- As lojas oficiais de cada marca não necessariamente oferecem produtos das demais marcas. Ausência de anúncio não foi convertida em “Sem estoque”; essa indicação só foi usada quando a loja declarou indisponibilidade.

## Critérios

Frete e prazo pertencem ao mesmo serviço e ao CEP informado. Havendo alternativas, foi escolhido o menor frete pago. Zero foi preservado somente quando explicitamente confirmado, sem alternativa paga. Valores condicionados a cupom não substituem o preço ordinário do produto ou do serviço de entrega.

Na Shopee, os lojistas são mantidos pelo identificador público da loja; o nome foi recuperado quando os detalhes do anúncio o disponibilizaram. Uma identificação genérica substituída pelo nome confirmado não permanece como uma segunda fonte ativa do mesmo anúncio.

**MAGISTER 4B INOX PTO** usa o cadastro **300001610**, Dako Magister de quatro bocas, preto, mesa inox, sem grill. Variantes incompatíveis não foram associadas ao código solicitado.

Nenhuma senha, cookie ou dado privado da conta faz parte do Excel.
