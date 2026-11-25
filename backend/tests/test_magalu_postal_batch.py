import unittest
from unittest.mock import patch,MagicMock
from services.magalu_public_shipping import simulate_destinations,quote_destinations,_validate_product


def estimate(cep,cost,days):
    return {'status':'OK','shippingAddress':{'zipCode':cep},'deliveries':[{'modalities':[
        {'type':'store_pickup','cost':{'customer':0},'shippingTime':{'unit':'working_days','value':{'min':1,'max':1}}},
        {'type':'conventional','name':'Entrega','cost':{'customer':cost},'shippingTime':{'unit':'working_days','value':{'min':days,'max':days}}}]}]}


class MagaluPostalBatchTests(unittest.TestCase):
    def test_exact_title_and_sku_in_opening_paragraph_confirm_the_offer(self):
        title = "Compressor Embraco 1/4+ 110V R134 EMR80HLR"
        item = {"sku": "W11647660", "url": "https://www.magazinevoce.com.br/compressor/p/be49f36dcc/ed/acrf/"}
        product = {"title": title, "reference": "BRASTEMP",
                   "description": f"<p>{title} W11647660</p><p>Compatível com outros modelos.</p>"}
        _validate_product(item, product)
        product["description"] = f"<p>{title}</p><p>Compatível com W11647660.</p>"
        with self.assertRaises(ValueError):
            _validate_product(item, product)

    def setUp(self):
        self.product={'title':'Sensor W10531315','reference':'W10531315','available':True,
            'seller':{'id':'merchant','sku':'merchant-42','category':'3p'},'price':{'price':'40'},'variationId':'platform-42'}

    @patch('services.magalu_public_shipping.simulate')
    @patch('services.magalu_public_shipping.Session')
    def test_each_alias_keeps_its_own_cep_single_quantity_and_response(self,factory,fallback):
        session=factory.return_value.__enter__.return_value
        session.post.return_value.json.return_value={'data':{'d0':estimate('01001000',12,3),'d1':estimate('30111000',25,7)}}
        rows=simulate_destinations(self.product,{},['01001000','30111000'])
        self.assertEqual(rows['01001000'][0]['shipping_cost'],12)
        self.assertEqual(rows['30111000'][0]['delivery_max_days'],7)
        request=session.post.call_args.kwargs['json']
        self.assertIsInstance(request,dict)
        self.assertEqual(request['variables']['r0']['zipcode'],'01001000')
        self.assertEqual(request['variables']['r1']['zipcode'],'30111000')
        self.assertTrue(all(value['product']['quantity']==1 for value in request['variables'].values()))
        fallback.assert_not_called()

    @patch('services.magalu_public_shipping.simulate')
    @patch('services.magalu_public_shipping.Session')
    def test_only_missing_or_wrong_destination_alias_falls_back(self,factory,fallback):
        session=factory.return_value.__enter__.return_value
        session.post.return_value.json.return_value={'data':{'d0':estimate('01001000',12,3),'d1':estimate('99999999',1,1)}}
        fallback.return_value=({'shipping_cost':25,'delivery_max_days':7,'destination_zip':'30111000'}, {})
        rows=simulate_destinations(self.product,{},['01001000','30111000'])
        self.assertEqual(rows['01001000'][0]['shipping_cost'],12)
        self.assertEqual(rows['30111000'][0]['shipping_cost'],25)
        self.assertEqual(fallback.call_count,1)
        self.assertEqual(fallback.call_args.args[2],'30111000')

    @patch('services.magalu_public_shipping.simulate', side_effect=TimeoutError('timeout'))
    @patch('services.magalu_public_shipping.Session')
    def test_failed_individual_retry_preserves_other_postal_estimates(self, factory, fallback):
        session = factory.return_value.__enter__.return_value
        session.post.return_value.json.return_value = {'data': {'d0': estimate('01001000', 12, 3)}}
        item = {'sku':'W10531315','seller':'merchant-Magalu','url':'https://www.magazinevoce.com.br/sensor/p/123/ed/peca/'}
        results = quote_destinations(item, ['01001000', '30111000'], page_context=(self.product, {}))
        self.assertEqual(results['01001000']['shipping_cost'], 12)
        self.assertEqual(results['30111000']['price'], 40)
        self.assertNotIn('delivery_max_days', results['30111000'])
        self.assertEqual(results['30111000']['_logistics_error'], 'timeout')
        fallback.assert_called_once()
        with self.assertRaises(ValueError):
            quote_destinations({**item, 'seller':'outro-Magalu'}, ['01001000'], page_context=(self.product, {}))

    @patch('services.magalu_public_shipping.simulate')
    @patch('services.magalu_public_shipping.Session')
    def test_all_27_destinations_are_preserved_in_three_requests(self,factory,fallback):
        session=factory.return_value.__enter__.return_value
        def post(url,**kwargs):
            response=MagicMock()
            response.json.return_value={'data':{f'd{i}':estimate(value['zipcode'],int(value['zipcode'][-2:])+1,3)
                for i,value in enumerate(kwargs['json']['variables'].values())}}
            return response
        session.post.side_effect=post
        ceps=[f'{10000000+i:08d}' for i in range(27)]
        item={'sku':'W10531315','url':'https://www.magazinevoce.com.br/sensor/p/123/ed/peca/','price':40}
        rows=quote_destinations(item,ceps,page_context=(self.product,{}))
        self.assertEqual(set(rows),set(ceps));self.assertEqual(session.post.call_count,3)
        for index,cep in enumerate(ceps):
            self.assertEqual(rows[cep]['destination_zip'],cep)
            self.assertEqual(rows[cep]['shipping_cost'],index+1)
        fallback.assert_not_called()

    @patch('services.magalu_public_shipping.simulate_destinations')
    def test_stockout_preserves_all_destinations_without_delivery_simulation(self,simulate):
        item={'sku':'W10531315','url':'https://www.magazinevoce.com.br/sensor/p/123/ed/peca/','price':40}
        rows=quote_destinations(item,['01001000','30111000'],page_context=({**self.product,'available':False},{}))
        self.assertTrue(all(row['availability']=='out_of_stock' and row['price'] is None for row in rows.values()))
        simulate.assert_not_called()

    @patch('services.magalu_public_shipping.simulate_destinations')
    def test_quote_preserves_store_reason_and_published_payment_and_delivery_owner(self, simulate):
        cep = '01001000'
        simulate.return_value = {cep: ({}, {'data': {'shipping': {'deliveries': [
            {'status': {'code': 'regional', 'message': 'Frete indisponível para sua região.'}},
        ]}}})}
        item = {'sku': 'W10531315', 'url': 'https://www.magazinevoce.com.br/sensor/p/123/ed/peca/'}
        product = {**self.product, 'seller': {**self.product['seller'], 'deliveryId': 'merchant', 'deliveryDescription': 'Loja A'}}
        result = quote_destinations(item, [cep], page_context=(product, {}))[cep]
        self.assertEqual(result['_logistics_error'], 'Frete indisponível para sua região.')
        self.assertEqual(result['installment_price'], 40)
        self.assertEqual(result['fulfilled_by'], 'Loja A')
        self.assertNotIn('shipping_cost', result)
