import unittest
from services.product_resolver import ProductIdentity,resolve_candidate,appliance_kind


class MaterialIdentityRegressionTests(unittest.TestCase):
    def test_compressor_model_alias_checks_voltage_and_preserves_exact_sku(self):
        identity = ProductIdentity(sku="W11647660", model="EMR80HLR", name="Compressor Embraco EMR80HLR 127V R134a")
        for name in ("Compressor EMR80HLR 127V R134a", "Compressor EMR80HLR 110V R134", "Compressor W11647660"):
            self.assertTrue(resolve_candidate(identity, {"name": name}).accepted, name)
        for name in ("Compressor EMR80HLR 220V R134a", "Compressor EMR80HLR", "Compressor W11647660 220V"):
            self.assertFalse(resolve_candidate(identity, {"name": name}).accepted, name)

    def test_refrigerator_spare_is_not_reclassified_as_whole_refrigerator(self):
        identity=ProductIdentity(sku='W10531315',model='W10531315',name='Kit Sensor de Temperatura para Geladeira - W10531315',brand='Brastemp')
        self.assertIsNone(appliance_kind(identity.name))
        for name in ['Sensor de Temperatura 2,7k 25mm Original Whirlpool W10531315 - BRASTEM',
                     'Kit Sensor de Temperatura para Geladeira - W10531315 W10531315']:
            self.assertTrue(resolve_candidate(identity,{'name':name,'brand':'Brastemp'}).accepted,name)

    def test_spare_identity_still_rejects_different_component_and_combined_control_kit(self):
        identity=ProductIdentity(sku='W10531315',name='Kit Sensor de Temperatura para Geladeira W10531315')
        for name in ['Placa para Geladeira compatível W10531315',
                     'Kit Controle Eletrônico Bivolt e Kit Sensor de Temperatura para Geladeira CJ-W10531315_2']:
            self.assertFalse(resolve_candidate(identity,{'name':name}).accepted,name)

    def test_complete_appliance_still_rejects_compatible_spare(self):
        identity=ProductIdentity(sku='BRM46MB',name='Geladeira Brastemp BRM46MB')
        self.assertEqual(appliance_kind(identity.name),'refrigerator')
        self.assertFalse(resolve_candidate(identity,{'name':'Sensor para Geladeira Brastemp BRM46MB'}).accepted)
        self.assertTrue(resolve_candidate(identity,{'name':'Geladeira Brastemp BRM46MB'}).accepted)

    def test_shipping_browser_ignores_cep_checkbox_and_fills_editable_field(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        from threading import Thread
        from services.playwright_fallback import collect_shipping_with_playwright
        from services.browser_health import reset_browser_health
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                body=b'''<html><body><input type="checkbox" id="cep-promotions">
                <form onsubmit="event.preventDefault();document.getElementById('frete').innerText='Frete PAC R$ 15,90 - entrega em 4 dias';">
                <input type="text" name="cep" placeholder="CEP"><button type="submit">Calcular</button></form>
                <div id="frete"></div></body></html>'''
                self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(body)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=Thread(target=server.serve_forever,daemon=True);thread.start();reset_browser_health()
        try:
            quote=collect_shipping_with_playwright(f'http://127.0.0.1:{server.server_port}/product','01001000',5)
            self.assertEqual(quote.get('shipping_cost'),15.9)
            self.assertEqual(quote.get('delivery_max_days'),4)
            self.assertEqual(quote.get('destination_zip'),'01001000')
        finally:server.shutdown();server.server_close();thread.join(timeout=2);reset_browser_health()
