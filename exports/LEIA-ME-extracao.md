# Extração dos eletrodomésticos

Dados coletados em 11/11/2026 e persistidos no banco local do NeveTracker.

- **47 modelos únicos**, disponíveis em Database, Analytics e Lead Time.
- **1.384 anúncios de 260 lojistas**, em **6.920 registros de anúncio × CEP**.
- **5.225 cotações com frete e prazo confirmados**.
- Pelo menos uma cotação em **234 das 235 combinações de modelo × CEP**.
- Mercado Livre: **648 anúncios**, cobrindo **46 modelos**, e **2.171 cotações** de entrega.
- Shopee: **30 anúncios de cinco modelos**; cobertura parcial e entrega não confirmada nos CEPs solicitados.

CEPs exclusivos: **30130-003, 80530-908, 20211-110, 90010-170 e 01002-020**.
Uma oferta com preço observado e CEP associado não significa que a loja confirmou entrega nesse CEP. Frete e prazo só foram preenchidos mediante cotação específica; campos vazios não significam frete grátis.

## Arquivo completo

`extracao-eletrodomesticos-5-ceps.xlsx` contém 14 abas: Parâmetros, Database, Ofertas, Catálogo, Analytics por SKU, Preço por loja, Comparar lojas, Lead Time detalhes, Frete por estado, Lead Time matriz, Histórico de preços, Evolução de preços, Cadastros e Cobertura da extração.

A cobertura inclui as **1.880 combinações de modelo × CEP × loja solicitada**, inclusive fontes sem retorno. O histórico preserva as observações anteriores; a exportação não cria novas cotações nem inventa valores nos dias sem coleta.

## Lacunas verificadas

- **Shopee:** a sessão autenticada funcionou para buscas e detalhes. Foram lidos preços ordinários, separados de cupons pessoais e descontos condicionais. Após os primeiros resultados, as buscas passaram a receber **90309999**, inclusive após pausas. Tentativas pela barra normal também não liberaram a navegação. Os anúncios salvos pertencem a BLF61AR, BRE66AK, BRM46MB, BRM46MK e BRM62AK. Não houve confirmação de frete/prazo da Shopee nos cinco CEPs; a cotação do endereço padrão da conta não foi reaproveitada. Não se pode afirmar cobertura completa dessa loja.
- **Mercado Livre:** anúncios e lojistas foram identificados individualmente, com o anúncio físico fixado na URL. O modelo **CRE45MB** não retornou oferta confirmada nessa loja; há dados de outras lojas no banco. As cotações públicas de entrega confirmaram o CEP devolvido pelo site antes de serem salvas.
- **MD-RT611EVK46 × 20211-110:** o anúncio encontrado não oferece entrega para esse destino. Há cotações para os outros quatro CEPs. No catálogo oficial Midea, o modelo estava sem estoque. Esta é a única combinação de modelo × CEP sem pelo menos uma cotação confirmada entre as lojas.
- **Amazon:** preços públicos com identificação do vendedor foram preservados; frete e prazo não foram confirmados para os destinos solicitados.
- Uma loja pode não ter o modelo, estar sem estoque ou não entregar para determinado CEP. Esses estados não foram tratados como falha de preço nem preenchidos com informações de outro produto/endereço.

## Identidade e estoque

**MAGISTER 4B INOX PTO** corresponde a **Dako Magister, quatro bocas, preto, mesa inox e sem grill**, código **300001610**, EAN **7897180510206**. Variantes Style, mesa de vidro e Grill foram excluídas. As famílias Midea foram verificadas pelos códigos completos publicados pelas lojas.

Peças e acessórios compatíveis não foram associados aos eletrodomésticos. Anúncios explicitamente sem estoque foram preservados como **Sem estoque**. Ausência de estoque numérico não foi interpretada como indisponibilidade.

As sessões das contas ficam nos perfis locais dedicados. Nenhuma senha, cookie ou informação privada da conta está incluída na exportação. A implementação de leitura das respostas da própria página da Shopee foi baseada no método público do projeto [Unayung/shopee-cli](https://github.com/Unayung/shopee-cli).
