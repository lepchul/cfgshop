"""КФГ ОТ МУХАМЕДА. Python 3.11+, standard library only."""
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

NAME = 'КФГ ОТ МУХАМЕДА'
LOG = logging.getLogger('shop')


def load_env():
    p = Path('.env')
    if p.exists():
        for line in p.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('\"\''))


def keyboard(*rows):
    return {'inline_keyboard': [[{'text': t, 'callback_data': d} for t, d in row] for row in rows]}


def public_text(value):
    # Never publish Telegram mentions, links, hidden formatting or contact entities.
    value = re.sub(r'(?i)(?:https?://|tg://|(?:www\.)?t\.me/|telegram\.me/)\S+|@[\w]+', '[скрыто]', value)
    return ''.join(c for c in value if c.isprintable() or c == '\n').replace('—', '-')


class Telegram:
    def __init__(self, token):
        self.url = 'https://api.telegram.org/bot' + token + '/'

    def __call__(self, method, **data):
        req = Request(self.url + method, json.dumps(data).encode(), {'Content-Type': 'application/json'})
        try:
            with urlopen(req, timeout=40) as response:
                body = json.load(response)
        except HTTPError as exc:
            # Do not log URLs: they contain the token.
            try:
                body = json.loads(exc.read())
            except Exception:
                raise RuntimeError(f'Telegram HTTP {exc.code}') from None
        if not body.get('ok'):
            raise RuntimeError(str(body.get('description', 'Telegram error')))
        return body['result']


