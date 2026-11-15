import unittest
from bot import Shop, public_text


class ShopTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.fail_document = False
        def api(method, **data):
            self.calls.append((method, data))
            if method == 'sendDocument' and self.fail_document:
                raise RuntimeError('offline')
            return True
        self.shop = Shop(':memory:', api, {1})
        self.shop.execute('INSERT INTO products(title,description,price,photo,file,instructions) VALUES(?,?,?,?,?,?)', ('Конфиг', 'Описание', 100, 'photo', 'file', 'Инструкция'))

    def tearDown(self):
        self.shop.db.close()

    def order(self):
        self.shop.callback(2, 'buy:1')
        return self.shop.one('SELECT * FROM orders')

    def pay(self, o, **extra):
        p = dict(telegram_payment_charge_id='charge', invoice_payload=o['id'], currency='XTR', total_amount=100)
        p.update(extra)
        self.shop.payment(2, p)

    def test_invoice(self):
        self.order()
        method, data = self.calls[-1]
        self.assertEqual(method, 'sendInvoice')
        self.assertEqual(data['currency'], 'XTR')
        self.assertEqual(data['prices'][0]['amount'], 100)
        self.assertEqual(data['provider_token'], '')

    def test_checkout_rejects_wrong_owner_currency_amount(self):
        o = self.order()
        valid = dict(id='q', invoice_payload=o['id'], currency='XTR', total_amount=100, **{'from': {'id': 2}})
        self.shop.precheckout(valid)
        self.assertTrue(self.calls[-1][1]['ok'])
        for patch in [{'from': {'id': 3}}, {'currency': 'USD'}, {'total_amount': 1}]:
            self.shop.precheckout(valid | patch)
            self.assertFalse(self.calls[-1][1]['ok'])

    def test_duplicate_payment_and_retry(self):
        o = self.order()
        self.pay(o)
        self.pay(o)
        self.assertEqual(self.shop.one('SELECT COUNT(*) FROM payment_events')[0], 1)
        self.fail_document = True
        self.shop.retry_deliveries()
        self.assertEqual(self.shop.one('SELECT delivered FROM orders')[0], 0)
        self.fail_document = False
        self.shop.execute('UPDATE orders SET retry_at=0')
        self.shop.retry_deliveries()
        self.assertEqual(self.shop.one('SELECT delivered FROM orders')[0], 1)
        before = len(self.calls)
        self.shop.retry_deliveries()
        self.assertEqual(len(self.calls), before)

    def test_bad_payment_quarantined(self):
        o = self.order()
        self.pay(o, total_amount=1)
        self.assertEqual(self.shop.one('SELECT paid FROM orders')[0], 0)
        self.assertEqual(self.shop.one('SELECT status FROM payment_events')[0], 'needs_review')

    def test_admin_enforced_for_buttons_and_states(self):
        self.shop.callback(2, 'toggle:1')
        self.assertEqual(self.shop.one('SELECT active FROM products')[0], 1)
        self.shop.state(2, {'kind': 'product', 'step': 'title'})
        self.msg('Hacked')
        self.assertEqual(self.shop.state(2), {})
        self.shop.callback(2, 'refund_confirm:x')
        self.assertFalse(any(m == 'refundStarPayment' for m, _ in self.calls))

    def msg(self, text):
        self.shop.message({'chat': {'type': 'private'}, 'from': {'id': 2}, 'text': text})

    def test_review_purchase_gate_anonymous_and_upsert(self):
        self.shop.callback(2, 'review:1')
        self.assertFalse(self.shop.state(2))
        o = self.order()
        self.pay(o)
        for rating in [5, 4]:
            self.shop.callback(2, 'review:1')
            self.shop.callback(2, f'rating:{rating}')
            self.shop.callback(2, 'anonymous')
            self.msg('Отлично @username https://t.me/example')
        r = self.shop.one('SELECT * FROM reviews')
        self.assertEqual(r['name'], 'Аноним')
        self.assertEqual(r['stars'], 4)
        self.assertNotIn('@', r['text'])
        self.assertEqual(self.shop.one('SELECT COUNT(*) FROM reviews')[0], 1)

    def test_no_download_without_purchase_or_wrong_owner(self):
        o = self.order()
        self.shop.callback(2, 'download:' + o['id'])
        self.pay(o)
        self.shop.callback(3, 'download:' + o['id'])
        self.assertFalse(any(m == 'sendDocument' for m, _ in self.calls))

    def test_refund_revokes_access(self):
        o = self.order()
        self.pay(o)
        self.shop.callback(1, 'refund_confirm:' + o['id'])
        self.assertFalse(self.shop.eligible(2, 1))
        self.shop.callback(2, 'download:' + o['id'])
        self.assertFalse(any(m == 'sendDocument' for m, _ in self.calls))

    def test_price_boundaries(self):
        for price in ['0', '1001', '-1', '1.5', 'abc']:
            self.shop.state(1, {'kind': 'product', 'step': 'price'})
            self.shop.message({'chat': {'type': 'private'}, 'from': {'id': 1}, 'text': price})
            self.assertEqual(self.shop.state(1)['step'], 'price')
        for price in ['1', '1000']:
            self.shop.state(1, {'kind': 'product', 'step': 'price'})
            self.shop.message({'chat': {'type': 'private'}, 'from': {'id': 1}, 'text': price})
            self.assertEqual(self.shop.state(1)['step'], 'photo')

    def test_file_and_instruction_snapshot(self):
        o = self.order()
        self.shop.execute("UPDATE products SET file='new',instructions='changed'")
        self.pay(o)
        self.shop.retry_deliveries()
        sent = [d for m, d in self.calls if m == 'sendDocument']
        self.assertEqual(sent[0]['document'], 'file')


if __name__ == '__main__':
    unittest.main()