class Shop:
    def __init__(self, db, api, admins, support='Свяжитесь с продавцом.'):
        self.db = sqlite3.connect(db)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.api, self.admins, self.support = api, set(admins), support
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS products(
          id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
          price INTEGER NOT NULL CHECK(price BETWEEN 1 AND 1000), photo TEXT NOT NULL,
          file TEXT NOT NULL, instructions TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS orders(
          id TEXT PRIMARY KEY, uid INTEGER NOT NULL, product INTEGER NOT NULL,
          title TEXT NOT NULL, price INTEGER NOT NULL, file TEXT NOT NULL, instructions TEXT NOT NULL,
          created INTEGER NOT NULL, charge TEXT UNIQUE, paid INTEGER DEFAULT 0,
          delivered INTEGER DEFAULT 0, refunded INTEGER DEFAULT 0, retry_at INTEGER DEFAULT 0,
          FOREIGN KEY(product) REFERENCES products(id));
        CREATE TABLE IF NOT EXISTS reviews(
          id INTEGER PRIMARY KEY, uid INTEGER NOT NULL, product INTEGER NOT NULL,
          stars INTEGER NOT NULL CHECK(stars BETWEEN 1 AND 5), name TEXT NOT NULL,
          text TEXT NOT NULL, hidden INTEGER DEFAULT 0, UNIQUE(uid,product));
        CREATE TABLE IF NOT EXISTS states(uid INTEGER PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS payment_events(charge TEXT PRIMARY KEY, data TEXT NOT NULL, status TEXT NOT NULL);
        ''')

    def execute(self, sql, args=()):
        with self.db:
            return self.db.execute(sql, args)

    def one(self, sql, args=()):
        return self.db.execute(sql, args).fetchone()

    def say(self, uid, text, markup=None):
        args = {'chat_id': uid, 'text': text}
        if markup:
            args['reply_markup'] = markup
        return self.api('sendMessage', **args)

    def state(self, uid, value=None):
        if value is not None:
            self.execute('INSERT OR REPLACE INTO states VALUES(?,?)', (uid, json.dumps(value)))
            return
        r = self.one('SELECT data FROM states WHERE uid=?', (uid,))
        return json.loads(r[0]) if r else {}

    def home(self, uid):
        rows = [[('Ознакомиться с товарами', 'catalog:0')], [('Отзывы', 'reviews:0')], [('Мои покупки', 'orders:0')]]
        if uid in self.admins:
            rows.append([('Админ-панель', 'admin')])
        self.say(uid, 'Приветствуем вас в магазине конфигов КФГ ОТ МУХМЕДА\n\nВыбирай конфиг, оплачивай звездами и забирай файл. Ойойойойой ⭐', keyboard(*rows))

    def eligible(self, uid, pid):
        return self.one('SELECT id FROM orders WHERE uid=? AND product=? AND paid=1 AND refunded=0', (uid, pid))

    def precheckout(self, q):
        o = self.one('SELECT * FROM orders WHERE id=?', (q['invoice_payload'],))
        ok = bool(o and o['uid'] == q['from']['id'] and not o['paid'] and
                  q['currency'] == 'XTR' and q['total_amount'] == o['price'] and
                  time.time() - o['created'] < 3600 and
                  self.one('SELECT id FROM products WHERE id=? AND active=1', (o['product'],)))
        args = {'pre_checkout_query_id': q['id'], 'ok': ok}
        if not ok:
            args['error_message'] = 'Этот счет уже недоступен. Открой товар и создай новый.'
        self.api('answerPreCheckoutQuery', **args)

    def payment(self, uid, p):
        charge = p['telegram_payment_charge_id']
        with self.db:
            if self.one('SELECT charge FROM payment_events WHERE charge=?', (charge,)):
                return
            o = self.one('SELECT * FROM orders WHERE id=?', (p['invoice_payload'],))
            valid = bool(o and o['uid'] == uid and o['price'] == p['total_amount'] and p['currency'] == 'XTR' and not o['paid'])
            self.db.execute('INSERT INTO payment_events VALUES(?,?,?)',
                            (charge, json.dumps({'uid': uid, 'payment': p}), 'accepted' if valid else 'needs_review'))
            if valid:
                self.db.execute('UPDATE orders SET paid=1,charge=? WHERE id=?', (charge, o['id']))
        if not valid:
            LOG.error('Payment needs manual review, stored in payment_events')
            for admin in self.admins:
                try:
                    self.say(admin, 'Платеж требует проверки. Запись сохранена в payment_events. Покупатель: ' + str(uid))
                except Exception:
                    LOG.warning('Could not notify administrator')

    def deliver(self, o):
        self.api('sendDocument', chat_id=o['uid'], document=o['file'], caption='Оплачено ⭐ ' + o['title'])
        self.say(o['uid'], 'Как установить:\n\n' + o['instructions'], keyboard([('Оставить отзыв', f"review:{o['product']}")]))
        self.execute('UPDATE orders SET delivered=1 WHERE id=?', (o['id'],))

    def retry_deliveries(self):
        for o in self.db.execute('SELECT * FROM orders WHERE paid=1 AND refunded=0 AND delivered=0 AND retry_at<=? LIMIT 3', (int(time.time()),)).fetchall():
            try:
                self.deliver(o)
            except Exception:
                self.execute('UPDATE orders SET retry_at=? WHERE id=?', (int(time.time()) + 60, o['id']))
                LOG.warning('Delivery delayed for order %s', o['id'])

    def callback(self, uid, data):
        action, _, arg = data.partition(':')
        if action in {'admin', 'add', 'manage', 'toggle', 'moderate', 'hide', 'stats', 'refund', 'refund_confirm'} and uid not in self.admins:
            return self.say(uid, 'Нет доступа.')
        if action == 'home':
            self.state(uid, {})
            return self.home(uid)
        if action == 'catalog':
            page = max(0, int(arg))
            rows = self.db.execute('SELECT * FROM products WHERE active=1 ORDER BY id DESC LIMIT 9 OFFSET ?', (page * 8,)).fetchall()
            buttons = [[(f"{p['title']} · {p['price']} ⭐", f"product:{p['id']}")] for p in rows[:8]]
            if page:
                buttons.append([('Назад', f'catalog:{page-1}')])
            if len(rows) > 8:
                buttons.append([('Дальше', f'catalog:{page+1}')])
            buttons.append([('В меню', 'home')])
            return self.say(uid, 'Выбирай, что тебе по душе 👇' if rows else 'Пока пусто. Скоро добавим конфиги.', keyboard(*buttons))
        if action == 'product':
            p = self.one('SELECT * FROM products WHERE id=? AND active=1', (int(arg),))
            if not p:
                return self.say(uid, 'Товар пока недоступен.')
            return self.api('sendPhoto', chat_id=uid, photo=p['photo'], caption=f"{p['title']}\n\n{p['description']}\n\nЦена: {p['price']} ⭐\nНажимая оплату, ты принимаешь /terms.", reply_markup=keyboard([(f"Купить за {p['price']} ⭐", f"buy:{p['id']}")], [('К товарам', 'catalog:0')]))
        if action == 'buy':
            p = self.one('SELECT * FROM products WHERE id=? AND active=1', (int(arg),))
            if not p:
                return self.say(uid, 'Товар пока недоступен.')
            if self.eligible(uid, p['id']):
                return self.say(uid, 'У тебя уже есть этот конфиг. Забери его в «Мои покупки».', keyboard([('Мои покупки', 'orders:0')]))
            pending = self.one('SELECT created FROM orders WHERE uid=? ORDER BY created DESC LIMIT 1', (uid,))
            if pending and time.time() - pending['created'] < 5:
                return self.say(uid, 'Подожди пару секунд перед новым счетом.')
            oid = uuid.uuid4().hex
            self.execute('INSERT INTO orders(id,uid,product,title,price,file,instructions,created) VALUES(?,?,?,?,?,?,?,?)',
                         (oid, uid, p['id'], p['title'], p['price'], p['file'], p['instructions'], int(time.time())))
            return self.api('sendInvoice', chat_id=uid, title=p['title'], description=p['description'], payload=oid,
                            provider_token='', currency='XTR', prices=[{'label': p['title'], 'amount': p['price']}], start_parameter='shop')
        if action == 'orders':
            page = max(0, int(arg))
            rows = self.db.execute('SELECT * FROM orders WHERE uid=? AND paid=1 AND refunded=0 ORDER BY created DESC LIMIT 9 OFFSET ?', (uid, page * 8)).fetchall()
            buttons = [[(o['title'] + ' · Скачать', 'download:' + o['id'])] for o in rows[:8]]
            if page:
                buttons.append([('Назад', f'orders:{page-1}')])
            if len(rows) > 8:
                buttons.append([('Дальше', f'orders:{page+1}')])
            return self.say(uid, 'Твои покупки 👇' if rows else 'Покупок пока нет.', keyboard(*buttons, [('В меню', 'home')]))
        if action == 'download':
            o = self.one('SELECT * FROM orders WHERE id=? AND uid=? AND paid=1 AND refunded=0', (arg, uid))
            if o:
                return self.deliver(o)
            return self.say(uid, 'Покупка не найдена.')
        if action == 'review':
            pid = int(arg)
            if not self.eligible(uid, pid):
                return self.say(uid, 'Отзывы доступны после покупки.')
            self.state(uid, {'kind': 'review', 'product': pid, 'step': 'rating'})
            return self.say(uid, 'Как тебе конфиг? Выбери оценку.', keyboard([(str(n) + ' ⭐', f'rating:{n}') for n in range(1, 6)]))
        if action == 'rating':
            s = self.state(uid)
            if s.get('kind') != 'review' or s.get('step') != 'rating' or not 1 <= int(arg) <= 5:
                return
            s.update(stars=int(arg), step='name')
            self.state(uid, s)
            return self.say(uid, 'Напиши имя для отзыва, без @ и ссылок. Или выбери анонимный отзыв.', keyboard([('Анонимно', 'anonymous')]))
        if action == 'anonymous':
            s = self.state(uid)
            if s.get('kind') == 'review' and s.get('step') == 'name':
                s.update(name='Аноним', step='text')
                self.state(uid, s)
                return self.say(uid, 'Напиши пару слов о покупке. До 600 символов.')
        if action in {'reviews', 'moderate'}:
            page = max(0, int(arg))
            condition = '' if action == 'moderate' else 'WHERE hidden=0'
            rows = self.db.execute(f'SELECT * FROM reviews {condition} ORDER BY id DESC LIMIT 6 OFFSET ?', (page * 5,)).fetchall()
            avg = self.one('SELECT COUNT(*),AVG(stars) FROM reviews WHERE hidden=0')
            self.say(uid, f'Отзывы: {avg[0]}' + (f' · {avg[1]:.1f}/5 ⭐' if avg[0] else '\nБудь первым после покупки.'))
            for r in rows[:5]:
                p = self.one('SELECT title FROM products WHERE id=?', (r['product'],))
                buttons = keyboard([('Показать' if r['hidden'] else 'Скрыть', f"hide:{r['id']}")]) if action == 'moderate' else None
                self.say(uid, f"{'⭐' * r['stars']} · {public_text(r['name'])}\n{public_text(p['title'])}\n\n{public_text(r['text'])}", buttons)
            nav = []
            if page:
                nav.append(('Назад', f'{action}:{page-1}'))
            if len(rows) > 5:
                nav.append(('Дальше', f'{action}:{page+1}'))
            return self.say(uid, 'Все отзывы оставлены покупателями.', keyboard(nav, [('В меню', 'home')]) if nav else keyboard([('В меню', 'home')]))
        if action == 'admin':
            return self.say(uid, 'Админ-панель\nЧто делаем?', keyboard([('Добавить товар', 'add')], [('Товары', 'manage:0')], [('Отзывы', 'moderate:0')], [('Статистика', 'stats')], [('Возврат оплаты', 'refund')], [('В меню', 'home')]))
        if action == 'add':
            self.state(uid, {'kind': 'product', 'step': 'title'})
            return self.say(uid, 'Напиши название товара, до 32 символов.\nОтменить: /cancel')
        if action == 'manage':
            page = max(0, int(arg))
            rows = self.db.execute('SELECT * FROM products ORDER BY id DESC LIMIT 9 OFFSET ?', (page * 8,)).fetchall()
            buttons = [[(('🟢 ' if r['active'] else '⚫ ') + r['title'], f"toggle:{r['id']}")] for r in rows[:8]]
            if page:
                buttons.append([('Назад', f'manage:{page-1}')])
            if len(rows) > 8:
                buttons.append([('Дальше', f'manage:{page+1}')])
            return self.say(uid, 'Нажми на товар, чтобы скрыть или вернуть в продажу. Для новой версии создай новый товар.', keyboard(*buttons, [('Админ-панель', 'admin')]))
        if action == 'toggle':
            self.execute('UPDATE products SET active=1-active WHERE id=?', (int(arg),))
            return self.say(uid, 'Готово.', keyboard([('Товары', 'manage:0')]))
        if action == 'hide':
            self.execute('UPDATE reviews SET hidden=1-hidden WHERE id=?', (int(arg),))
            return self.say(uid, 'Видимость отзыва изменена.')
        if action == 'stats':
            r = self.one('SELECT COUNT(*),COALESCE(SUM(price),0) FROM orders WHERE paid=1 AND refunded=0')
            pending = self.one('SELECT COUNT(*) FROM orders WHERE paid=1 AND refunded=0 AND delivered=0')[0]
            return self.say(uid, f'Покупки без возвратов: {r[0]}\nОплачено: {r[1]} ⭐\nОжидают выдачи: {pending}')
        if action == 'refund':
            self.state(uid, {'kind': 'refund'})
            return self.say(uid, 'Пришли ID заказа из /receipt покупателя. Перед возвратом покажу сумму.')
        if action == 'refund_confirm':
            o = self.one('SELECT * FROM orders WHERE id=? AND paid=1 AND refunded=0', (arg,))
            if not o:
                return self.say(uid, 'Заказ не найден или уже возвращен.')
            try:
                self.api('refundStarPayment', user_id=o['uid'], telegram_payment_charge_id=o['charge'])
            except RuntimeError as exc:
                if 'CHARGE_ALREADY_REFUNDED' not in str(exc):
                    raise
            self.execute('UPDATE orders SET refunded=1 WHERE id=?', (arg,))
            if not self.eligible(o['uid'], o['product']):
                self.execute('UPDATE reviews SET hidden=1 WHERE uid=? AND product=?', (o['uid'], o['product']))
            return self.say(uid, 'Звезды возвращены покупателю.')

    def message(self, m):
        if m['chat']['type'] != 'private':
            return
        uid = m['from']['id']
        if 'successful_payment' in m:
            return self.payment(uid, m['successful_payment'])
        if 'refunded_payment' in m:
            p = m['refunded_payment']
            o = self.one('SELECT * FROM orders WHERE charge=?', (p['telegram_payment_charge_id'],))
            if o:
                self.execute('UPDATE orders SET refunded=1 WHERE id=?', (o['id'],))
                if not self.eligible(o['uid'], o['product']):
                    self.execute('UPDATE reviews SET hidden=1 WHERE uid=? AND product=?', (o['uid'], o['product']))
            return
        text = m.get('text', '').strip()
        command = text.split()[0].split('@')[0] if text else ''
        if command in {'/start', '/cancel'}:
            self.state(uid, {})
            return self.home(uid)
        if command == '/admin':
            return self.callback(uid, 'admin')
        if command == '/id':
            return self.say(uid, f'Твой Telegram ID: {uid}')
        if command in {'/support', '/paysupport'}:
            return self.say(uid, 'Проблема с оплатой или файлом?\n' + self.support + '\nНомер заказа можно найти через /receipt.')
        if command == '/terms':
            return self.say(uid, 'Ты покупаешь цифровой файл с инструкцией. Цена указана в звездах Telegram. Файл выдается после подтверждения оплаты. Повторная загрузка доступна в «Мои покупки». Совместимость смотри в описании товара. По вопросам и возвратам: /paysupport.\n\n' + os.getenv('SHOP_TERMS', ''))
        if command == '/privacy':
            return self.say(uid, 'Храним Telegram ID, покупки, ID платежей и отзывы для выдачи товаров и поддержки. Username не сохраняем и в отзывах не показываем. Анонимный отзыв скрывает имя от посетителей, но продавец может связать отзыв с покупкой. По вопросам данных: /support.')
        if command == '/receipt':
            rows = self.db.execute('SELECT * FROM orders WHERE uid=? AND paid=1 ORDER BY created DESC LIMIT 10', (uid,)).fetchall()
            return self.say(uid, '\n\n'.join(f"{r['title']} · {r['price']} ⭐\n{r['id']}" for r in rows) or 'Покупок пока нет.')
        s = self.state(uid)
        kind = s.get('kind')
        if kind in {'product', 'refund'} and uid not in self.admins:
            self.state(uid, {})
            return self.say(uid, 'Нет доступа.')
        if kind == 'refund':
            o = self.one('SELECT * FROM orders WHERE id=? AND paid=1 AND refunded=0', (text,))
            if not o:
                return self.say(uid, 'Не нашел оплаченный заказ. Проверь ID.')
            self.state(uid, {})
            return self.say(uid, f"Вернуть {o['price']} ⭐ за {o['title']}?\nПокупатель: {o['uid']}\nУже скачанный файл отозвать нельзя.", keyboard([('Вернуть звезды', 'refund_confirm:' + o['id'])], [('Отмена', 'admin')]))
        if kind == 'product':
            steps = ['title', 'description', 'price', 'photo', 'file', 'instructions']
            step = s['step']
            limits = {'title': 32, 'description': 650, 'instructions': 3500}
            if step in limits:
                if not 1 <= len(text) <= limits[step] or text.startswith('/'):
                    return self.say(uid, f'Нужен текст от 1 до {limits[step]} символов.')
                s[step] = text.replace('—', '-')
            elif step == 'price':
                if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= 1000:
                    return self.say(uid, 'Цена должна быть целым числом от 1 до 1000 ⭐.')
                s[step] = int(text)
            elif step == 'photo':
                if not m.get('photo'):
                    return self.say(uid, 'Отправь картинку как фото, не как документ.')
                s[step] = m['photo'][-1]['file_id']
            elif step == 'file':
                if not m.get('document') or m['document'].get('file_size', 0) > 49 * 1024 * 1024:
                    return self.say(uid, 'Пришли файл документом, до 49 МБ.')
                s[step] = m['document']['file_id']
            i = steps.index(step)
            if i == len(steps) - 1:
                self.execute('INSERT INTO products(title,description,price,photo,file,instructions) VALUES(?,?,?,?,?,?)', tuple(s[k] for k in steps))
                self.state(uid, {})
                return self.say(uid, 'Ойойойойой, товар уже в магазине 🔥', keyboard([('Открыть товары', 'catalog:0')], [('Админ-панель', 'admin')]))
            s['step'] = steps[i + 1]
            self.state(uid, s)
            prompts = {'description': 'Напиши описание и совместимость, до 650 символов.', 'price': 'Сколько звезд? От 1 до 1000.', 'photo': 'Пришли картинку товара как фото.', 'file': 'Теперь отправь файл конфига документом.', 'instructions': 'Напиши инструкцию. Она придет вместе с покупкой. До 3500 символов.'}
            return self.say(uid, prompts[s['step']])
        if kind == 'review':
            if not self.eligible(uid, s['product']):
                self.state(uid, {})
                return self.say(uid, 'Нет активной покупки для отзыва.')
            if s['step'] == 'name':
                if not 1 <= len(text) <= 32 or '@' in text or public_text(text) != text or m.get('entities'):
                    return self.say(uid, 'Напиши обычное имя до 32 символов, без ссылок и @. Или нажми «Анонимно».')
                s.update(name=text, step='text')
                self.state(uid, s)
                return self.say(uid, 'Напиши отзыв, до 600 символов.')
            if s['step'] == 'text':
                if not 1 <= len(text) <= 600:
                    return self.say(uid, 'Нужен отзыв от 1 до 600 символов.')
                # Editing an existing review does not silently reverse moderation.
                self.execute('''INSERT INTO reviews(uid,product,stars,name,text) VALUES(?,?,?,?,?)
                  ON CONFLICT(uid,product) DO UPDATE SET stars=excluded.stars,name=excluded.name,text=excluded.text''',
                  (uid, s['product'], s['stars'], s['name'], public_text(text)))
                self.state(uid, {})
                return self.say(uid, 'Спасибо за отзыв 🤝', keyboard([('В меню', 'home')]))
        return self.say(uid, 'Открой меню через /start. Отменить текущий шаг: /cancel.')

    def update(self, u):
        if 'pre_checkout_query' in u:
            return self.precheckout(u['pre_checkout_query'])
        if 'message' in u:
            return self.message(u['message'])
        if 'callback_query' in u:
            q = u['callback_query']
            try:
                self.api('answerCallbackQuery', callback_query_id=q['id'])
            except Exception:
                pass
            if q.get('message', {}).get('chat', {}).get('type') == 'private':
                try:
                    return self.callback(q['from']['id'], q.get('data', ''))
                except (ValueError, KeyError):
                    return self.say(q['from']['id'], 'Кнопка устарела. Открой /start.')


def main():
    load_env()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    token = os.getenv('BOT_TOKEN', '')
    admins = {int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip().isdigit()}
    support = os.getenv('SUPPORT_CONTACT', '')
    if not token or not admins or not support:
        raise SystemExit('Заполни BOT_TOKEN, ADMIN_IDS и SUPPORT_CONTACT в .env')
    db = os.getenv('DATABASE_PATH', 'data/shop.sqlite3')
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    api = Telegram(token)
    shop = Shop(db, api, admins, support)
    if api('getWebhookInfo').get('url'):
        raise SystemExit('У бота включен webhook. Отключи его перед запуском polling.')
    api('setMyName', name=NAME)
    api('setMyCommands', commands=[{'command': c, 'description': d} for c, d in [('start', 'Магазин конфигов'), ('admin', 'Админ-панель'), ('id', 'Мой Telegram ID'), ('receipt', 'Номера покупок'), ('paysupport', 'Помощь с оплатой'), ('terms', 'Условия покупки'), ('privacy', 'Данные и отзывы'), ('cancel', 'Отмена')]])
    LOG.info('Shop started')
    while True:
        try:
            r = shop.one("SELECT value FROM meta WHERE key='offset'")
            updates = api('getUpdates', offset=int(r[0]) if r else 0, timeout=20, allowed_updates=['message', 'callback_query', 'pre_checkout_query'])
            # Confirm checkout before slower catalog operations in the same batch.
            for u in sorted(updates, key=lambda x: 0 if 'pre_checkout_query' in x else 1):
                try:
                    shop.update(u)
                except Exception:
                    LOG.warning('Update %s failed', u['update_id'])
                    # Payment updates must never be acknowledged before durable storage.
                    if 'successful_payment' in u.get('message', {}) or 'refunded_payment' in u.get('message', {}):
                        raise
            if updates:
                shop.execute("INSERT OR REPLACE INTO meta VALUES('offset',?)", (str(max(u['update_id'] for u in updates) + 1),))
            shop.retry_deliveries()
        except KeyboardInterrupt:
            break
        except Exception:
            LOG.warning('Connection or processing failed. Retrying in 3 seconds.')
            time.sleep(3)


if __name__ == '__main__':
    main()
